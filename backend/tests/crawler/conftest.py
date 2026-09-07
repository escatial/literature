# -*- coding: utf-8 -*-
"""知网爬虫测试公共设施:本地 mock 知网服务器 + 隔离的爬虫运行环境。

原则:
- 绝不向真实知网发请求(所有端点指向 127.0.0.1 随机端口)
- 每个用例独立的 CONFIG/session/限速/令牌状态,互不污染
- sleep_jitter 默认替换为记录器:退避断言看记录值,用例秒级完成
"""
import copy
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
import requests
from requests.adapters import HTTPAdapter

# backend/src 加入 import 路径(crawler 包依赖),conftest 所在目录加入(helpers)
BACKEND_SRC = Path(__file__).resolve().parents[2] / "src"
for p in (str(BACKEND_SRC), str(Path(__file__).resolve().parent)):
    if p not in sys.path:
        sys.path.insert(0, p)

from automation.cnki import crawler  # noqa: E402  (导入即 init,fixture 中覆盖全局状态)


# ========================== mock 知网服务器 ==========================
class _Handler(BaseHTTPRequestHandler):
    """HTTP 处理器:全部转发给 MockKNS.route 决策。"""

    kns = None  # 类属性注入 MockKNS 实例

    def log_message(self, *args):  # 静默访问日志
        pass

    def do_GET(self):
        self._serve("GET")

    def do_POST(self):
        self._serve("POST")

    def _serve(self, method):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", "ignore") if length else ""
        parsed = urlparse(self.path)
        if method == "POST":
            body = parse_qs(raw, keep_blank_values=True)
        else:
            body = parse_qs(parsed.query, keep_blank_values=True)
        self.kns.requests_log.append({
            "method": method, "path": parsed.path,
            "body": body, "raw": raw,
            "headers": dict(self.headers),  # 留痕请求头(签名 4 头断言用)
        })
        resp = self.kns.route(method, parsed.path, body, raw)
        if resp is None:
            self._send(404, "text/plain", "mock-404", [])
            return
        status, ctype, payload, extra = resp
        self._send(status, ctype, payload, extra)

    def _send(self, status, ctype, payload, extra):
        data = payload.encode("utf-8") if isinstance(payload, str) else payload
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        for k, v in extra:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class MockKNS:
    """本地知网模拟器:脚本化响应 + 请求留痕。

    用法:
      kns.push_grid(page1_html, page2_html)   # 依序弹出,弹完 sticky 最后一个
      kns.grid_callable = fn                  # 或按请求体动态决策(优先级更高)
      kns.details["/kcms2/detail/xx.html"] = (200, "text/html", html)
      kns.requests_log                        # 断言发出过什么请求
    """

    def __init__(self):
        self.requests_log = []
        self.grid_script = []
        self.grid_callable = None          # callable(body:dict, raw:str) -> str
        self.adv_script = []
        self.adv_search_html = "<html>高级检索</html>"
        self.home_html = "<html>home</html>"
        self.home_cookies = {"KNS2COOKIE": "fresh-token"}
        self.details = {}                  # path -> (status, ctype, body|callable)
        self.export_script = []
        self.export_sticky = {"code": 1, "data": []}
        self._server = None
        self._thread = None

    # ---------- 生命周期 ----------
    def start(self):
        handler = type("_H", (_Handler,), {"kns": self})
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._thread.join(timeout=3)

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    # ---------- 脚本注册 ----------
    def push_grid(self, *items, sticky=True):
        """注册列表接口响应序列;sticky=True 时弹完后重复最后一个。"""
        self.grid_script.extend(items)
        if sticky and items:
            self.grid_sticky = items[-1]
        elif sticky:
            pass

    grid_sticky = ""

    def push_adv(self, *items, sticky=True):
        self.adv_script.extend(items)
        if sticky and items:
            self.adv_search_html = items[-1]

    def push_export(self, *items, sticky=True):
        self.export_script.extend(items)
        if sticky and items:
            self.export_sticky = items[-1]

    # ---------- 路由 ----------
    def route(self, method, path, body, raw):
        if method == "GET" and path == "/":
            cookies = [("Set-Cookie", f"{k}={v}; Path=/") for k, v in self.home_cookies.items()]
            return 200, "text/html; charset=utf-8", self.home_html, cookies
        if method == "GET" and path == "/kns8s/AdvSearch":
            if self.adv_script:
                return 200, "text/html; charset=utf-8", self.adv_script.pop(0), []
            return 200, "text/html; charset=utf-8", self.adv_search_html, []
        if method == "POST" and path == "/kns8s/brief/grid":
            if self.grid_callable is not None:
                return 200, "text/html; charset=utf-8", self.grid_callable(body, raw), []
            if self.grid_script:
                return 200, "text/html; charset=utf-8", self.grid_script.pop(0), []
            return 200, "text/html; charset=utf-8", self.grid_sticky, []
        if method == "POST" and path.endswith("/GetExport"):
            item = self.export_script.pop(0) if self.export_script else self.export_sticky
            if callable(item):
                item = item(body)
            return 200, "application/json", json.dumps(item, ensure_ascii=False), []
        if path in self.details:
            item = self.details[path]
            if callable(item):
                item = item()
            status, ctype, payload = item
            return status, ctype, payload, []
        return None

    # ---------- 断言辅助 ----------
    def grid_requests(self):
        return [r for r in self.requests_log if r["path"] == "/kns8s/brief/grid"]


@pytest.fixture
def kns():
    server = MockKNS()
    server.start()
    yield server
    server.stop()


# ========================== 爬虫隔离环境 ==========================
@pytest.fixture
def crawler_env(kns, tmp_path, monkeypatch):
    """把爬虫全局状态切到 mock 服务器 + tmp 目录,并冻结真实睡眠。"""
    cfg = copy.deepcopy(crawler.DEFAULT_CONFIG)
    base = kns.base_url
    cfg["endpoints"] = {
        "base": base,
        "search": base + "/kns8s/brief/grid",
        "adv_search": base + "/kns8s/AdvSearch",
        "verify_api": base + "/verify-api/web",
        "verify_home": base + "/verify/home",
    }
    cfg["paths"]["cookies_file"] = str(tmp_path / "cookies.json")
    cfg["paths"]["captcha_back_image"] = str(tmp_path / "captcha_back.jpg")
    cfg["paths"]["debug_abstract_html"] = str(tmp_path / "debug_abstract.html")
    cfg["paths"]["failed_file"] = str(tmp_path / "failed.json")
    cfg["runtime"]["delay_seconds"] = 0.01
    cfg["http"]["timeout"] = 5

    monkeypatch.setattr(crawler, "CONFIG", cfg)

    s = requests.Session()
    s.mount("http://", HTTPAdapter(max_retries=0, pool_connections=4, pool_maxsize=4))
    s.cookies.update({"KNS2COOKIE": "test-kns", "Ecp_ClientId": "test-client"})
    monkeypatch.setattr(crawler, "session", s)

    cookies = {"KNS2COOKIE": "test-kns", "Ecp_ClientId": "test-client"}
    monkeypatch.setattr(crawler, "COOKIES", cookies)
    monkeypatch.setattr(crawler, "_turnpage", "TP_TOKEN")
    monkeypatch.setattr(crawler, "_cookie_refresh_ts", 0.0)
    monkeypatch.setattr(crawler, "_empty_abstract_streak", 0)
    crawler.throttle_init(0.01)

    # 真实 sleep → 记录器(退避断言看记录值;节奏类压测自行恢复真实现)
    sleeps = []
    monkeypatch.setattr(crawler, "sleep_jitter", lambda base, _log=sleeps: _log.append(base))

    return SimpleNamespace(cfg=cfg, sleeps=sleeps, kns=kns, cookies=cookies)
