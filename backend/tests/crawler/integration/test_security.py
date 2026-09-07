#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""爬虫系统安全测试（五大维度之安全 + 异常处理）。

覆盖面：
- S-2 回归：全局 500 处理器响应体不得携带 traceback/异常类名/内部路径；
- 路径穿越矩阵：task_id 注入 ../ %2e%2e %5C %00 超长串 → 只许 404/422，绝不 500；
- SQL 注入样例：task_id 携带注入语句（内存 dict 查找，无 SQL 面）→ 404；
- XSS 存储契约：后端原样存储、JSON+application/json 响应，转义责任在前端
  （Vue 插值默认转义；后端绝不以 text/html 上下文回显用户数据）；
- 参数白名单：pydantic 忽略未知字段 + registry 白名单 ValueError→422 双重防护；
- reset_breaker 未知名：get-or-create 幂等复位（记录性验证，非 500）；
- S-1 回归：cookies.json / debug_*.html 必须被 git 忽略且退出跟踪；
- CORS：preflight 放开来源但绝不携带 allow-credentials=true；
- 代理凭据打码：/crawler/proxies 响应不得出现用户名/密码明文。

路径约定：与 test_admin_api / test_cnki_api_contract 一致，独立 app 直接
挂 router（无 /api 前缀）——生产由 main.include_router(prefix="/api") 补齐。
"""
import subprocess
from pathlib import Path

import fastapi
import pytest
from fastapi.testclient import TestClient

from automation.cnki import monitor as m_monitor
from automation.cnki import proxy_pool as m_proxy_pool
from automation.cnki import resilience as m_resilience
from automation.cnki import scheduler as m_scheduler
from api import cnki as cnki_api
from api import crawler_admin

# 仓库根（tests/crawler/integration/test_security.py → 上溯 4 级）
_REPO_ROOT = Path(__file__).resolve().parents[4]


@pytest.fixture(autouse=True)
def _isolate_singletons(monkeypatch):
    """全局单例与环境隔离（前后各重置一次），另隔离断路器表与 cnki 任务态。

    - reset_breaker 是 get-or-create：测试注入的垃圾名会驻留 _breakers，
      用 monkeypatch 换空 dict，teardown 自动还原；
    - 挂载 cnki router 后须清空任务三字典，防用例间串扰。
    """
    monkeypatch.delenv("CNKI_PROXY_MODE", raising=False)
    monkeypatch.delenv("CNKI_PROXY_LIST", raising=False)  # pool.load 会读环境变量
    monkeypatch.setattr(m_resilience, "_breakers", {})
    cnki_api._task_results.clear()
    cnki_api._task_queues.clear()
    cnki_api._CANCEL_EVENTS.clear()
    m_monitor.reset_registry()
    m_resilience.reset_alert_manager()
    m_scheduler.reset_pool()
    m_proxy_pool.reset_proxy_pool()
    yield
    cnki_api._task_results.clear()
    cnki_api._task_queues.clear()
    cnki_api._CANCEL_EVENTS.clear()
    m_monitor.reset_registry()
    m_resilience.reset_alert_manager()
    m_scheduler.reset_pool()
    m_proxy_pool.reset_proxy_pool()


@pytest.fixture()
def client():
    """独立 FastAPI 实例挂载 crawler_admin + cnki 两个 router（不触发 lifespan）。"""
    app = fastapi.FastAPI()
    app.include_router(crawler_admin.router)
    app.include_router(cnki_api.router)
    return TestClient(app)


# ========================== S-2 回归：500 响应体脱敏 ==========================
def test_s2_500_response_no_internal_info():
    """S-2 回归：未捕获异常的 500 响应必须是统一文案，不泄露任何内部信息。

    将 main._unhandled_exc（生产同款处理器）挂到独立 app，触发未捕获
    RuntimeError（消息里埋入内部路径样例），断言响应体不含：
    异常类名 / 内部消息 / Traceback 字样 / .py 文件名。
    """
    from main import _unhandled_exc

    app = fastapi.FastAPI()

    @app.get("/boom")
    def boom():
        raise RuntimeError("SECRET-INTERNAL C:\\Users\\x\\db\\session.py line 1")

    app.add_exception_handler(Exception, _unhandled_exc)
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.get("/boom")
    assert resp.status_code == 500
    text = resp.text
    assert "SECRET-INTERNAL" not in text, "内部异常消息泄露到响应体"
    assert "RuntimeError" not in text, "异常类名泄露到响应体"
    assert "Traceback" not in text.lower(), "traceback 字样泄露到响应体"
    assert "session.py" not in text, "内部文件路径泄露到响应体"
    assert resp.json() == {"detail": "服务器内部错误，请稍后重试"}


# ========================== 路径穿越矩阵 ==========================
_TRAVERSAL_PAYLOADS = [
    "..%2F..%2Fetc%2Fpasswd",  # 编码斜杠上跳
    "%2e%2e%2f%2e%2e%2fwindows%2fwin.ini",  # 全编码点号
    "..%5C..%5Cwindows%5Cwin.ini",  # 反斜杠变体
    "a%00b",  # null 字节截断
    "x" * 300,  # 超长串
    "../etc/passwd",  # 未编码字面量
]


@pytest.mark.parametrize("payload", _TRAVERSAL_PAYLOADS)
def test_path_traversal_matrix_never_500(client, payload):
    """task_id 路径穿越矩阵：双端点只许 404/422，绝不 500/异常逃逸。"""
    for url in (f"/crawler/tasks/{payload}", f"/cnki/stream/{payload}"):
        resp = client.get(url)
        assert resp.status_code in (404, 422), f"{url[:60]} → {resp.status_code}"


@pytest.mark.parametrize(
    "payload",
    [
        "'; DROP TABLE crawler_tasks;--",
        "1' OR '1'='1",
        "admin'--",
        "%27%29%3B--",
    ],
)
def test_sql_injection_task_id_never_500(client, payload):
    """task_id 携带 SQL 注入语句：任务查找是内存 dict，无 SQL 面 → 恒 404。"""
    resp = client.get(f"/crawler/tasks/{payload}")
    assert resp.status_code == 404


# ========================== XSS 存储契约 ==========================
def test_xss_payload_stored_raw_json_contract(client):
    """XSS payload 原样存储 + JSON 契约：后端不做 HTML 上下文回显。

    契约：任务字段含 <script>/<img onerror> 时，detail API 仍以
    application/json 原样返回（不包装 HTML、不拼接模板）——转义由
    前端 Vue 插值层默认完成。若未来某端点改成 text/html 回显，
    此用例即失效并暴露存储型 XSS 面。
    """
    m_monitor.get_registry().register(
        "t-xss",
        query="<script>alert('xss')</script>",
        params={"topic": "<img src=x onerror=alert(1)>"},
    )
    resp = client.get("/crawler/tasks/t-xss")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    body = resp.json()
    assert body["query"] == "<script>alert('xss')</script>"
    assert body["params"]["topic"] == "<img src=x onerror=alert(1)>"


# ========================== 参数白名单与越界 ==========================
def test_update_params_extra_field_ignored(client):
    """白名单外字段被 pydantic 静默忽略：不落账、不报错、不影响合法键。"""
    m_monitor.get_registry().register("t-p", params={"delay_seconds": 1.0})
    resp = client.put(
        "/crawler/tasks/t-p/params",
        json={"delay_seconds": 2.5, "evil_key": "pwn", "stop_event": True},
    )
    assert resp.status_code == 200
    params = resp.json()["params"]
    assert "evil_key" not in params, "白名单外字段落账"
    assert "stop_event" not in params, "控制面字段不得经参数接口注入"
    assert params["delay_seconds"] == 2.5


@pytest.mark.parametrize(
    "patch",
    [
        {"delay_seconds": -1},
        {"delay_seconds": 0},
        {"max_workers": 0},
        {"max_workers": 17},
        {"page_size": 9},
        {"page_size": 51},
        {"max_per_keyword": 0},
    ],
)
def test_update_params_out_of_range_422(client, patch):
    """越界值矩阵：pydantic 约束层直接 422，不到达 registry。"""
    m_monitor.get_registry().register("t-p")
    resp = client.put("/crawler/tasks/t-p/params", json=patch)
    assert resp.status_code == 422


def test_update_params_empty_patch_and_unknown_task(client):
    """空补丁(None 值全滤) → 422；未知任务 → 404，不误报 500。"""
    m_monitor.get_registry().register("t-p")
    resp = client.put("/crawler/tasks/t-p/params", json={"delay_seconds": None})
    assert resp.status_code == 422
    resp = client.put("/crawler/tasks/no-such-task/params", json={"delay_seconds": 2})
    assert resp.status_code == 404


# ========================== 断路器复位（get-or-create 幂等） ==========================
def test_reset_breaker_unknown_name_idempotent(client):
    """未知名 reset_breaker：get-or-create 语义 → 幂等复位返回 200（非 500）。

    记录性验证：运维语义是「保证名字对应的断路器处于 closed」，未知名
    创建即复位，符合幂等预期；快照中出现该名字是设计行为而非副作用。
    """
    resp = client.post("/crawler/breakers/no-such-breaker/reset")
    assert resp.status_code == 200
    assert resp.json() == {"name": "no-such-breaker", "reset": True}
    snap_text = str(client.get("/crawler/breakers").json())
    assert "no-such-breaker" in snap_text


# ========================== S-1 回归：敏感文件退出 git 跟踪 ==========================
def test_s1_sensitive_files_git_ignored():
    """S-1 回归：cookies.json 与 debug_*.html 必须被 .gitignore 覆盖且零跟踪。

    cookies.json 含知网会话 cookie（真实 token/IP），一旦入库即泄露凭据。
    """
    targets = [
        "backend/src/automation/cnki/data/cookies.json",
        "backend/src/automation/cnki/data/debug_20260903.html",
    ]
    for rel in targets:
        proc = subprocess.run(
            ["git", "check-ignore", "-v", rel],
            cwd=_REPO_ROOT, capture_output=True, text=True,
        )
        assert proc.returncode == 0, f"{rel} 未被 .gitignore 覆盖"
        assert ".gitignore" in proc.stdout
    tracked = subprocess.run(
        ["git", "ls-files", "backend/src/automation/cnki/data/"],
        cwd=_REPO_ROOT, capture_output=True, text=True,
    )
    assert "cookies.json" not in tracked.stdout, "cookies.json 仍被 git 跟踪"
    assert not any("debug_" in line for line in tracked.stdout.splitlines()), \
        "debug 快照仍被 git 跟踪"


# ========================== CORS：放开来源但不带凭据 ==========================
def test_cors_preflight_no_credentials():
    """CORS preflight：allow_origins=* 但 allow_credentials=False，
    响应绝不允许携带 access-control-allow-credentials=true（否则任意
    来源可携 cookie 调用管理面）。"""
    from main import app as main_app

    client = TestClient(main_app)
    resp = client.options(
        "/api/cnki/start",
        headers={
            "Origin": "http://evil.example",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") == "*"
    assert resp.headers.get("access-control-allow-credentials") != "true"


# ========================== 代理凭据打码（数据脱敏） ==========================
def test_proxy_credentials_masked_in_snapshot_api(client):
    """代理池快照 API：带凭据的代理 URL 必须打码为 scheme://***@host:port。"""
    m_proxy_pool.init_proxy_pool({
        "mode": "off",
        "pool": ["http://alice:supersecret@10.0.0.8:8080"],
    })
    resp = client.get("/crawler/proxies")
    assert resp.status_code == 200
    text = resp.text
    assert "supersecret" not in text, "代理密码明文泄露"
    assert "alice" not in text, "代理用户名明文泄露"
    assert "***@10.0.0.8:8080" in text
