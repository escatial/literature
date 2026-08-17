#!/usr/bin/env python
# -*- coding: utf-8 -*-


import argparse
import base64
import csv
import io
import json
import os
import random
import re
import sys
import threading
import time
from hashlib import md5
from pathlib import Path
from urllib.parse import urlparse, parse_qs, quote

import requests
import yaml
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from lxml import etree
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .cjy_client import (
    CjyClient,
    CaptchaDispatcher,
    CjyError,
    CjyFatalError,
    RecognizeResult,
)

# ========================== 路径（嵌入后全部绝对化）==========================
# 本模块目录: backend/src/automation/cnki
_PKG_DIR = Path(__file__).resolve().parent
# 运行时文件目录(自动创建): cookies.json / 滑块背景图 / 调试页 / 失败清单
_DATA_DIR = _PKG_DIR / "data"

# ========================== 内置兜底默认（最末优先级）==========================
DEFAULT_CONFIG = {
    "chaojiying": {
        "user": "",
        "pass": "",
        "soft_id": "",
        "codetype": 9902,
    },
    "sign": {
        "app_id": "LoginWap",
        "secret": "",
    },
    "endpoints": {
        "base": "https://kns.cnki.net",
        "search": "https://kns.cnki.net/kns8s/brief/grid",
        "adv_search": "https://kns.cnki.net/kns8s/AdvSearch",
        "verify_api": "https://kns.cnki.net/verify-api/web",
        "verify_home": "https://kns.cnki.net/verify/home",
    },
    "http": {
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36",
        "sec_ch_ua": '"Not=A?Brand";v="99", "Google Chrome";v="151", "Chromium";v="151"',
        "sec_ch_ua_platform": '"Windows"',
        "sec_ch_ua_mobile": "?0",
        "accept_language": "zh-CN,zh;q=0.9",
        "uniplatform": "NZKPT",
        "timeout": 20,
        # 翻页 token：会话绑定，可能随 cookie 失效而变化（运行时优先从高级检索页提取）
        "turnpage": "8Kf6r96aVUubfe4hUZXU-w%21%21",
    },
    "search": {
        "default_field": "SU",
        "default_operator": "TOPRANK",
        "default_resource": "CAPJ",
        "page_size": 20,
        "max_per_keyword": 20,
    },
    "resource_map": {
        "CAPJ":    ["YSTT4HG0,LSTPFY1C,EMRPGLPA,JUP3MUPD,MPMFIG1A,WQ0UVIAA,BLZOG7CK,PWFIRAGL,NN3FJMUV,NLBO1Z6R", "WD0FTY92"],
        "CAPM":    ["CDMD,CDMDL", "WD0FTY92"],
        "CAJD":    ["CPFD", "WD0FTY92"],
        "CCND":    ["CCND", "WD0FTY92"],
        "CIBD":    ["CIPD", "WD0FTY92"],
        "CROSSDB": ["YSTT4HG0,LSTPFY1C,EMRPGLPA,JUP3MUPD,MPMFIG1A,WQ0UVIAA,BLZOG7CK,PWFIRAGL,NN3FJMUV,NLBO1Z6R", "WD0FTY92"],
    },
    "runtime": {
        "delay_seconds": 1.0,
        "retry_times": 3,
        "progress_bar_width": 30,
    },
    "paths": {
        "cookies_file": "cookies.json",
        "config_file": "config.yaml",
        "captcha_back_image": "captcha_back.jpg",
        "debug_abstract_html": "debug_abstract.html",
        "failed_file": "failed.json",
        "default_output_prefix": "result",
    },
}


# ========================== 配置加载 ==========================
def deep_merge(base: dict, override: dict) -> dict:
    """深度合并字典，override 优先级更高"""
    result = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def load_config(path: str = None) -> dict:
    """加载 yaml 配置，与内置默认合并；path 缺省用包内 config.yaml"""
    path = path or str(_PKG_DIR / "config.yaml")
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                user_cfg = yaml.safe_load(f) or {}
            cfg = deep_merge(cfg, user_cfg)
            print(f"[配置] 已加载 {path}")
        except Exception as e:
            print(f"[警告] 解析 {path} 失败: {e}，使用内置默认")
    else:
        print(f"[配置] {path} 不存在，使用内置默认（建议创建）")
    return cfg


def load_cookies(path: str) -> dict:
    """加载 cookie 文件（去掉以 _ 开头的注释字段）"""
    if not os.path.exists(path):
        print(f"[cookies] {path} 不存在，将不带 cookie 启动")
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return {k: v for k, v in data.items() if not k.startswith("_")}
    except Exception as e:
        print(f"[警告] 读取 {path} 失败: {e}")
        return {}


def save_cookies(cookies: dict, path: str):
    """保存 cookie 到文件"""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cookies, f, ensure_ascii=False, indent=2)
    print(f"[cookies] 已保存到 {path}")


def get_env_override(cfg: dict) -> dict:
    """从环境变量读取覆盖值（CNKI_<SECTION>_<KEY>=value），并按内置默认值类型转换"""
    env_cfg = {}
    for k, v in os.environ.items():
        if not k.startswith("CNKI_"):
            continue
        parts = k[5:].lower().split("_", 1)
        if len(parts) != 2:
            continue
        section, key = parts
        env_cfg.setdefault(section, {})[key] = v
    if not env_cfg:
        return cfg
    merged = deep_merge(cfg, env_cfg)
    # 环境变量全是字符串，须按内置默认类型转回（否则 page_size=20 变 "20" 会崩 min()/比较运算）
    for section, kv in env_cfg.items():
        for key, val in kv.items():
            default_val = DEFAULT_CONFIG.get(section, {}).get(key)
            env_name = f"CNKI_{section.upper()}_{key.upper()}"
            if isinstance(default_val, bool):
                merged[section][key] = val.lower() in ("1", "true", "yes", "on")
            elif isinstance(default_val, int):
                try:
                    merged[section][key] = int(val)
                except ValueError:
                    print(f"[警告] {env_name}={val!r} 无法转为整数，已忽略")
                    merged[section][key] = default_val
            elif isinstance(default_val, float):
                try:
                    merged[section][key] = float(val)
                except ValueError:
                    print(f"[警告] {env_name}={val!r} 无法转为浮点数，已忽略")
                    merged[section][key] = default_val
    return merged


def pick_first(env_names: tuple, cfg_val: str, label: str) -> str:
    """环境变量优先，其次配置文件；两者都空则返回空串"""
    for n in env_names:
        v = os.environ.get(n)
        if v:
            return v
    return cfg_val or ""


# ========================== 全局状态（init() 重建）==========================
CONFIG: dict = {}
COOKIES: dict = {}
session: requests.Session | None = None
cj: CjyClient | None = None
dispatcher: CaptchaDispatcher | None = None
_turnpage = ""


def init() -> None:
    """加载/重载配置与会话（模块导入时自动调用；保存配置后手动调用热重载）。"""
    global CONFIG, COOKIES, session, cj, dispatcher, _turnpage

    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG = get_env_override(load_config())

    # 运行时文件全部落到包内 data/ 目录，绝对路径，与工作目录解耦
    for key, name in (
        ("cookies_file", "cookies.json"),
        ("captcha_back_image", "captcha_back.jpg"),
        ("debug_abstract_html", "debug_abstract.html"),
        ("failed_file", "failed.json"),
    ):
        CONFIG["paths"][key] = str(_DATA_DIR / name)
    CONFIG["paths"]["config_file"] = str(_PKG_DIR / "config.yaml")

    COOKIES = load_cookies(CONFIG["paths"]["cookies_file"])

    # ---------- session 初始化 ----------
    s = requests.Session()
    s.headers.update({
        "User-Agent": CONFIG["http"]["user_agent"],
        "Accept-Language": CONFIG["http"]["accept_language"],
        "sec-ch-ua": CONFIG["http"]["sec_ch_ua"],
        "sec-ch-ua-platform": CONFIG["http"]["sec_ch_ua_platform"],
        "sec-ch-ua-mobile": CONFIG["http"]["sec_ch_ua_mobile"],
        "uniplatform": CONFIG["http"]["uniplatform"],
    })
    # 知网为国内站,默认直连:不读取系统代理(Clash 等)。
    # 若代理出口节点证书与 kns.cnki.net 不匹配,会报 TLS "Hostname mismatch",
    # 表现为从某篇文章起连续"抓取失败"。config.yaml http.trust_env 可开回代理。
    s.trust_env = bool(CONFIG.get("http", {}).get("trust_env", False))
    # 连接池 + 瞬断重试（3 次指数退避），应对知网偶发 5xx/429/超时
    retry_cfg = Retry(
        total=3, connect=3, read=3, status=3,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST", "HEAD"]),
    )
    adapter = HTTPAdapter(max_retries=retry_cfg, pool_connections=10, pool_maxsize=10)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    for k, v in COOKIES.items():
        s.cookies.set(k, v, domain=CONFIG["endpoints"]["base"].replace("https://", ""))
    session = s

    # ---------- 超级鹰客户端（凭据优先读环境变量 CJY_*，避免明文落盘泄露）----------
    cjy_user = pick_first(
        ("CNKI_CJY_USER", "CHAOJIYING_USER", "CJY_USER"),
        CONFIG["chaojiying"]["user"],
        "超级鹰账号",
    )
    cjy_pass = pick_first(
        ("CNKI_CJY_PASS", "CHAOJIYING_PASS", "CJY_PASS"),
        CONFIG["chaojiying"]["pass"],
        "超级鹰密码",
    )
    cjy_soft = pick_first(
        ("CNKI_CJY_SOFT_ID", "CHAOJIYING_SOFT_ID", "CJY_SOFT_ID"),
        CONFIG["chaojiying"]["soft_id"],
        "超级鹰 soft_id",
    )
    if not (cjy_user and cjy_pass and cjy_soft):
        print(
            "[警告] 超级鹰凭据缺失，验证码识别功能不可用",
            file=sys.stderr,
        )
    cj = CjyClient(
        cjy_user,
        cjy_pass,
        cjy_soft,
        timeout=CONFIG["http"]["timeout"],
        retry_times=CONFIG["runtime"]["retry_times"],
    )
    # 全场景验证码调度器（滑块 / 英数 / 点选 自动降级）
    dispatcher = CaptchaDispatcher(cj)

    # 翻页 token：会话绑定，运行时优先从高级检索页提取，提取失败用配置兜底
    _turnpage = CONFIG["http"].get("turnpage", "")


def extract_turnpage(html: str) -> str:
    """从页面 JS 中提取 turnpage token（形如 turnpage='xxx'），找不到返回空串"""
    m = re.search(r"turnpage\s*=\s*['\"]([^'\"]+)['\"]", html or "")
    return m.group(1) if m else ""


def sleep_jitter(base_seconds: float):
    """带随机抖动的休眠（±20%），降低请求节奏的机器特征，缓解风控"""
    time.sleep(max(base_seconds * random.uniform(0.8, 1.2), 0))


# ========================== 过程日志回调（嵌入后用于实时推送前端）==========================
# 线程本地：CLI 主线程与后端 executor 线程互不干扰；并发跑多个任务也不会串日志
_log_local = threading.local()


def set_log_callback(cb):
    """设置当前线程的过程日志回调（后端把它转发到 SSE）；传 None 恢复纯 stdout 输出。"""
    _log_local.callback = cb


def emit_log(msg: str):
    """过程日志：有回调则转发给调用方，同时始终打印到 stdout（CLI 兼容）。"""
    cb = getattr(_log_local, "callback", None)
    if cb:
        try:
            cb(msg)
        except Exception:
            pass
    print(msg)


# ========================== 签名算法 ==========================
def make_signature(url: str, client_id: str) -> dict:
    parsed = urlparse(url)
    qs = parse_qs(parsed.query, keep_blank_values=True)
    sorted_keys = sorted(qs.keys(), key=lambda x: x.lower())
    sorted_params2 = "".join(f"{k}={qs[k][0]}" for k in sorted_keys)

    timestamp = int(time.time() * 1000)
    # 13 位随机数字 nonce（服务端按请求里提交的 nonce 重算签名，不校验 nonce 生成方式）
    nonce = f"{random.randrange(10 ** 13):013d}"
    # 注意：当前仅对 URL query 签名，而本请求 URL 无 query → 签名串实际不含任何业务参数。
    # 知网一旦开启 body 参数签名校验将立即失效，需对照浏览器逆向同步算法。
    sign_str = f"{timestamp}{nonce}{CONFIG['sign']['secret']}{sorted_params2}{client_id}"
    signature = md5(sign_str.encode("utf-8")).hexdigest()

    return {
        "timestamp": str(timestamp),
        "nonce": nonce,
        "signature": signature,
        "appID": CONFIG["sign"]["app_id"],
    }


# ========================== 检索式构造 ==========================
def build_query(
    keyword: str,
    field: str = None,
    operator: str = None,
    resource: str = None,
    classid: str = None,
    extra: list = None,
) -> str:
    field = field or CONFIG["search"]["default_field"]
    operator = operator or CONFIG["search"]["default_operator"]
    resource = resource or CONFIG["search"]["default_resource"]

    qnode_item = {
        "Key": "input[data-tipid=gradetxt-1]",
        "Title": "", "Logic": 0,
        "Items": [{
            "Key": "input[data-tipid=gradetxt-1]",
            "Title": "", "Logic": 0,
            "Field": field, "Operator": operator,
            "Value": keyword, "Value2": ""
        }],
        "ChildItems": []
    }

    subject_group = {
        "Key": "Subject", "Title": "", "Logic": 0,
        "Items": [], "ChildItems": [qnode_item]
    }

    if extra:
        for cond in extra:
            subject_group["ChildItems"].append({
                "Key": "input[data-tipid=gradetxt-1]",
                "Title": "", "Logic": 1,
                "Items": [{
                    "Key": "input[data-tipid=gradetxt-1]",
                    "Title": "", "Logic": 1,
                    "Field": cond["field"],
                    "Operator": cond.get("operator", "TOPRANK"),
                    "Value": cond["value"],
                    "Value2": ""
                }],
                "ChildItems": []
            })

    # 资源代码 → (KuaKuCode, 默认 Classid)；非法代码直接报错，避免静默回退默认库
    resource_map = CONFIG["resource_map"]
    if resource not in resource_map:
        raise ValueError(
            f"未知资源代码 {resource!r}，可用: {', '.join(sorted(resource_map))}"
            "（如 CAPJ=期刊、CAPM=博硕、CROSSDB=总库）"
        )
    kua_ku, default_classid = resource_map[resource]
    # 如果用户没显式传 classid，就用资源对应的默认
    if classid is None:
        classid = default_classid

    query = {
        "Platform": "",
        "Resource": resource,
        "Classid": classid,
        "Products": "",
        "QNode": {"QGroup": [subject_group, {"Key": "ControlGroup", "Title": "", "Logic": 0, "Items": [], "ChildItems": []}]},
        "ExScope": "1",
        "SearchType": 1,
        "Rlang": "CHINESE",
        "KuaKuCode": kua_ku,
        "Expands": {},
        "View": "changeDBCh",
        "SearchFrom": 1
    }
    return quote(json.dumps(query, separators=(",", ":"), ensure_ascii=False))


def build_expert_query(
    expert_str: str,
    resource: str = None,
    classid: str = None,
) -> str:
    """
    构造专业检索 QueryJson。
    逆向自浏览器专业检索请求：检索式原文直接放入 Expert 节点 Value，
    无需解析布尔逻辑，服务端自行解析。结构差异（对比高级检索）：
      - QGroup[0].Items 放 Expert 节点（Field=EXPERT, Operator=0）
      - SearchType=4（高级检索为 1）
    示例检索式：
      SU=('卡车' + '车辆') * '无人机' * '协同' * ('配送' + '运输') * ('应急' + '救灾' + '救援') * '物资'
    """
    resource = resource or CONFIG["search"]["default_resource"]
    resource_map = CONFIG["resource_map"]
    if resource not in resource_map:
        raise ValueError(
            f"未知资源代码 {resource!r}，可用: {', '.join(sorted(resource_map))}"
            "（如 CAPJ=期刊、CAPM=博硕、CROSSDB=总库）"
        )
    kua_ku, default_classid = resource_map[resource]
    if classid is None:
        classid = default_classid

    expert_item = {
        "Key": "Expert",
        "Title": "",
        "Logic": 0,
        "Field": "EXPERT",
        "Operator": 0,
        "Value": expert_str,
        "Value2": "",
    }
    subject_group = {
        "Key": "Subject",
        "Title": "",
        "Logic": 0,
        "Items": [expert_item],
        "ChildItems": [],
    }
    query = {
        "Platform": "",
        "Resource": resource,
        "Classid": classid,
        "Products": "",
        "QNode": {"QGroup": [subject_group, {"Key": "ControlGroup", "Title": "", "Logic": 0, "Items": [], "ChildItems": []}]},
        "ExScope": "1",
        "SearchType": 4,
        "Rlang": "CHINESE",
        "KuaKuCode": kua_ku,
        "Expands": {},
        "View": "changeDBCh",
        "SearchFrom": 1
    }
    return quote(json.dumps(query, separators=(",", ":"), ensure_ascii=False))


# ========================== AES 加密 ==========================
def aes_encrypt_point(plain: str, key_str: str) -> str:
    key = key_str.encode("utf-8")[:16].ljust(16, b"\0")
    cipher = AES.new(key, AES.MODE_ECB)
    ct = cipher.encrypt(pad(plain.encode("utf-8"), AES.block_size))
    return base64.b64encode(ct).decode("utf-8")


def make_pointjson(x1, y1, x2, y2, captcha_id, secret_key=""):
    key = secret_key if secret_key else captcha_id.replace("-", "")[:16]
    candidates = [
        json.dumps({"x": x1, "y": y1}, separators=(",", ":")),
        json.dumps([{"x": x1, "y": y1}, {"x": x2, "y": y2}], separators=(",", ":")),
        f"{x1},{y1}", f"{x1},{y1}|{x2},{y2}",
        f"{x1},{y1},{captcha_id}", f"{abs(x2 - x1)}", f"{x1}",
    ]
    return [aes_encrypt_point(p, key) for p in candidates]


def save_b64_image(b64_str: str, path: str):
    if not b64_str:
        return
    if "," in b64_str:
        b64_str = b64_str.split(",", 1)[1]
    with open(path, "wb") as f:
        f.write(base64.b64decode(b64_str))


# ========================== 验证码流程 ==========================
def recognize_slider(info):
    back_path = CONFIG["paths"]["captcha_back_image"]
    save_b64_image(info["backImage"], back_path)
    if not os.path.exists(back_path):
        raise RuntimeError("滑块背景图保存失败（backImage 为空？），无法识别")
    with open(back_path, "rb") as f:
        im = f.read()
    # 调度器自动降级：9902(两图形块) → 9900(缺口定位) → 9602(水平拼图)
    # min_points=2：9900 只返回 1 个缺口坐标，不满足则继续降级 9602，避免链条在此中断
    result = dispatcher.recognize(im, "slider", require="points", min_points=2)
    pts = result.points
    if len(pts) < 2:
        raise RuntimeError(f"滑块识别结果不足两个坐标: {result.pic_str!r}")
    (x1, y1), (x2, y2) = pts[0], pts[1]
    print(f"[识别] 类型={result.codetype} 中心点 ({x1},{y1}) ({x2},{y2}) 距离={abs(x2 - x1)}")
    emit_log(f"[识别] 滑块验证码识别成功: 中心点 ({x1},{y1}) ({x2},{y2})")
    return x1, y1, x2, y2, result.pic_id


def solve_vericode(html: str) -> str:
    """
    处理知网翻页触发的 vericodeForm（5 位英数验证码）。
    从页面中提取验证码图片 → 超级鹰 1005 识别 → 返回识别文本。
    """
    # 1) 优先取 <img> 的 src（相对/绝对路径），其次 base64 data URI
    tree = etree.HTML(html)
    img_src = ""
    for node in tree.xpath('//form[contains(@id,"veri") or contains(@id,"Veri")]//img/@src'):
        img_src = node
        break
    if not img_src:
        for node in tree.xpath('//img[contains(@src,"veri") or contains(@src,"code")'
                               ' or contains(@src,"rand") or contains(@src,"captcha")]/@src'):
            img_src = node
            break
    if not img_src:
        m = re.search(r'data:image/[^;]+;base64,([^"\']+)', html)
        if m:
            im = base64.b64decode(m.group(1))
            return _recognize_alnum(im)
        raise RuntimeError("vericodeForm 页面中未找到验证码图片")

    if img_src.startswith("data:"):
        im = base64.b64decode(img_src.split(",", 1)[1])
    elif img_src.startswith("http"):
        im = session.get(img_src, timeout=CONFIG["http"]["timeout"]).content
    else:
        im = session.get(CONFIG["endpoints"]["base"] + img_src,
                         timeout=CONFIG["http"]["timeout"]).content
    return _recognize_alnum(im)


def _recognize_alnum(im: bytes) -> str:
    """英数验证码统一识别（1005 主选，失败降级 1902/1004）"""
    result = dispatcher.recognize(im, "alnum", require="text")
    text = result.text
    print(f"[识别] 类型={result.codetype} 英数验证码={text!r}")
    emit_log(f"[识别] 英数验证码识别成功: {text!r}")
    return text


def submit_vericode(code: str) -> bool:
    """
    提交英数验证码到 /kns8s/brief/checkcode。
    逆向自 kns.brief.min.js: g.ajax({url: APPPATH+"/brief/checkcode", type:"POST",
    data:{vericode:n}, success:function(o){ if(!o){ 校验通过,重新搜索 } }})
    即：响应体为空 = 通过；非空 = 错误。
    """
    headers = {
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Origin": CONFIG["endpoints"]["base"],
        "Referer": CONFIG["endpoints"]["search"],
        "X-Requested-With": "XMLHttpRequest",
    }
    resp = session.post(
        f"{CONFIG['endpoints']['base']}/kns8s/brief/checkcode",
        data={"vericode": code},
        headers=headers,
        timeout=CONFIG["http"]["timeout"],
    )
    body = resp.content.decode("utf-8", errors="ignore").strip()
    print(f"[checkcode] status={resp.status_code} body={body[:120]!r}")
    if resp.status_code != 200:
        return False
    # 成功响应为空串（JS 判断 !o）
    return not body


def submit_captcha(info, x1, y1, x2, y2, pic_id=""):
    candidates = make_pointjson(x1, y1, x2, y2, info["captchaId"], info.get("secretKey") or "")
    headers = {
        "Origin": CONFIG["endpoints"]["base"],
        "Referer": f"{CONFIG['endpoints']['verify_home']}?{info['qs']}",
        "Content-Type": "application/json;charset=UTF-8",
        "uniplatform": CONFIG["http"]["uniplatform"],
    }
    ok = False
    last_err = None
    for idx, pj in enumerate(candidates):
        ts = int(time.time() * 1000)
        payload = {
            "captchaType": "blockPuzzle",
            "pointJson": pj,
            "ident": info["ident"],
            "returnUrl": info["returnUrl"],
            "token": info["token"],
            "ts": ts,
        }
        try:
            resp = session.post(f"{CONFIG['endpoints']['verify_api']}/check", json=payload,
                                headers=headers, timeout=CONFIG["http"]["timeout"])
            ret = resp.json()
        except Exception as e:
            last_err = e
            print(f"[check] 候选{idx}: 网络异常 {e}")
            time.sleep(0.3)
            continue
        print(f"[check] 候选{idx}: → code={ret.get('code')} msg={ret.get('message')}")
        if ret.get("code") == "0":
            ok = True
            data = ret.get("data") or {}
            return data.get("captchaVerification") or data.get("token") or json.dumps(data)
        time.sleep(0.3)
    # 所有候选都失败（含网络异常）→ 判断为识别错误，调用超级鹰报错返分（3 分钟内有效）
    if pic_id:
        try:
            rr = cj.report_error(pic_id)
            print(f"[报错返分] pic_id={pic_id} → {rr}")
        except Exception as e:
            print(f"[警告] 报错返分失败: {e}")
    if not ok:
        detail = f"（最后网络异常: {last_err}）" if last_err else ""
        raise RuntimeError(f"所有候选都未通过 check{detail}")


def trigger_captcha(query_json: str) -> str:
    """触发一次搜索让 verify 验证码出现，识别后返回 captchaVerification"""
    global _turnpage
    adv_resp = session.get(CONFIG["endpoints"]["adv_search"], timeout=CONFIG["http"]["timeout"])
    extracted = extract_turnpage(adv_resp.text)
    if extracted:
        _turnpage = extracted
    body_str = (
        f"boolSearch=true&QueryJson={query_json}&pageNum=1&pageSize={CONFIG['search']['page_size']}"
        "&dstyle=listmode&boolSortSearch=false&sentenceSearch=false&productStr="
        "&searchFrom=%E8%B5%84%E6%BA%90%E8%8C%83%E5%9B%B4%EF%BC%9A%E6%80%BB%E5%BA%93%3B++"
        f"&subject=&turnpage={_turnpage}"
        "&language=&uniplatform=&CurPage=1"
    )
    client_id = session.cookies.get("Ecp_ClientId", "")
    sign = make_signature(CONFIG["endpoints"]["search"], client_id)
    headers = {
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Origin": CONFIG["endpoints"]["base"],
        "Referer": CONFIG["endpoints"]["adv_search"],
        "X-Requested-With": "XMLHttpRequest",
        **sign,
        "ClientID": client_id,
    }
    resp = session.post(CONFIG["endpoints"]["search"], data=body_str,
                        headers=headers, timeout=CONFIG["http"]["timeout"])
    body = resp.content.decode("utf-8", errors="ignore")
    print(f"[触发] 状态={resp.status_code}，body 前300={body[:300]}")
    emit_log(f"[触发] 提交滑块验证码: HTTP {resp.status_code}")

    if "verify/home" not in body:
        print("[触发] 未触发验证码")
        emit_log("[触发] 未触发验证码，直接放行")
        return ""

    m = re.search(r"verify/home\?([^\"'<>\s]+)", body)
    if not m:
        return ""
    qs = m.group(1)
    parsed = dict(re.findall(r"([^&=]+)=([^&]+)", qs))
    # 必须包含这三个关键参数，缺一即视为解析失败（参数顺序变化也能适配）
    if not {"captchaId", "ident", "returnUrl"} <= set(parsed):
        return ""
    info = {"captchaId": parsed["captchaId"], "ident": parsed["ident"],
            "returnUrl": parsed["returnUrl"], "qs": qs}

    session.get(f"{CONFIG['endpoints']['verify_home']}?{qs}", timeout=CONFIG["http"]["timeout"])
    ts = int(time.time() * 1000)
    get_payload = {"captchaType": "blockPuzzle", "clientUid": None,
                   "ident": info["ident"], "captchaId": info["captchaId"], "ts": ts}
    get_resp = session.post(
        f"{CONFIG['endpoints']['verify_api']}/get", json=get_payload,
        headers={"Origin": CONFIG["endpoints"]["base"],
                 "Referer": f"{CONFIG['endpoints']['verify_home']}?{qs}",
                 "Content-Type": "application/json;charset=UTF-8",
                 "uniplatform": CONFIG["http"]["uniplatform"]},
        timeout=CONFIG["http"]["timeout"],
    )
    gd = get_resp.json()
    if gd.get("code") != "0":
        raise RuntimeError(f"get 失败: {gd}")
    info.update({"token": gd["data"].get("token"), "secretKey": gd["data"].get("secretKey"),
                 "blockPuzzleImage": gd["data"].get("blockPuzzleImage"),
                 "backImage": gd["data"].get("backImage")})

    x1, y1, x2, y2, pic_id = recognize_slider(info)
    return submit_captcha(info, x1, y1, x2, y2, pic_id=pic_id)


# ========================== 列表 + 搜索 ==========================
def search_grid(query_json: str, page_num: int = 1, page_size: int = None,
                captcha_verification: str = "", bool_search: str = "true"):
    """
    请求搜索结果页。
    :param bool_search: true=新搜索（可能触发验证码）；false=翻页/重搜（验证码通过后必须用 false）
                        实测：checkcode 通过后 boolSearch=true 重搜仍触发验证码，
                              boolSearch=false 才能拿到数据页
    """
    page_size = page_size or CONFIG["search"]["page_size"]
    body_str = (
        f"boolSearch={bool_search}&QueryJson={query_json}"
        f"&pageNum={page_num}&pageSize={page_size}&dstyle=listmode"
        "&boolSortSearch=false&sentenceSearch=false&productStr="
        "&searchFrom=%E8%B5%84%E6%BA%90%E8%8C%83%E5%9B%B4%EF%BC%9A%E6%80%BB%E5%BA%93%3B++"
        f"&subject=&turnpage={_turnpage}"
        "&language=&uniplatform=&CurPage=1"
    )
    if captcha_verification:
        body_str += f"&captchaVerification={captcha_verification}"

    client_id = session.cookies.get("Ecp_ClientId", "")
    sign = make_signature(CONFIG["endpoints"]["search"], client_id)
    headers = {
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Origin": CONFIG["endpoints"]["base"],
        "Referer": CONFIG["endpoints"]["adv_search"],
        "X-Requested-With": "XMLHttpRequest",
        **sign,
        "ClientID": client_id,
    }
    resp = session.post(CONFIG["endpoints"]["search"], data=body_str,
                        headers=headers, timeout=CONFIG["http"]["timeout"])
    return resp.content.decode("utf-8", errors="ignore")


def parse_list(html: str):
    tree = etree.HTML(html)
    items = []
    for td in tree.xpath('//td[@class="name"]'):
        a = td.xpath(
            './/a[contains(concat(" ", normalize-space(@class), " "), " fz14 ")]'
        )
        if not a:
            continue
        title = a[0].xpath("string(.)").strip()
        href = a[0].get("href", "")
        if href.startswith("/"):
            href = CONFIG["endpoints"]["base"] + href
        items.append({"title": title, "url": href})
    return items


def is_captcha_required(html: str) -> bool:
    return (
        "verify/home" in html
        or "captchaId=" in html
        or "vericodeForm" in html
        or "请输入验证码" in html
    )


def fetch_all_list(query_json: str, max_count: int | None = None,
                   force_captcha: bool = False, delay: float = None) -> list:
    """列表翻页抓取。

    max_count:
      - None  → 翻到知网没有更多结果为止(自然结束);
      - 正整数 → 抓到指定条数即停(防失控)。
    验证码:触发后无限重试直到通过(用户已配超级鹰),不再中途放弃。
    """
    if max_count is None or max_count <= 0:
        max_count = 10_000_000  # 实际不会触达:知网会在无数据时返回"暂无数据"
    all_items = []
    captcha_verification = ""
    # --delay 同样作用于列表翻页间隔（默认取配置值）
    delay = delay if delay is not None else CONFIG["runtime"]["delay_seconds"]
    # 关键：boolSearch=true 时服务端必然触发验证码检查（对第2页+）；
    # 一旦验证码通过，后续重搜/翻页必须用 boolSearch=false 才能拿到数据页
    bool_search = "true"

    page = 1
    while len(all_items) < max_count:
        need = max_count - len(all_items)
        cur_size = min(CONFIG["search"]["page_size"], need)

        if force_captcha and page == 1:
            captcha_verification = trigger_captcha(query_json)
            bool_search = "false"

        html = search_grid(query_json, page_num=page, page_size=cur_size,
                           captcha_verification=captcha_verification,
                           bool_search=bool_search)
        if is_captcha_required(html):
            # 分流：英数验证码(vericodeForm) 走 1005，滑块验证码(verify/home) 走 9902
            if "vericodeForm" in html or "请输入验证码" in html:
                print(f"[列表] 第{page}页触发英数验证码，走超级鹰 1005 识别...")
                emit_log(f"[列表] 第{page}页触发英数验证码，调用超级鹰识别中...")
                # 无限重试,直到通过或外部抛错(网络层)
                while True:
                    code = ""
                    try:
                        code = solve_vericode(html)
                    except Exception as e:
                        print(f"[警告] 英数识别失败: {e},重新拉取页面再试")
                        emit_log(f"[警告] 英数识别失败: {e},重新拉取页面再试")
                        sleep_jitter(1)
                        html = search_grid(query_json, page_num=page, page_size=cur_size,
                                           captcha_verification=captcha_verification,
                                           bool_search=bool_search)
                        continue
                    if code:
                        break
                    print("[警告] 英数识别返回空,重新拉取页面再试")
                    emit_log("[警告] 英数识别返回空,重新拉取页面再试")
                    sleep_jitter(1)
                    html = search_grid(query_json, page_num=page, page_size=cur_size,
                                       captcha_verification=captcha_verification,
                                       bool_search=bool_search)
                # 提交到 checkcode 接口,通过后必须用 boolSearch=false 重搜当前页
                if not submit_vericode(code):
                    print(f"[警告] 英数验证码校验未通过,重新识别...")
                    emit_log("[警告] 英数验证码校验未通过,重新识别...")
                    continue  # 回到 while True 顶部,重新 solve_vericode
                emit_log(f"[列表] 第{page}页英数验证码已通过")
                bool_search = "false"
                html = search_grid(query_json, page_num=page, page_size=cur_size,
                                   bool_search=bool_search)
            else:
                print(f"[列表] 第{page}页触发滑块验证码，走超级鹰 9902 识别...")
                emit_log(f"[列表] 第{page}页触发滑块验证码，调用超级鹰识别中...")
                # 无限重试,直到触发流程返回 captcha_verification(通过)
                while True:
                    try:
                        captcha_verification = trigger_captcha(query_json)
                    except Exception as e:
                        print(f"[警告] 滑块触发失败: {e},稍后重试")
                        emit_log(f"[警告] 滑块触发失败: {e},稍后重试")
                        sleep_jitter(1)
                        continue
                    if captcha_verification:
                        break
                    print("[警告] 滑块触发返回空,稍后重试")
                    emit_log("[警告] 滑块触发返回空,稍后重试")
                    sleep_jitter(1)
                emit_log(f"[列表] 第{page}页滑块验证码已通过")
                bool_search = "false"
                html = search_grid(query_json, page_num=page, page_size=cur_size,
                                   captcha_verification=captcha_verification,
                                   bool_search=bool_search)
        preview = html[:300].replace("\n", " ")
        print(f"[列表] 第{page}页 预览: {preview}")
        if "抱歉，暂无数据" in html or "no-content" in html:
            block = classify_block_page(html)
            if block == "login":
                print("[提示] 疑似 cookie 失效（返回登录页），请在配置页刷新 cookie")
                emit_log("[提示] 疑似 cookie 失效（返回登录页），请刷新 cookies.json")
            elif block == "security":
                print("[提示] 疑似触发安全验证（风控），请降低请求频率或稍后重试")
                emit_log("[提示] 疑似触发安全验证（风控），请降低请求频率或稍后重试")
            else:
                print(f"[列表] 第{page}页无数据，停止翻页")
                emit_log(f"[列表] 第{page}页无数据，停止翻页(已穷尽知网结果)")
            break

        items = parse_list(html)
        if not items:
            block = classify_block_page(html)
            print(f"[列表] 第{page}页 解析 0 条, block={block}, head={html[:200]!r}")
            if block == "security":
                print("[提示] 疑似触发安全验证（风控），请降低请求频率或稍后重试")
                emit_log(f"[列表] 第{page}页 疑似触发安全验证（风控），停止翻页")
                break
            if block == "login":
                print("[提示] 疑似 cookie 失效（返回登录页），请刷新 cookies.json")
                emit_log(f"[列表] 第{page}页 疑似 cookie 失效（返回登录页），停止翻页")
                break
            # 非风控：可能是偶发空响应/半加载页面，重拉一次再判定
            emit_log(f"[列表] 第{page}页 解析 0 条(block={block})，1s 后重拉一次…")
            sleep_jitter(1)
            html = search_grid(query_json, page_num=page, page_size=cur_size,
                               captcha_verification=captcha_verification,
                               bool_search=bool_search)
            items = parse_list(html)
            if not items:
                print(f"[列表] 第{page}页 重试后仍 0 条，停止")
                emit_log(f"[列表] 第{page}页 重试后仍 0 条，停止翻页")
                break
        all_items.extend(items)
        print(f"[列表] 第{page}页 抓到 {len(items)} 条，累计 {len(all_items)}")
        emit_log(f"[列表] 第{page}页 抓到 {len(items)} 条，累计 {len(all_items)}")
        if len(all_items) >= max_count:
            break
        page += 1
        # 验证码通过后翻页用 boolSearch=false 不再触发
        if bool_search == "true":
            bool_search = "false"
        sleep_jitter(delay)

    return all_items[:max_count]


# ========================== 详情页元数据 ==========================
def fetch_abstract(detail_url: str) -> dict:
    resp = session.get(detail_url, timeout=CONFIG["http"]["timeout"])
    resp.encoding = "utf-8"
    text = resp.text

    # 详情页被拦：登录页=cookie 失效；安全验证页=风控（降低频率/稍后重试）
    block = classify_block_page(text)
    if block == "security":
        raise RuntimeError("详情页触发安全验证（风控），请降低请求频率或稍后重试（delay 调大）")
    if block == "login":
        raise RuntimeError("详情页返回登录页，cookie 可能已过期（请刷新 cookie）")

    # 反爬/异常时返回 JSON：补齐全部字段键，避免 CSV 缺列
    if text.startswith("{") or text.startswith("["):
        try:
            data = resp.json()
        except Exception:
            data = {}
        return {
            "title": "", "authors": [], "orgs": [], "source": "",
            "abstract": str(data)[:200], "keywords": [], "funds": [],
            "doi": "", "album": "", "topic": "", "clc_code": "",
            "publish_time": "", "url": detail_url,
        }

    try:
        return _parse_detail(text, detail_url)
    except Exception as e:
        # 仅解析失败时保留页面供调试，避免每抓一篇都覆盖写盘
        try:
            with open(CONFIG["paths"]["debug_abstract_html"], "w", encoding="utf-8") as f:
                f.write(text)
        except Exception:
            pass
        raise


def _parse_detail(text: str, detail_url: str) -> dict:
    tree = etree.HTML(text)

    def clean(s):
        return s.replace("\n", " ").replace("\r", " ").strip() if s else ""

    def first_xpath(exprs, joiner=" "):
        for e in exprs:
            n = tree.xpath(e)
            if not n:
                continue
            # text() 表达式返回字符串列表
            if isinstance(n[0], str):
                return joiner.join(x.strip() for x in n if x and x.strip())
            # 节点表达式 → 统一取文本，避免输出 "<Element ... at 0x...>" 垃圾串
            texts = [(x.xpath("string(.)") or "").strip() for x in n]
            texts = [t for t in texts if t]
            if texts:
                return joiner.join(texts)
        return ""

    title = first_xpath(['//div[@class="wx-tit"]/h1/text()', '//h1/text()'])

    authors = []
    for a in tree.xpath('//h3[@class="author" and @id="authorpart"]//a'):
        for sup in a.xpath(".//sup"):
            sup.getparent().remove(sup)
        name = clean(a.xpath("string(.)"))
        if name:
            authors.append(name)

    orgs = []
    for a in tree.xpath('//div[@class="wx-tit"]/h3[@class="author"][2]//a'):
        txt = clean(a.xpath("string(.)"))
        txt = txt.lstrip("0123456789. \t\r\n")
        txt = txt.lstrip(":：)(")
        orgs.append(txt.strip())

    abstract = first_xpath([
        '//input[@id="abstract_text"]/@value',
        '//span[@class="abstract-text"]/text()',
        '//span[contains(@class,"abstract-text")]/text()',
    ])

    kws_raw = tree.xpath('//p[@class="keywords"]/a/text()')
    keywords = [clean(k).rstrip(";；") for k in kws_raw if clean(k)]

    funds_raw = tree.xpath('//p[@class="funds"]/span//a/text() | //p[@class="funds"]/span/text()')
    funds = [clean(f) for f in funds_raw if clean(f)]

    def get_row(label):
        n = tree.xpath(
            f'//span[contains(@class,"rowtit")][contains(text(),"{label}")]/following-sibling::p[1]'
        )
        if n:
            return clean(n[0].xpath("string(.)"))
        n = tree.xpath(
            f'//span[contains(@class,"rowtit")][contains(text(),"{label}")]/following-sibling::*[1]'
        )
        if n and n[0].tag == "p":
            return clean(n[0].xpath("string(.)"))
        return ""

    source = first_xpath([
        '//a[contains(@href,"navi.cnki.net") and not(contains(@href,"keyword")) and not(contains(@href,"author"))]/text()'
    ])

    return {
        "title": title,
        "authors": authors,
        "orgs": orgs,
        "source": source,
        "abstract": abstract,
        "keywords": keywords,
        "funds": funds,
        "doi": get_row("DOI"),
        "album": get_row("专辑"),
        "topic": get_row("专题"),
        "clc_code": get_row("分类号"),
        "publish_time": get_row("在线公开时间"),
        "url": detail_url,
    }


# ========================== 保存结果 ==========================
# CSV 固定列顺序（与 JSON 键一致）；用固定列表而非取第一条的键，避免个别缺键记录导致丢列
CSV_FIELDS = [
    "title", "authors", "orgs", "source", "abstract", "keywords", "funds",
    "doi", "album", "topic", "clc_code", "publish_time", "url",
]


def save_results(results: list, output: str):
    if not output:
        prefix = CONFIG["paths"]["default_output_prefix"]
        output = f"{prefix}_{int(time.time())}.json"
    if output.endswith(".csv"):
        flat = [{
            "title": r.get("title", ""),
            "authors": " | ".join(r.get("authors", []) or []),
            "orgs": " | ".join(r.get("orgs", []) or []),
            "source": r.get("source", ""),
            "abstract": r.get("abstract", ""),
            "keywords": " | ".join(r.get("keywords", []) or []),
            "funds": " | ".join(r.get("funds", []) or []),
            "doi": r.get("doi", ""),
            "album": r.get("album", ""),
            "topic": r.get("topic", ""),
            "clc_code": r.get("clc_code", ""),
            "publish_time": r.get("publish_time", ""),
            "url": r.get("url", ""),
        } for r in results]
        with open(output, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
            writer.writeheader()
            writer.writerows(flat)
    else:
        with open(output, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"[保存] 已写入 {output} ({len(results)} 条)")


def save_failed(failed_items: list, path: str):
    """保存失败清单（供 --retry-failed 补抓），空清单不写盘"""
    if not failed_items:
        return
    with open(path, "w", encoding="utf-8") as f:
        json.dump(failed_items, f, ensure_ascii=False, indent=2)
    print(f"[保存] 失败清单 {len(failed_items)} 条已写入 {path}")


def load_existing_results(output: str) -> tuple:
    """读取已有输出文件（--resume 用），返回 (结果列表, 已抓 URL 集合)"""
    if not output or not os.path.exists(output):
        return [], set()
    try:
        if output.endswith(".csv"):
            with open(output, encoding="utf-8-sig", newline="") as f:
                rows = [dict(r) for r in csv.DictReader(f)]
        else:
            with open(output, encoding="utf-8") as f:
                rows = json.load(f)
        urls = {r.get("url", "") for r in rows if r.get("url")}
        return list(rows), urls
    except Exception as e:
        print(f"[警告] 读取已有输出 {output} 失败: {e}，忽略 --resume")
        return [], set()


def cmd_retry_failed(failed_path: str, output: str) -> bool:
    """读取失败清单，逐条重抓摘要：成功则保存并移出清单，失败保留"""
    try:
        with open(failed_path, encoding="utf-8") as f:
            failed = json.load(f)
    except Exception as e:
        print(f"[错误] 读取失败清单 {failed_path} 失败: {e}")
        return False
    if not failed:
        print("[重试] 失败清单为空，无需补抓")
        return False

    remaining = []
    results = []
    delay = CONFIG["runtime"]["delay_seconds"]
    for item in failed:
        url = item.get("url") if isinstance(item, dict) else str(item)
        if not url:
            remaining.append(item)
            continue
        try:
            d = fetch_abstract(url)
            results.append(d)
            print(f"[重试] 成功 {url[:70]}")
        except Exception as e:
            if isinstance(item, dict):
                item["error"] = str(e)
            else:
                item = {"url": str(item), "error": str(e)}
            remaining.append(item)
            print(f"[重试] 仍失败 {url[:70]}: {e}")
        sleep_jitter(delay)

    save_results(results, output)
    save_failed(remaining, failed_path)
    print(f"[重试] 本轮成功 {len(results)} 条，剩余失败 {len(remaining)} 条")
    return True


# ========================== CLI 工具 ==========================
def parse_extra(s: str) -> list:
    items = []
    if not s:
        return items
    for part in s.split("|"):
        if ":" not in part:
            continue
        k, v = part.split(":", 1)
        items.append({"field": k.strip(), "value": v.strip()})
    return items


def parse_keywords_file(path: str) -> list:
    items = []
    # utf-8-sig：自动去除记事本保存时写入的 UTF-8 BOM（\ufeff），避免首行关键词检索为空
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("|")
            # 支持专业检索行：EXPERT|检索式
            if parts[0].strip().upper() in ("EXPERT", "PRO"):
                if len(parts) < 2:
                    continue
                items.append(("EXPERT", parts[1].strip(), ""))
                continue
            kw = parts[0]
            field = parts[1] if len(parts) > 1 else CONFIG["search"]["default_field"]
            operator = parts[2] if len(parts) > 2 else CONFIG["search"]["default_operator"]
            items.append((kw, field, operator))
    return items


class ProgressBar:
    def __init__(self, total: int, title: str = "进度", width: int = None):
        self.total = total
        self.current = 0
        self.title = title
        self.width = width or CONFIG["runtime"]["progress_bar_width"]
        self.success = 0
        self.fail = 0
        self.start_time = time.time()

    def update(self, success: bool = True):
        self.current += 1
        if success:
            self.success += 1
        else:
            self.fail += 1
        self._render()

    def _render(self):
        ratio = min(self.current / max(self.total, 1), 1.0)
        filled = int(self.width * ratio)
        bar = "#" * filled + "-" * (self.width - filled)
        elapsed = time.time() - self.start_time
        speed = self.current / elapsed if elapsed > 0 else 0
        eta = (self.total - self.current) / speed if speed > 0 else 0
        sys.stdout.write(
            f"\r{self.title} |{bar}| {self.current}/{self.total} "
            f"({ratio*100:.1f}%) 成功:{self.success} 失败:{self.fail} "
            f"速度:{speed:.1f}/s ETA:{eta:.0f}s   "
        )
        sys.stdout.flush()

    def finish(self):
        sys.stdout.write("\n")
        sys.stdout.flush()


# ========================== setup-cookies 命令 ==========================
def cmd_setup_cookies():
    """交互式重新录入 cookie"""
    print("=" * 60)
    print(" 知网 Cookie 录入向导")
    print("=" * 60)
    print("步骤：")
    print("  1. 浏览器打开 https://kns.cnki.net/kns8s/AdvSearch")
    print("  2. F12 → Network → 触发一次搜索")
    print("  3. 找到任意请求 → 右键 Copy → Copy as cURL (bash)")
    print("  4. 从 cURL 的 -b '...' 里复制整段 cookie 字符串")
    print("  5. 粘贴到下方（直接回车结束）")
    print()
    raw = input("请粘贴 cookie 字符串 > ").strip()
    if not raw:
        print("[错误] 未输入任何内容")
        return False

    new_cookies = {}
    for part in raw.split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        k = k.strip()
        v = v.strip()
        # URL decode value
        from urllib.parse import unquote
        v = unquote(v)
        new_cookies[k] = v

    # 必须有这几个核心 cookie
    required = ["KNS2COOKIE", "Ecp_ClientId"]
    missing = [r for r in required if r not in new_cookies]
    if missing:
        print(f"[错误] 缺少必需的 cookie: {missing}")
        print(f"[提示] 你粘贴的内容里有这些 key: {list(new_cookies.keys())}")
        return False

    print(f"[成功] 解析到 {len(new_cookies)} 个 cookie：")
    for k in new_cookies:
        print(f"  - {k}")

    path = CONFIG["paths"]["cookies_file"]
    save_cookies(new_cookies, path)
    print(f"\n✅ 已保存到 {path}，下次运行自动加载")
    return True


# ========================== cookie 有效性检查 ==========================
# 知网拦截页强特征（登录页 / 安全验证页）。判定必须配合页面尺寸，避免误伤正常页面
# （正常详情页/列表页通常 >100KB，拦截页仅几 KB）
LOGIN_MARKERS = ("欢迎登录", "kns8s/login", "user/login", "cas/login")
SECURITY_MARKERS = ("安全验证", "/verify/", "captchaId=")


def classify_block_page(text: str) -> str:
    """
    识别知网拦截页类型，返回 "login"（登录页）/ "security"（安全验证/风控页）/ ""（正常）。
    依据：页面尺寸很小（<100KB）且头部含对应强特征，避免误伤正常大页面。
    """
    if not text or len(text) > 100000:
        return ""
    head = text[:5000]
    if any(m in head for m in SECURITY_MARKERS):
        return "security"
    if any(m in head for m in LOGIN_MARKERS):
        return "login"
    return ""


def check_cookies() -> bool:
    """
    启动时校验 cookie（跨设备使用，每次运行前都应确认本机 cookie 有效）：
      1) 必需字段齐全：KNS2COOKIE（会话令牌）+ Ecp_ClientId（签名客户端 ID）
      2) 一次轻量 GET 高级检索页探测真实可用性（不触发搜索/验证码、不扣题分）
    失效则给出明确提示并返回 False（调用方应退出）。
    """
    required = ["KNS2COOKIE", "Ecp_ClientId"]
    missing = [k for k in required if k not in COOKIES]
    if missing:
        print("[错误] cookies.json 缺少必需 cookie: " + ", ".join(missing))
        print("[提示] 请刷新 cookie（运行 --setup-cookies 或把 cURL 里的 cookie 写入 cookies.json）")
        return False

    try:
        resp = session.get(CONFIG["endpoints"]["adv_search"], timeout=CONFIG["http"]["timeout"])
        final_url = resp.url or ""
        if "login" in final_url.lower():
            print("[错误] cookie 已失效：访问高级检索页被重定向到登录")
            print("[提示] 请用最新 cookie 刷新 cookies.json")
            return False
        block = classify_block_page(resp.text)
        if block == "login":
            print("[错误] cookie 已失效：高级检索页返回登录页")
            print("[提示] 请用最新 cookie 刷新 cookies.json")
            return False
        if block == "security":
            print("[警告] 访问高级检索页触发安全验证（风控），稍后重试或降低频率")
            return True
        if "AdvSearch" in resp.text[:2000] or "高级检索" in resp.text[:2000] or "检索" in resp.text[:2000]:
            print("[cookies] 校验通过，会话有效")
            return True
        print("[警告] cookie 状态无法确认（页面特征异常），将继续运行尝试")
        return True
    except Exception as e:
        print(f"[警告] cookie 探测失败（网络问题?）: {e}，将继续运行尝试")
        return True


# ========================== 主流程（保留 CLI，嵌入后由 adapter 调用 API）==========================
def main():
    parser = argparse.ArgumentParser(description="中国知网列表/摘要爬虫（参数全部外置）")
    parser.add_argument("--keyword", "-k", default="", help="检索关键词（--keyword / --expert / --keywords-file 三者必填其一）")
    parser.add_argument("--expert", "-e", default="", help="专业检索式，如 SU=('卡车'+'车辆')*'无人机'*'协同'（优先于 --keyword）")
    parser.add_argument("--field", "-f", help=f"SU=主题 TI=题名 KY=关键词 AU=作者（默认 {CONFIG['search']['default_field']}）")
    parser.add_argument("--operator", "-op", help=f"TOPRANK=模糊 EQ=精确（默认 {CONFIG['search']['default_operator']}）")
    parser.add_argument("--resource", "-r", help=f"CAPJ=期刊 CAPM=博硕 等（默认 {CONFIG['search']['default_resource']}）")
    parser.add_argument("--extra", default="", help="附加 AND 条件，格式 TI:VRP|AU:张三")
    parser.add_argument("--keywords-file", default="", help="批量关键词文件（每行一个，支持 keyword|FIELD|OPERATOR）")
    parser.add_argument("--max", "-m", type=int, help=f"每个关键词最多抓多少条（默认 {CONFIG['search']['max_per_keyword']}）")
    parser.add_argument("--page-size", type=int, help=f"每页大小（默认 {CONFIG['search']['page_size']}）")
    parser.add_argument("--output", "-o", default="", help="输出文件 (.json / .csv)")
    parser.add_argument("--resume", action="store_true", help="断点续传：读取已有输出文件，跳过已抓详情页")
    parser.add_argument("--retry-failed", default="", metavar="FAILED_FILE",
                        help="补抓失败清单（默认 config paths.failed_file 即 failed.json）")
    parser.add_argument("--force-captcha", action="store_true", help="强制走滑块验证流程")
    parser.add_argument("--delay", type=float, help=f"请求间隔秒数（默认 {CONFIG['runtime']['delay_seconds']}）")
    parser.add_argument("--setup-cookies", action="store_true", help="重新录入 cookie（退出爬虫流程）")
    args = parser.parse_args()

    # setup-cookies 模式
    if args.setup_cookies:
        sys.exit(0 if cmd_setup_cookies() else 1)

    # 跨设备使用：每次启动都校验 cookie（必需字段 + 真实可用性探测）
    if not check_cookies():
        sys.exit(3)

    # 补抓模式：只重抓失败清单里的详情页，不重新检索
    if args.retry_failed:
        sys.exit(0 if cmd_retry_failed(args.retry_failed, args.output) else 1)

    # 命令行覆盖配置（仅本次运行生效）
    kw_default = (args.keyword or "").strip()
    field_default = args.field or CONFIG["search"]["default_field"]
    operator_default = args.operator or CONFIG["search"]["default_operator"]
    resource_default = args.resource or CONFIG["search"]["default_resource"]
    max_default = args.max if args.max is not None else CONFIG["search"]["max_per_keyword"]
    delay_default = args.delay if args.delay is not None else CONFIG["runtime"]["delay_seconds"]

    # 检索条件来源：--expert > --keywords-file > --keyword，均未提供则报错
    if args.expert:
        keyword_list = [("EXPERT", args.expert, "")]
    elif args.keywords_file:
        if not os.path.exists(args.keywords_file):
            print(f"[错误] 批量关键词文件不存在: {args.keywords_file}")
            sys.exit(2)
        keyword_list = parse_keywords_file(args.keywords_file)
        if not keyword_list:
            print(f"[错误] 批量关键词文件为空: {args.keywords_file}")
            sys.exit(2)
        print(f"[批量] 共读取 {len(keyword_list)} 个检索条件")
    elif kw_default:
        keyword_list = [(kw_default, field_default, operator_default)]
    else:
        print("[错误] 未提供任何检索条件！请使用以下任一方式指定：")
        print("  python -m automation.cnki.crawler --keyword 关键词   # 高级检索（默认 SU 主题）")
        print("  python -m automation.cnki.crawler --expert \"检索式\"  # 专业检索")
        print("  python -m automation.cnki.crawler --keywords-file keywords.txt")
        sys.exit(2)

    extra = parse_extra(args.extra)
    all_results = []
    seen_urls = set()
    dup_count = 0
    failed_items = []
    failed_path = args.retry_failed or CONFIG["paths"].get("failed_file", "failed.json")
    overall_start = time.time()

    # 断点续传：加载已有输出文件，跳过已抓详情页
    if args.resume:
        prev_results, prev_urls = load_existing_results(args.output)
        all_results = list(prev_results)
        seen_urls = set(prev_urls)
        if prev_results:
            print(f"[续传] 已从 {args.output} 载入 {len(prev_results)} 条，跳过已抓 URL")

    for idx, (kw, field, operator) in enumerate(keyword_list, 1):
        if kw.upper() == "EXPERT":
            print(f"\n{'=' * 70}\n[{idx}/{len(keyword_list)}] 专业检索: {field}\n{'=' * 70}")
            query_json = build_expert_query(
                expert_str=field,
                resource=resource_default,
            )
        else:
            print(f"\n{'=' * 70}\n[{idx}/{len(keyword_list)}] 检索: {kw}  字段:{field}  算符:{operator}\n{'=' * 70}")
            query_json = build_query(
                keyword=kw, field=field, operator=operator,
                resource=resource_default, extra=extra,
            )

        items = fetch_all_list(
            query_json=query_json,
            max_count=max_default,
            force_captcha=args.force_captcha,
            delay=delay_default,
        )
        new_items = [it for it in items if it["url"] not in seen_urls]
        dup_in_page = len(items) - len(new_items)
        dup_count += dup_in_page
        print(f"[列表] 关键词 '{kw}' 共 {len(items)} 条，去重 {dup_in_page} 条，待处理 {len(new_items)} 条")

        if not new_items:
            continue

        for it in new_items:
            seen_urls.add(it["url"])

        progress = ProgressBar(total=len(new_items), title=f"[{kw[:15]:<15}] 摘要进度")
        for it in new_items:
            try:
                d = fetch_abstract(it["url"])
                all_results.append(d)
                progress.update(success=True)
            except Exception as e:
                print(f"\n[摘要] 失败 {it.get('url','')[:60]}: {e}")
                failed_items.append({"url": it["url"], "error": str(e), "keyword": kw})
                progress.update(success=False)
            sleep_jitter(delay_default)
        progress.finish()

        # 每个关键词完成后增量写盘，中断/异常时不丢已抓数据
        try:
            save_results(all_results, args.output)
            save_failed(failed_items, failed_path)
        except Exception as e:
            print(f"[警告] 保存结果失败: {e}")

    # 全部结束后兜底保存
    try:
        save_results(all_results, args.output)
        save_failed(failed_items, failed_path)
    except Exception as e:
        print(f"[警告] 最终保存失败: {e}")

    elapsed_total = time.time() - overall_start
    avg_speed = len(all_results) / elapsed_total if elapsed_total > 0 else 0
    print(f"\n{'=' * 70}")
    print(f"=== 全部完成，共 {len(all_results)} 条 ===")
    if dup_count > 0:
        print(f"=== 跨关键词去重跳过 {dup_count} 条重复 ===")
    print(f"=== 总耗时 {elapsed_total:.1f}s，平均 {avg_speed:.2f} 条/s ===")
    print(f"{'=' * 70}")


# 模块导入时自动初始化（保存配置后调用 init() 可热重载）
init()

if __name__ == "__main__":
    main()
