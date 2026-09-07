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
# 产品级改造子系统：分层重试/断路器/告警(resilience)、指纹(fingerprint)、
# 代理池(proxy_pool)、动态并发池(scheduler)、任务监控(monitor)
from . import fingerprint as _fingerprint
from . import monitor as _monitor
from . import proxy_pool as _proxy_pool
from . import scheduler as _scheduler
from .resilience import (
    AlertManager,
    CircuitBreaker,
    CircuitOpenError,
    ErrorClass,
    RetryPolicy,
    classify_exception,
    get_alert_manager,
    get_breaker,
    register_exception_classifier,
    resilient_call,
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
        # 浏览器指纹随机化（会话级）：关闭则全程用上面的固定指纹
        "fingerprint": {"enabled": True, "pool": []},
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
        # 产品级:2 秒基础请求间隔,背靠背快请求是触发知网限流的典型诱因
        "delay_seconds": 2.0,
        "retry_times": 3,
        # GB/T 导出 API 单篇重试次数(指数退避 2/4/8s),耗尽才抛 APIFailed
        "gbt_api_retries": 4,
        "progress_bar_width": 30,
        # 动态并发池边界(adapter 抓详情用;池在区间内按负载自适应)
        "min_workers": 1,
        "max_workers": 6,
    },
    # ---------- 代理 IP 池（反爬应对）----------
    # mode: off=直连(默认,行为与旧版一致) / on=全走代理 / failover=直连失败自动切代理
    # 池来源:本配置 pool 列表 + 环境变量 CNKI_PROXY_LIST(逗号分隔),自动去重合并
    "proxy": {
        "mode": "off",
        "pool": [],
        "check_url": "https://kns.cnki.net/kns8s/AdvSearch",
        "check_timeout": 8.0,
        "check_interval": 300.0,
        "cooldown": 600.0,
        "score_threshold": 0.0,
    },
    # ---------- 稳定性（分层重试 + 断路器 + 告警）----------
    "resilience": {
        # HTTP 层断路器:连续失败达阈值熔断,冷却后半开探测
        "breaker": {
            "failure_threshold": 5,
            "recovery_timeout": 60.0,
        },
        # 分层重试:TRANSIENT 短退避快速重试;RATE_LIMITED 长退避跨限流窗口;
        # BLOCKED/FATAL 不重试(语义上重试无意义,交上层处置)
        "retry": {
            "transient_retries": 3,
            "rate_limited_retries": 2,
            "transient_backoff": [1.0, 2.0, 4.0],
            "rate_limited_backoff": [15.0, 45.0],
            "jitter": 0.2,
        },
        # 告警推送:CNKI_ALERT_WEBHOOK_URL 环境变量指向接收端(企业微信/钉钉/自建);
        # cooldown 秒内同类告警节流合并(count 累加)
        "alert_cooldown": 300.0,
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
            elif isinstance(default_val, (list, tuple)):
                # v9.6:列表按 JSON 或逗号分隔解析——此前整串塞入,
                # CNKI_PROXY_POOL="http://a,http://b" 会按字符逐个入池
                try:
                    parsed = json.loads(val)
                    ok = isinstance(parsed, list)
                except ValueError:
                    ok = False
                if ok:
                    merged[section][key] = [str(x) for x in parsed]
                else:
                    items = [x.strip() for x in val.split(",") if x.strip()]
                    if items:
                        merged[section][key] = items
                    else:
                        print(f"[警告] {env_name}={val!r} 无法解析为列表,已忽略")
                        merged[section][key] = default_val
            elif isinstance(default_val, dict):
                # v9.6:字典按 JSON 解析——此前整串塞入(如 CNKI_HTTP_FINGERPRINT)
                # 会让 crawler.init() 在导入期拿到字符串而崩溃
                try:
                    parsed = json.loads(val)
                    if not isinstance(parsed, dict):
                        raise ValueError("not a JSON object")
                    merged[section][key] = parsed
                except ValueError as e:
                    print(f"[警告] {env_name}={val!r} 无法解析为 JSON 对象({e}),已忽略")
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
    # 自适应限速基准跟随配置(热重载时同步重置)
    throttle_init(CONFIG["runtime"]["delay_seconds"])

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

    # 翻页 token：会话绑定。优先读上次运行落盘的新鲜值(data/turnpage.txt)，
    # 其次 config.yaml 兜底；运行中经 refresh_turnpage 刷新并回写落盘。
    # 教训：只靠 config 写死值，进程重启后旧令牌失效 → 知网报"查询对象结构错误"
    _turnpage = _load_turnpage()

    # ---------- 产品级子系统（零网络副作用，测试导入安全）----------
    # 1) 浏览器指纹：会话级随机挑选（UA↔sec-ch-ua 严格配对），全程一致
    fp = _fingerprint.init_fingerprint(CONFIG["http"])
    fp.apply_to(s)  # 覆盖上面按 config 写死的同名字头，其余头保留
    print(f"[指纹] 本次会话: {fp.name} ({fp.user_agent[:60]}…)")

    # 2) 代理池：mode=off 时不启动巡检线程，行为与旧版直连一致
    _proxy_pool.init_proxy_pool(CONFIG.get("proxy") or {})

    # 3) 动态并发池：边界来自 runtime 段，运行中按负载自适应
    _scheduler.configure_pool(CONFIG["runtime"])

    # 4) 告警中心：冷却期来自 resilience 段
    _alert_manager = get_alert_manager()
    _alert_manager.cooldown = float(
        CONFIG.get("resilience", {}).get("alert_cooldown", 300.0)
    )

    # 5) 知网业务异常 → 错误层级映射（仅注册一次；幂等）
    register_exception_classifier(_classify_cnki_exception)


def _classify_cnki_exception(exc: BaseException):
    """知网业务异常 → resilience 错误层级（分类钩子，返回 None 交内置规则）。

    - ServerBusy 空壳      → RATE_LIMITED（限流窗口分钟级，长退避才有意义）
    - Cookie 失效          → BLOCKED（重试无意义：换 cookie/代理是上层的事）
    - 超级鹰余额不足/改版  → FATAL（人工介入，重试纯烧钱/烧时间）
    - GB/T 单篇缺失        → FATAL（结构问题，重试不会变好）
    - GB/T API 故障        → RATE_LIMITED（多为限流/抖动）
    """
    if isinstance(exc, CnkiServerBusyError):
        return ErrorClass.RATE_LIMITED
    if isinstance(exc, CnkiCookieError):
        return ErrorClass.BLOCKED
    if isinstance(exc, (CnkiCaptchaBalanceError, CnkiRevisionError, CnkiGBTCitationMissing)):
        return ErrorClass.FATAL
    if isinstance(exc, CnkiGBTCitationAPIFailed):
        return ErrorClass.RATE_LIMITED
    return None


def extract_turnpage(html: str) -> str:
    """从页面 JS 中提取 turnpage token（形如 turnpage='xxx'），找不到返回空串"""
    m = re.search(r"turnpage\s*=\s*['\"]([^'\"]+)['\"]", html or "")
    return m.group(1) if m else ""


def _turnpage_file():
    """turnpage 落盘路径：data/turnpage.txt（运行期间提取的新鲜令牌）"""
    return _DATA_DIR / "turnpage.txt"


def _load_turnpage() -> str:
    """turnpage 加载：data/turnpage.txt（上次运行提取的新鲜值）优先，config.yaml 兜底"""
    try:
        val = _turnpage_file().read_text("utf-8").strip()
        if val:
            return val
    except OSError:
        pass
    return CONFIG["http"].get("turnpage", "")


def refresh_turnpage(reason: str = "") -> bool:
    """GET 高级检索页重新提取 turnpage 会话令牌并落盘。

    turnpage 是会话绑定令牌：进程重启后模块变量回退到 config.yaml 写死的旧值，
    旧值失效时知网返回 value="查询对象结构错误！"（会被误判为限流空壳白等退避）。
    提取成功 → 更新模块变量 + 落盘 data/turnpage.txt，下次 init() 直接可用。
    """
    global _turnpage
    try:
        resp = session.get(CONFIG["endpoints"]["adv_search"], timeout=CONFIG["http"]["timeout"])
        extracted = extract_turnpage(resp.text)
    except Exception as e:
        print(f"[警告] turnpage 刷新失败({reason}): {e}")
        return False
    if not extracted:
        print(f"[警告] turnpage 刷新失败({reason}): 高级检索页未提取到令牌（页面可能改版）")
        return False
    if extracted != _turnpage:
        print(f"[turnpage] 令牌已刷新({reason})")
        debug_log(f"[turnpage] 令牌已刷新({reason})")
    _turnpage = extracted
    try:
        _turnpage_file().write_text(extracted, "utf-8")
    except OSError:
        pass
    return True


def sleep_jitter(base_seconds: float):
    """带随机抖动的休眠（±20%），降低请求节奏的机器特征，缓解风控"""
    time.sleep(max(base_seconds * random.uniform(0.8, 1.2), 0))


# ========================== 自适应限速 ==========================
# 风控信号(验证码/限流空壳/安全验证)→ 间隔倍增;连续成功 → 逐步回落到基准。
# 目标:请求节奏跟随知网风控强度自适应,而不是拿固定 delay 硬撞限流窗口。
#
# v9.4 预防性节拍冷却:知网限流风控基于滑动窗口请求频率累积,连续高频翻页
# (生产实测约第 10 页)即触发"请稍后重试"空壳。每连续完成 N 页主动长歇一次,
# 在限流窗口成型前打断累积 —— 预防优于事后退避(退避再好也是已中招)。
_PACE_COOLDOWN_EVERY_PAGES = 8
_PACE_COOLDOWN_SECONDS = 40.0
_throttle_lock = threading.Lock()
_throttle = {
    "base": 2.0,        # 基准间隔(config runtime.delay_seconds)
    "current": 2.0,     # 当前生效间隔
    "good_streak": 0,   # 连续成功计数(用于回落)
    "max_delay": 30.0,  # 间隔上限,避免限速失控拖死任务
}


def throttle_init(base: float) -> None:
    """按配置基准初始化限速状态(init 与 main(--delay 覆盖)时调用)。"""
    with _throttle_lock:
        _throttle["base"] = max(float(base), 0.5)
        _throttle["current"] = _throttle["base"]
        _throttle["good_streak"] = 0


def throttle_hit(reason: str = "", heavy: bool = False) -> None:
    """风控信号:生效间隔倍增(常规信号 ×1.8;分钟级限流空壳 heavy ×2.5),封顶 30s。

    heavy 语义:知网限流窗口为分钟级(见 SERVER_BUSY_BACKOFF_SECONDS 注),
    常规 ×1.8 的抬升追不上窗口成型速度,限流空壳必须更重地拉开间隔。
    线程安全:adapter 以 3 线程并发抓详情页,共享同一份限速状态。
    产品级联动:同一步风控信号同步喂给动态并发池(立即收缩并发)与
    任务计数器(面板展示验证码/风控命中次数),三套自愈机制协同。
    """
    with _throttle_lock:
        old = _throttle["current"]
        _throttle["current"] = min(old * (2.5 if heavy else 1.8), _throttle["max_delay"])
        _throttle["good_streak"] = 0
        grew = _throttle["current"] > old * 1.01
    if grew:
        debug_log(f"[限速] {reason},请求间隔 {old:.1f}s → {_throttle['current']:.1f}s")
    # 风控信号外送（失败静默，不影响主流程）
    try:
        pool = _scheduler.get_pool()
        if pool is not None:
            pool.record_risk_signal()
    except Exception:
        pass
    _task_incr("captcha_hits")


def throttle_ok() -> None:
    """单篇/单页抓取成功:连续 8 次后间隔 ×0.9 缓慢回落到基准。

    v9.4:5 次 ×0.85 → 8 次 ×0.9 —— 限流窗口为分钟级,回落过快会在
    窗口尚未滑出时重新加速,再次撞窗出现空壳;保守回落优先压低复发率。
    """
    with _throttle_lock:
        _throttle["good_streak"] += 1
        if _throttle["good_streak"] < 8 or _throttle["current"] <= _throttle["base"]:
            return
        old = _throttle["current"]
        _throttle["current"] = max(_throttle["base"], old * 0.9)
    debug_log(f"[限速] 连续成功,请求间隔 {old:.1f}s → {_throttle['current']:.1f}s")


def effective_delay() -> float:
    """当前生效的请求间隔(所有请求间 sleep 应使用该值而非固定配置)。"""
    with _throttle_lock:
        return _throttle["current"]


def throttle_base() -> float:
    """基准请求间隔(最终报告用它判断本次风控强度)。"""
    with _throttle_lock:
        return _throttle["base"]


# v9.2:知网 2026-08-31 起对 pageSize 做参数校验(只认 10/20/50 白名单),
# 余量收缩产生的 pageSize=5 等值会被拒:"参数校验;字段【pageSize】校验失败"空壳,
# 且空壳文案与限流文案重叠,曾导致误判限流白烧退避 → 0 篇入库。
# 修复:向上对齐到白名单最小可用值(多抓的部分由 fetch_all_list 末尾截断兜底)。
_PAGE_SIZE_WHITELIST = (10, 20, 50)


def _normalize_page_size(size: int) -> int:
    """把任意请求页大小对齐到知网 pageSize 白名单(10/20/50)。"""
    if size in _PAGE_SIZE_WHITELIST:
        return size
    for s in _PAGE_SIZE_WHITELIST:
        if s >= size:
            return s
    return _PAGE_SIZE_WHITELIST[-1]


# ========================== 过程日志回调（嵌入后用于实时推送前端）==========================
# 线程本地：CLI 主线程与后端 executor 线程互不干扰；并发跑多个任务也不会串日志
_log_local = threading.local()


def set_log_callback(cb):
    """设置当前线程的过程日志回调（后端把它转发到 SSE）；传 None 恢复纯 stdout 输出。"""
    _log_local.callback = cb


def emit_log(msg: str):
    """过程日志：有回调则转发给调用方，同时始终打印到 stdout（CLI 兼容）。

    产品级增强：当前线程绑定了任务 ID（见 set_current_task）时，同步写入
    monitor 注册表的每任务环形日志——全链路日志监控的数据源之一。
    """
    cb = getattr(_log_local, "callback", None)
    if cb:
        try:
            cb(msg)
        except Exception:
            pass
    task_id = getattr(_log_local, "task_id", None)
    if task_id:
        try:
            _monitor.get_registry().append_log(task_id, msg)
        except Exception:
            pass
    print(msg)


def debug_log(msg: str):
    """内部诊断日志（v9.5 产品级分级）：只落 stdout，不进 SSE/环形日志。

    判据：含实现机制细节（限速数值、令牌、签名、坐标、dump 文件名、
    异常类名等）的消息对终端用户无行动价值，一律走本函数——
    用户面板只见产品级文案，排障细节留在服务器控制台。
    """
    print(msg)


# ---- 当前线程绑定的任务 ID（adapter 在任务线程入口设置，日志/计数归账用）----
def set_current_task(task_id: str | None) -> None:
    """绑定/解绑当前线程的任务 ID（任务线程入口调用；结束时传 None）。"""
    _log_local.task_id = task_id


def current_task_id() -> str | None:
    """当前线程绑定的任务 ID（未绑定为 None）。"""
    return getattr(_log_local, "task_id", None)


def _task_incr(counter: str, n: int = 1) -> None:
    """向当前线程的任务记账一条计数（未绑定任务则忽略）。"""
    task_id = current_task_id()
    if task_id:
        try:
            _monitor.get_registry().incr(task_id, counter, n)
        except Exception:
            pass


# ========================== 统一请求入口（断路器 + 分层重试 + 代理 failover）==========================
def _build_retry_policy() -> RetryPolicy:
    """从 CONFIG 组装分层重试策略（init/热重载后生效）。"""
    rc = (CONFIG.get("resilience") or {}).get("retry") or {}
    return RetryPolicy(
        max_retries={
            ErrorClass.TRANSIENT: int(rc.get("transient_retries", 3)),
            ErrorClass.RATE_LIMITED: int(rc.get("rate_limited_retries", 2)),
            ErrorClass.BLOCKED: 0,
            ErrorClass.FATAL: 0,
        },
        backoff_table={
            ErrorClass.TRANSIENT: [float(x) for x in rc.get("transient_backoff", [1.0, 2.0, 4.0])],
            ErrorClass.RATE_LIMITED: [float(x) for x in rc.get("rate_limited_backoff", [15.0, 45.0])],
        },
        jitter=float(rc.get("jitter", 0.2)),
    )


def _pick_proxies(attempt: int) -> dict | None:
    """按代理模式决定本次请求的 proxies 参数。

    - off      永远直连（默认，测试与既有部署零影响）
    - on       每次都走池内最优代理
    - failover 首试(attempt==0)直连；重试改走代理——直连被限流时自动切换出口
    """
    mode = _proxy_pool.proxy_mode(CONFIG.get("proxy") or {})
    if mode == "off":
        return None
    pool = _proxy_pool.get_proxy_pool()
    if pool is None:
        return None
    if mode == "failover" and attempt == 0:
        return None
    url = pool.get_proxy()
    if url:
        _task_incr("proxy_switches")
        return {"http": url, "https": url}
    return None


def safe_request(method: str, url: str, *, headers: dict | None = None,
                 data=None, timeout: float | None = None,
                 raise_for_status: bool = False, retries: bool = True,
                 breaker_name: str = "http", op_name: str = "HTTP"):
    """统一请求入口：断路器 → 分层重试 → 代理 failover → 全链路记账。

    与 adapter 层 urllib3 Retry(3 次瞬断重试)的分工：
    - urllib3 处理毫秒级瞬断（连接池内重试，对外透明）；
    - 本入口处理会话级故障（超时/限流），重试时重建请求并可切换代理出口。

    :param raise_for_status: True 时非 2xx 触发 HTTPError 参与分层
        （429/503→RATE_LIMITED 长退避；其余 5xx→TRANSIENT；4xx→FATAL）；
        False 时状态码交调用方处置（列表/详情页的空壳语义在 HTML 里）
    :param retries: False 时禁用本层重试（仍保留断路器/记账/告警/代理），
        供自带重试封装的调用方（如 GB/T with_retry）避免双重退避叠加
    :raises CircuitOpenError: 断路器熔断中（调用方按请求失败处置）
    :raises requests.RequestException: 分层重试耗尽后原样上抛
    """
    timeout = timeout if timeout is not None else CONFIG["http"]["timeout"]
    breaker_cfg = (CONFIG.get("resilience") or {}).get("breaker") or {}
    breaker = get_breaker(
        breaker_name,
        failure_threshold=int(breaker_cfg.get("failure_threshold", 5)),
        recovery_timeout=float(breaker_cfg.get("recovery_timeout", 60.0)),
    )
    # retries=False → 全层级零重试（失败立即上抛，重试语义交调用方）
    policy = _build_retry_policy() if retries else RetryPolicy(
        max_retries={c: 0 for c in ErrorClass},
    )
    alert = get_alert_manager()

    def _on_retry(error_class: ErrorClass, attempt: int, exc: BaseException, wait: float):
        # 每次重试：任务记账 + 负载记账 + 换代理出口（failover 语义）+ 过程日志
        _task_incr("retries")
        # v9.6:把重试次数同步到 _do.attempt——此前 _bump_attempt 定义了却从未
        # 传入 resilient_call,attempt 恒 0,failover 模式下重试永远直连不切代理
        _do.attempt = attempt
        pool = _scheduler.get_pool()
        if pool is not None:
            pool.record_failure()
        debug_log(
            f"[重试] {op_name} 第{attempt}次失败({error_class.value}: "
            f"{type(exc).__name__})，退避 {wait:.0f}s 后重试"
        )

    def _on_give_up(exc: BaseException, error_class: ErrorClass):
        _task_incr("requests_failed")
        alert.alert(
            title=f"{op_name} 重试耗尽",
            level="critical" if error_class in (ErrorClass.BLOCKED, ErrorClass.FATAL)
            else "warning",
            detail=f"{method.upper()} {url[:120]} → {type(exc).__name__}: {exc}",
        )

    def _do():
        resp = session.request(
            method, url, headers=headers, data=data, timeout=timeout,
            proxies=_pick_proxies(_do.attempt),
        )
        if raise_for_status:
            # 非 2xx 抛 HTTPError → classify_exception 分层:
            # 429/503→RATE_LIMITED(长退避) / 其余 5xx→TRANSIENT / 4xx→FATAL
            resp.raise_for_status()
        return resp

    _do.attempt = 0  # 首试直连(failover 语义),重试时由 _on_retry 递增换代理

    try:
        resp = resilient_call(
            _do, policy=policy, breaker=breaker, sleep_fn=sleep_jitter,
            on_retry=_on_retry, on_give_up=_on_give_up, op_name=op_name,
        )
    except CircuitOpenError as exc:
        # 熔断期间的请求直接失败：记账 + 告警（节流），不向知网发无效流量
        _task_incr("breaker_trips")
        alert.alert(title=f"断路器熔断({breaker_name})", level="critical", detail=str(exc))
        raise
    else:
        # 成功：负载记账（延迟驱动动态并发池）+ 断路器成功计数已在 resilient_call 内
        _task_incr("requests_total")
        pool = _scheduler.get_pool()
        if pool is not None:
            try:
                latency = resp.elapsed.total_seconds()
            except Exception:
                latency = 0.0
            pool.record_success(latency)
        return resp


def http_get(url: str, **kw):
    """GET 的 safe_request 封装（详情页/首页等用）。"""
    return safe_request("GET", url, op_name=kw.pop("op_name", "GET"), **kw)


def http_post(url: str, **kw):
    """POST 的 safe_request 封装（检索/导出接口用）。"""
    return safe_request("POST", url, op_name=kw.pop("op_name", "POST"), **kw)


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
      SU=('主题词A' + '主题词B') * '主题词C' * ('主题词D' + '主题词E')
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
def _cjy_fatal_msg(e) -> str:
    """超级鹰致命错误 → 面向用户的中文指引(前端错误横幅直接展示)。"""
    no = getattr(e, "err_no", None)
    if no in (-1005, -10052):
        return "超级鹰题分不足，自动重试无意义——请登录超级鹰后台充值后重试（任务已停止）"
    if no in (-1001, -1002, -10023):
        return "超级鹰账号/密码错误或软件 ID 配置有误——请检查打码账号配置（任务已停止）"
    if no in (-10071, -10072):
        return "超级鹰识别错误率过高被平台限制——请稍后重试或联系超级鹰客服（任务已停止）"
    if no == -1013:
        return "超级鹰提示本机 IP 受限——请检查超级鹰 IP 白名单设置或更换网络（任务已停止）"
    return f"超级鹰账号异常（{e}）——请登录超级鹰后台检查账户状态（任务已停止）"


def recognize_slider(info):
    back_path = CONFIG["paths"]["captcha_back_image"]
    save_b64_image(info["backImage"], back_path)
    if not os.path.exists(back_path):
        raise RuntimeError("滑块背景图保存失败（backImage 为空？），无法识别")
    with open(back_path, "rb") as f:
        im = f.read()
    # 调度器自动降级：9902(两图形块) → 9900(缺口定位) → 9602(水平拼图)
    # min_points=2：9900 只返回 1 个缺口坐标，不满足则继续降级 9602，避免链条在此中断
    try:
        result = dispatcher.recognize(im, "slider", require="points", min_points=2)
    except CjyFatalError as e:
        # 余额/账号/IP 受限类:重试只会继续失败,映射为闸口异常交前端告知
        raise CnkiCaptchaBalanceError(_cjy_fatal_msg(e)) from e
    pts = result.points
    if len(pts) < 2:
        raise RuntimeError(f"滑块识别结果不足两个坐标: {result.pic_str!r}")
    (x1, y1), (x2, y2) = pts[0], pts[1]
    print(f"[识别] 类型={result.codetype} 中心点 ({x1},{y1}) ({x2},{y2}) 距离={abs(x2 - x1)}")
    debug_log(f"[识别] 滑块验证码识别成功: 中心点 ({x1},{y1}) ({x2},{y2})")
    return x1, y1, x2, y2, result.pic_id


def solve_vericode(html: str) -> str:
    """
    处理知网翻页触发的 vericodeForm（5 位英数验证码）。
    从页面中提取验证码图片 → 超级鹰 1005 识别 → 返回识别文本。
    """
    # 1) 优先取 <img> 的 src（相对/绝对路径），其次 base64 data URI
    # v9.6:etree.HTML("") 返回 None 会 AttributeError,空树兜底走下方 regex 提取
    tree = etree.HTML(html) or etree.Element("html")
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
    try:
        result = dispatcher.recognize(im, "alnum", require="text")
    except CjyFatalError as e:
        # 余额/账号/IP 受限类:重试无意义,映射为闸口异常交前端告知
        raise CnkiCaptchaBalanceError(_cjy_fatal_msg(e)) from e
    text = result.text
    print(f"[识别] 类型={result.codetype} 英数验证码={text!r}")
    debug_log(f"[识别] 英数验证码识别成功: {text!r}")
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
    if not client_id:
        # 签名前置自检:Ecp_ClientId 是签名串的客户端 ID 源,缺失时签名必然被拒,
        # 症状为"每页都解析 0 条"——在此提前点破,避免误判为知网改版/限流
        print("[签名] 警告: session 缺少 Ecp_ClientId,签名头不完整,请求大概率被知网拒绝")
        debug_log("[签名] 警告: session 缺少 Ecp_ClientId(签名客户端 ID),请求大概率被拒")
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
    debug_log(f"[触发] 提交滑块验证码: HTTP {resp.status_code}")

    if "verify/home" not in body:
        print("[触发] 未触发验证码")
        debug_log("[触发] 未触发验证码，直接放行")
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
    if not client_id:
        # 签名前置自检:Ecp_ClientId 是签名串的客户端 ID 源,缺失时签名必然被拒,
        # 症状为"每页都解析 0 条"——在此提前点破,避免误判为知网改版/限流
        print("[签名] 警告: session 缺少 Ecp_ClientId,签名头不完整,请求大概率被知网拒绝")
        debug_log("[签名] 警告: session 缺少 Ecp_ClientId(签名客户端 ID),请求大概率被拒")
    sign = make_signature(CONFIG["endpoints"]["search"], client_id)
    headers = {
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Origin": CONFIG["endpoints"]["base"],
        "Referer": CONFIG["endpoints"]["adv_search"],
        "X-Requested-With": "XMLHttpRequest",
        **sign,
        "ClientID": client_id,
    }
    # 核心列表请求走统一入口：断路器→分层重试→代理 failover→全链路记账
    # (200 空壳不抛异常，仍由下方限流退避逻辑处置——两层各司其职)
    resp = http_post(CONFIG["endpoints"]["search"], data=body_str,
                     headers=headers, timeout=CONFIG["http"]["timeout"])
    html = resp.content.decode("utf-8", errors="ignore")
    # 插桩:限流空壳现场取证 —— 记录 HTTP 状态码/响应长度/页码,
    # 样本落盘(debug_list_*.html,内容 md5 去重,上限 10 份),
    # 便于事后分析知网限流时到底返回了什么(拦截页/错误体/正常壳)
    if "请稍后重试" in html:
        print(f"[诊断] search_grid 空壳现场: HTTP {resp.status_code}, "
              f"长度 {len(html)}, pageNum={page_num}, pageSize={page_size}, boolSearch={bool_search}")
        _dump_debug_list_html(html, f"busy{page_num}")
    return html


def parse_list(html: str):
    # v9.6:etree.HTML("") 返回 None 会 AttributeError,空树兜底返回 0 条
    tree = etree.HTML(html) or etree.Element("html")
    items = []
    for tr in tree.xpath('//tr[td[@class="name"]]'):
        # 1) 标题链接(原来就有的)
        a = tr.xpath(
            './/td[@class="name"]//a[contains(concat(" ", normalize-space(@class), " "), " fz14 ")]'
        )
        if not a:
            continue
        title = a[0].xpath("string(.)").strip()
        href = a[0].get("href", "")
        if href.startswith("/"):
            href = CONFIG["endpoints"]["base"] + href
        # 2) GB/T 7714-2025 引文(同行的 td.quote 单元)
        #    列表页本身就给了,无需再进详情页;格式如: $[1]作者.题名[J].刊名,年,(期):页码.DOI:...
        quote_cells = tr.xpath('.//td[@class="quote"]')
        quote_text = quote_cells[0].xpath("string(.)").strip() if quote_cells else ""
        # 剥掉开头的 $[N] 序号(渲染时不需要)
        if quote_text.startswith("$"):
            quote_text = quote_text.lstrip("$")
            # 去掉前导的 [N] 形式(知网的样式可能是 $[1]xxx 或 $[12] xxx)
            import re as _re_quote
            quote_text = _re_quote.sub(r'^\s*\[\d+\]\s*', '', quote_text).strip()
        items.append({"title": title, "url": href, "quote_text": quote_text})
    return items


def is_captcha_required(html: str) -> bool:
    return (
        "verify/home" in html
        or "captchaId=" in html
        or "vericodeForm" in html
        or "请输入验证码" in html
    )


def _dump_debug_list_html(html: str, page: int) -> None:
    """列表页异常(无数据/解析0条)时落盘 HTML,便于离线分析知网实际返回了什么。

    v7.2:此前只有详情页失败会落盘(debug_abstract_*.html),列表页失败
    无任何现场留存,知网返回拦截页/空壳页时无法事后定位。文件名取
    html 内容 md5 前 8 位,相同内容自然去重;最多保留 10 个防堆积。
    """
    try:
        debug_base = Path(CONFIG["paths"]["debug_abstract_html"])
        tag = md5((html or "").encode("utf-8")).hexdigest()[:8]
        dump_path = debug_base.with_name(f"debug_list_{tag}_p{page}.html")
        if (not dump_path.exists()
                and len(list(debug_base.parent.glob("debug_list_*.html"))) < 10):
            dump_path.write_text(html or "", encoding="utf-8")
            print(f"[调试] 列表页已落盘: {dump_path}")
            debug_log(f"[调试] 列表页已落盘: {dump_path.name}")
    except Exception:
        pass  # 落盘失败不影响主流程


def fetch_all_list(query_json: str, max_count: int | None = None,
                   force_captcha: bool = False) -> list:
    """列表翻页抓取。

    max_count:
      - None  → 翻到知网没有更多结果为止(自然结束);
      - 正整数 → 抓到指定条数即停(防失控)。
    验证码:触发后无限重试直到通过(用户已配超级鹰),不再中途放弃。
    翻页间隔走自适应限速(throttle):风控信号倍增、连续成功回落。
    """
    if max_count is None or max_count <= 0:
        max_count = 10_000_000  # 实际不会触达:知网会在无数据时返回"暂无数据"
    all_items = []
    captcha_verification = ""
    # 关键：boolSearch=true 时服务端必然触发验证码检查（对第2页+）；
    # 一旦验证码通过，后续重搜/翻页必须用 boolSearch=false 才能拿到数据页
    bool_search = "true"
    # v9:cookie 自动续期每条式子只允许一次;续期后再遇登录墙=程序手段已穷尽
    login_fix_attempts = 0
    # v9.1:turnpage 结构错误自愈每条式子只允许一次(刷新后仍被拒=令牌机制疑似改版)
    struct_fix_attempted = False

    page = 1
    while len(all_items) < max_count:
        need = max_count - len(all_items)
        # v9.2:余量向上对齐 pageSize 白名单(10/20/50),避免发出 pageSize=5 触发参数校验空壳
        cur_size = _normalize_page_size(min(CONFIG["search"]["page_size"], need))

        if force_captcha and page == 1:
            captcha_verification = trigger_captcha(query_json)
            bool_search = "false"

        html = search_grid(query_json, page_num=page, page_size=cur_size,
                           captcha_verification=captcha_verification,
                           bool_search=bool_search)
        if is_captcha_required(html):
            # 分流：英数验证码(vericodeForm) 走 1005，滑块验证码(verify/home) 走 9902
            throttle_hit("列表页触发验证码")
            if "vericodeForm" in html or "请输入验证码" in html:
                print(f"[列表] 第{page}页触发英数验证码，走超级鹰 1005 识别...")
                emit_log(f"[列表] 第{page}页检测到安全验证，正在自动处理…")
                # v9:连续 6 次失败熔断 —— 防止余额耗尽/服务异常时无限烧题分
                alnum_fail = 0
                while True:
                    code = ""
                    try:
                        code = solve_vericode(html)
                    except CnkiCaptchaBalanceError:
                        raise  # 余额/账号闸口:立即停止,不烧重试
                    except Exception as e:
                        alnum_fail += 1
                        if alnum_fail >= 6:
                            raise CnkiCaptchaBalanceError(
                                f"英数验证码连续 {alnum_fail} 次识别失败（最后错误: {e}）——请检查超级鹰余额与服务状态"
                            )
                        print(f"[警告] 英数识别失败: {e},重新拉取页面再试({alnum_fail}/6)")
                        emit_log(f"[列表] 第{page}页安全验证处理中，正在自动重试…({alnum_fail}/6)")
                        sleep_jitter(1)
                        html = search_grid(query_json, page_num=page, page_size=cur_size,
                                           captcha_verification=captcha_verification,
                                           bool_search=bool_search)
                        continue
                    if code:
                        break
                    alnum_fail += 1
                    if alnum_fail >= 6:
                        raise CnkiCaptchaBalanceError(
                            f"英数验证码连续 {alnum_fail} 次识别为空——请检查超级鹰余额与服务状态"
                        )
                    print("[警告] 英数识别返回空,重新拉取页面再试")
                    emit_log(f"[列表] 第{page}页安全验证处理中，正在自动重试…")
                    sleep_jitter(1)
                    html = search_grid(query_json, page_num=page, page_size=cur_size,
                                       captcha_verification=captcha_verification,
                                       bool_search=bool_search)
                # 提交到 checkcode 接口,通过后必须用 boolSearch=false 重搜当前页
                if not submit_vericode(code):
                    alnum_fail += 1
                    if alnum_fail >= 6:
                        raise CnkiCaptchaBalanceError(
                            f"英数验证码连续 {alnum_fail} 次校验未通过——请检查超级鹰余额与服务状态"
                        )
                    print(f"[警告] 英数验证码校验未通过,重新识别({alnum_fail}/6)...")
                    emit_log(f"[列表] 第{page}页安全验证处理中，正在自动重试…")
                    continue  # 回到 while True 顶部,重新 solve_vericode
                emit_log(f"[列表] 第{page}页安全验证已通过，继续获取数据")
                bool_search = "false"
                html = search_grid(query_json, page_num=page, page_size=cur_size,
                                   bool_search=bool_search)
            else:
                print(f"[列表] 第{page}页触发滑块验证码，走超级鹰 9902 识别...")
                emit_log(f"[列表] 第{page}页检测到安全验证，正在自动处理…")
                # v9:连续 6 次失败熔断 —— 防止余额耗尽/服务异常时无限烧题分
                slider_fail = 0
                while True:
                    try:
                        captcha_verification = trigger_captcha(query_json)
                    except CnkiCaptchaBalanceError:
                        raise  # 余额/账号闸口:立即停止,不烧重试
                    except Exception as e:
                        slider_fail += 1
                        if slider_fail >= 6:
                            raise CnkiCaptchaBalanceError(
                                f"滑块验证码连续 {slider_fail} 次处理失败（最后错误: {e}）——请检查超级鹰余额与服务状态"
                            )
                        print(f"[警告] 滑块触发失败: {e},稍后重试({slider_fail}/6)")
                        emit_log(f"[警告] 滑块触发失败: {e},稍后重试({slider_fail}/6)")
                        sleep_jitter(1)
                        continue
                    if captcha_verification:
                        break
                    slider_fail += 1
                    if slider_fail >= 6:
                        raise CnkiCaptchaBalanceError(
                            f"滑块验证码连续 {slider_fail} 次未通过——请检查超级鹰余额与服务状态"
                        )
                    print("[警告] 滑块触发返回空,稍后重试")
                    emit_log(f"[列表] 第{page}页安全验证处理中，正在自动重试…")
                    sleep_jitter(1)
                emit_log(f"[列表] 第{page}页安全验证已通过，继续获取数据")
                bool_search = "false"
                html = search_grid(query_json, page_num=page, page_size=cur_size,
                                   captcha_verification=captcha_verification,
                                   bool_search=bool_search)
        preview = html[:300].replace("\n", " ")
        print(f"[列表] 第{page}页 预览: {preview}")
        if "查询对象结构错误" in html:
            # v9.1:value="查询对象结构错误！" —— 知网拒绝请求结构,根因是会话绑定的
            # turnpage 令牌为空/过期(进程重启后模块变量回退 config 写死旧值)。
            # 页面文案含"请稍后重试",若放行到下方空壳分支会被误判为限流:退避重试
            # 必然同样失败(令牌不刷新) → 白等 3 分钟 × 每条式子 → 全部失败 0 篇入库。
            # 处置:刷新令牌后重拉本页(每条式子仅一次);仍被拒 = 令牌机制疑似改版,
            # 上抛 CnkiRevisionError 交前端横幅,勿静默烧完清单。
            _dump_debug_list_html(html, f"struct{page}")
            if struct_fix_attempted:
                raise CnkiRevisionError(
                    "知网报'查询对象结构错误'：刷新 turnpage 令牌后仍被拒绝，检索式或接口结构疑似改版——请反馈开发者更新"
                )
            struct_fix_attempted = True
            print(f"[列表] 第{page}页 知网报'查询对象结构错误':turnpage 令牌疑似失效,刷新后重拉")
            debug_log(f"[列表] 第{page}页 查询对象结构错误,刷新 turnpage 令牌后重拉")
            if not refresh_turnpage("结构错误自愈"):
                raise CnkiRevisionError(
                    "知网报'查询对象结构错误'且 turnpage 令牌刷新失败（高级检索页未提取到令牌）——知网可能改版，请反馈开发者更新"
                )
            html = search_grid(query_json, page_num=page, page_size=cur_size,
                               captcha_verification=captcha_verification,
                               bool_search=bool_search)
            # v9.6:重拉后立即复查——结构错误空壳含「请稍后重试」文案,若放行会被
            # 下方限流分支误判:白等 ~3 分钟退避后误抛 CnkiServerBusyError,烧掉
            # 补漏冷却。刷新后仍被拒 = 令牌机制疑似改版,直接上抛改版错。
            if "查询对象结构错误" in html:
                _dump_debug_list_html(html, page)
                raise CnkiRevisionError(
                    "知网报'查询对象结构错误'：turnpage 令牌刷新后仍被拒绝（接口结构疑似改版）——请反馈开发者更新"
                )
            # 重拉成功:落回下方正常分支流程
        if "起始游标越界" in html:
            # v8.6:空壳 value 属性回显 "start:21,size:20,total:1" —— 请求的起始游标
            # 超过结果总数,即已翻过末页。这是确定性信号而非服务端异常:
            # 重试必然同样失败,保留已抓条目正常结束翻页。
            print(f"[列表] 第{page}页 起始游标越界(已过末页)，停止翻页")
            debug_log(f"[列表] 第{page}页 已过末页(游标越界)，停止翻页")
            break
        if "参数校验" in html:
            # v9.2:知网新接口参数校验空壳(value 属性回显"参数校验;字段【xx】校验失败"),
            # 页面通用文案含"请稍后重试",与限流空壳重叠——但参数错误重试无意义,
            # 前置识别直接抛改版错,避免伪装成限流白烧退避。
            m = re.search(r'value="([^"]*)"', html)
            detail = m.group(1) if m else "未知参数校验失败"
            _dump_debug_list_html(html, page)
            raise CnkiRevisionError(
                f"知网参数校验失败(第{page}页): {detail} —— 接口参数约束变更,请反馈开发者更新"
            )
        if "请稍后重试" in html:
            # v8.5:"抱歉,暂无数据,请稍后重试"是知网服务端异常/限流的空壳响应,
            # 不是真零结果(正常零结果页无"请稍后重试"字样):带退避重试,
            # 仍空壳则抛错交由调用方处置 —— 继续烧完式子只会条条中招。
            throttle_hit("服务端限流空壳", heavy=True)
            # 插桩:空壳现场限速状态 —— 间隔偏小=典型频率风控;已抓条数多=越翻越易中招
            print(f"[诊断] 第{page}页 空壳现场: 当前请求间隔 {effective_delay():.1f}s, "
                  f"已累计 {len(all_items)} 条, 样本见 debug_list_*busy*.html")
            debug_log(f"[列表] 第{page}页 空壳现场诊断: 请求间隔 {effective_delay():.1f}s, 已抓 {len(all_items)} 条")
            for attempt, wait in enumerate(SERVER_BUSY_BACKOFF_SECONDS, start=1):
                print(f"[列表] 第{page}页 服务端异常空壳(请稍后重试),退避 {wait:.1f}s 后重试({attempt}/{SERVER_BUSY_RETRIES})")
                emit_log(f"[列表] 数据源暂时繁忙，正在自动重试(第 {attempt}/{SERVER_BUSY_RETRIES} 次)，已获取的数据不会丢失")
                sleep_jitter(wait)
                html = search_grid(query_json, page_num=page, page_size=cur_size,
                                   captcha_verification=captcha_verification,
                                   bool_search=bool_search)
                if "请稍后重试" not in html:
                    break
            else:
                raise CnkiServerBusyError(
                    f"第{page}页连续 {SERVER_BUSY_RETRIES} 次未返回有效数据(数据源繁忙)"
                )
            # 重拉成功:落回下方正常解析流程(若变成拦截页,由解析 0 条分支分类处理)
        elif "抱歉，暂无数据" in html or "no-content" in html:
            block = classify_block_page(html)
            if block == "login":
                # v9:cookie 失效先自动续期(游客态重访首页即可种回 KNS2COOKIE),续期后重拉本页
                if not (login_fix_attempts < 1 and refresh_cookies("列表页返回登录页")):
                    raise CnkiCookieError(
                        "登录状态已过期，自动恢复未成功（数据源可能要求登录或当前网络受限），请检查网络后重试"
                    )
                login_fix_attempts = 1
                html = search_grid(query_json, page_num=page, page_size=cur_size,
                                   captcha_verification=captcha_verification,
                                   bool_search=bool_search)
                if "抱歉，暂无数据" in html or "no-content" in html:
                    if classify_block_page(html) == "login":
                        raise CnkiCookieError(
                            "登录状态已过期，自动恢复未成功（数据源可能要求登录或当前网络受限），请检查网络后重试"
                        )
                    print(f"[列表] 第{page}页无数据，停止翻页")
                    emit_log(f"[列表] 第{page}页未检索到更多数据，获取完成")
                    break
                # 续期生效:落回下方 parse_list 正常流程
            elif block == "security":
                print("[提示] 疑似触发安全验证（风控），请降低请求频率或稍后重试")
                emit_log(f"[列表] 第{page}页检测到访问限制，已停止获取，稍后可重试")
                break
            else:
                print(f"[列表] 第{page}页无数据，停止翻页")
                emit_log(f"[列表] 第{page}页未检索到更多数据，获取完成")
                # v7.2:知网声称"无数据"也可能是拦截页伪装,落盘留证
                _dump_debug_list_html(html, page)
                break

        items = parse_list(html)
        if not items:
            block = classify_block_page(html)
            print(f"[列表] 第{page}页 解析 0 条, block={block}, head={html[:200]!r}")
            if block == "security":
                throttle_hit("列表页安全验证")
                print("[提示] 疑似触发安全验证（风控），请降低请求频率或稍后重试")
                emit_log(f"[列表] 第{page}页检测到访问限制，已停止获取，稍后可重试")
                break
            elif block == "login":
                # v9:cookie 失效先自动续期,续期后重拉本页(每条式子限 1 次)
                if not (login_fix_attempts < 1 and refresh_cookies("列表解析返回登录页")):
                    raise CnkiCookieError(
                        "登录状态已过期，自动恢复未成功（数据源可能要求登录或当前网络受限），请检查网络后重试"
                    )
                login_fix_attempts = 1
                html = search_grid(query_json, page_num=page, page_size=cur_size,
                                   captcha_verification=captcha_verification,
                                   bool_search=bool_search)
                items = parse_list(html)
                if not items:
                    if classify_block_page(html) == "login":
                        raise CnkiCookieError(
                            "登录状态已过期，自动恢复未成功（数据源可能要求登录或当前网络受限），请检查网络后重试"
                        )
                    print(f"[列表] 第{page}页 续期后仍解析 0 条，停止")
                    emit_log(f"[列表] 第{page}页数据暂时不可获取，已停止本组获取")
                    _dump_debug_list_html(html, page)
                    break
                # 续期生效:items 非空,跳过重拉直接落回下方 extend
            else:
                # 非风控：可能是偶发空响应/半加载页面，重拉一次再判定
                debug_log(f"[列表] 第{page}页 解析 0 条(block={block})，1s 后重拉一次…")
                sleep_jitter(1)
                html = search_grid(query_json, page_num=page, page_size=cur_size,
                                   captcha_verification=captcha_verification,
                                   bool_search=bool_search)
                items = parse_list(html)
                if not items:
                    print(f"[列表] 第{page}页 重试后仍 0 条，停止")
                    emit_log(f"[列表] 第{page}页数据暂时不可获取，已停止本组获取")
                    # v7.2:解析0条且非已知拦截特征,落盘留证供离线分析
                    _dump_debug_list_html(html, page)
                    if page == 1:
                        # v9:真零结果早被"暂无数据"分支拦截;首页重拉仍解析不出
                        # → 列表模板失配(知网改版),上抛交前端告知,勿静默空结束。
                        # v9.3:签名排查提示落服务器控制台(print),用户可见文案保持产品级
                        print("[诊断] 首页解析连续 0 条且非已知拦截:优先排查签名机制"
                              "(当前签名仅覆盖 URL query,知网开启 body 签名校验症状一致)")
                        raise CnkiRevisionError(
                            "数据获取遇到异常：数据源返回的内容暂时无法解析"
                            "（数据源结构可能已更新），任务已停止。"
                            "现场样本已自动保存，请联系技术支持反馈此问题"
                        )
                    break
        all_items.extend(items)
        print(f"[列表] 第{page}页 抓到 {len(items)} 条，累计 {len(all_items)}")
        emit_log(f"[列表] 已获取 {len(items)} 条，累计 {len(all_items)} 条")
        if len(all_items) >= max_count:
            break
        if len(items) < cur_size:
            # v8.6:本页不满页 = 已是末页,不再请求下一页
            # (否则知网对越界游标返回"起始游标越界"异常空壳)
            break
        page += 1
        # 验证码通过后翻页用 boolSearch=false 不再触发
        if bool_search == "true":
            bool_search = "false"
        # v9.4 预防性节拍冷却:每连续完成 8 页(page-1 为已完成页数)主动长歇一次,
        # 打断知网滑动窗口的频率累积,在限流空壳出现前预防 —— 生产实测第 10 页
        # 高频翻页必中招,节拍冷却把它消解在成型之前。
        if (page - 1) % _PACE_COOLDOWN_EVERY_PAGES == 0:
            emit_log(
                f"[节拍] 已连续获取 {_PACE_COOLDOWN_EVERY_PAGES} 页，"
                f"休息 {_PACE_COOLDOWN_SECONDS:.0f}s 后继续，保障获取稳定"
            )
            sleep_jitter(_PACE_COOLDOWN_SECONDS)
        # 使用自适应限速的当前延迟(风控时增大,连续成功后回落)
        sleep_jitter(effective_delay())

    return all_items[:max_count]


class CnkiServerBusyError(Exception):
    """知网服务端异常空壳("抱歉,暂无数据,请稍后重试")。

    正常零结果页没有"请稍后重试"字样;该空壳是限流/风控的兜底响应,
    退避重试仍空壳时抛出,交由调用方处置(换式子/中止并提示)。
    """
    pass


class CnkiCookieError(Exception):
    """cookie 失效且自动续期无效(知网强制登录/IP 受限)——前端横幅告知。"""

    code = "cookie_expired"


class CnkiCaptchaBalanceError(Exception):
    """超级鹰题分不足/账号异常——需人工充值,自动重试无意义——前端横幅告知。"""

    code = "captcha_balance"


class CnkiRevisionError(Exception):
    """知网页面结构改版,既有模板/接口全部失配——需人工逆向——前端横幅告知。"""

    code = "cnki_revision"


# 服务端异常空壳的退避表(秒):知网限流窗口为分钟级,短退避基本无效,
# 按 15/45/120 秒逐级拉长(总等待 3 分钟,跨过单个限流窗口)
SERVER_BUSY_BACKOFF_SECONDS = [15, 45, 120]
SERVER_BUSY_RETRIES = len(SERVER_BUSY_BACKOFF_SECONDS)


class CnkiGBTCitationError(Exception):
    """GB/T 7714 引文获取失败的基类。"""
    pass


class CnkiGBTCitationMissing(CnkiGBTCitationError):
    """单篇 paper 极个别情况(详情页缺少 hidden input / 导出 API 无 GB/T 条目)。

    含义:不是 API 故障,是这一篇抓不到 GB/T 7714。
    adapter 应该跳过这篇,不阻塞整次检索。
    """
    pass


class CnkiGBTCitationAPIFailed(CnkiGBTCitationError):
    """知网 GB/T 7714 导出 API 调用本身失败(网络/超时/限流/cookie 失效/code!=1)。

    含义:整次检索应该停止,因为后续 paper 大概率也会同样失败。
    adapter 接到后应该 raise 出去,触发 stage:error 事件给前端。
    """
    pass


# ========================== 详情页元数据 ==========================
def _extract_hidden(html: str, field: str) -> str:
    """从详情页 HTML 抽 <input id='...' value='...'> 隐藏字段。"""
    import re as _re_h
    m = _re_h.search(rf'id="{_re_h.escape(field)}"[^>]*value="([^"]*)"', html)
    if m:
        return m.group(1)
    m = _re_h.search(rf'value="([^"]*)"[^>]*id="{_re_h.escape(field)}"', html)
    return m.group(1) if m else ""


def _fetch_gbt_citation(html: str) -> str:
    """从详情页 HTML 抽 GB/T 7714-2025 引文(server-side 路径,不依赖浏览器 JS)。

    原理:知网页面里有 3 个隐藏 input:
      #export-url  → https://kns.cnki.net/dm8/API/GetExport
      #export-id   → paper filename(论文 id)
      #paramdbcode → 库代码(CJFD/CAPJ 等)
    POST 该 API,displaymode=GBTREFER 即可拿 GB/T 7714-2025 原文。

    失败/异常处理:
      - 详情页缺少 hidden input(极个别论文结构特殊)→ 抛 CnkiGBTCitationMissing
        → adapter 跳过这 paper,继续下一篇
      - 导出 API 本身失败(网络/超时/限流/cookie 失效/code!=1)→ 抛 CnkiGBTCitationAPIFailed
        → adapter 停止整次检索,emit error 事件,告诉用户
    """
    import re as _re_gbt
    export_url = _extract_hidden(html, "export-url")
    export_id = _extract_hidden(html, "export-id")
    if not (export_url and export_id):
        # 极个别:这 paper 的详情页没有标准的 hidden input
        raise CnkiGBTCitationMissing(
            f"详情页缺少 GB/T 7714 导出 hidden input "
            f"(export-url={bool(export_url)}, export-id={bool(export_id)})"
        )
    up_m = _re_gbt.search(r'id="uniplatform"[^>]*value="([^"]*)"', html) or _re_gbt.search(r'value="([^"]*)"[^>]*id="uniplatform"', html)
    uniplatform = (up_m.group(1) if up_m else "") or "NZKPT"
    data = {
        "filename": export_id,
        "displaymode": "GBTREFER",
        "uniplatform": uniplatform,
    }
    headers = {
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Origin": "https://kns.cnki.net",
        "Referer": "https://kns.cnki.net/",
        "X-Requested-With": "XMLHttpStream",
    }
    try:
        # GB/T 走统一入口但禁用本层重试(retries=False)：断路器/代理 failover/
        # 全链路记账全部保留；重试节奏仍由外层 fetch_gbt_citation_with_retry
        # 的 2/4/8s 指数退避控制——两层叠加会把单篇等待拖到分钟级
        resp = http_post(export_url, data=data, headers=headers,
                         timeout=CONFIG["http"]["timeout"],
                         raise_for_status=True, retries=False,
                         op_name="GBT引文")
    except Exception as exc:
        raise CnkiGBTCitationAPIFailed(
            f"知网 GB/T 7714 导出 API 请求失败 "
            f"(url={export_url}, filename={export_id[:20]}…): {exc!r}"
        ) from exc
    try:
        j = resp.json()
    except Exception as exc:
        raise CnkiGBTCitationAPIFailed(
            f"知网 GB/T 7714 导出 API 返回非 JSON: {exc!r}"
        ) from exc
    # 合法 JSON 但非对象(如纯字符串/数组)→ 归类 APIFailed 进入重试,
    # 而不是让 j.get() 抛未分类 AttributeError 打穿重试封装
    if not isinstance(j, dict):
        raise CnkiGBTCitationAPIFailed(
            f"知网 GB/T 7714 导出 API 返回非对象 JSON: {type(j).__name__}, "
            f"body={str(j)[:120]!r}"
        )
    if j.get("code") != 1 or not isinstance(j.get("data"), list):
        raise CnkiGBTCitationAPIFailed(
            f"知网 GB/T 7714 导出 API 拒绝请求: code={j.get('code')}, msg={j.get('msg')!r}"
        )
    # 解析 value,只取 GB/T 7714-2025 格式
    parts: list[str] = []
    for d in j["data"]:
        if "GB/T 7714-2025" not in d.get("key", ""):
            continue
        for v in d.get("value", []):
            clean = _re_gbt.sub(r"<br\s*/?>", "\n", v)
            clean = _re_gbt.sub(r"<(?!\s*br\s*/?>)[^>]+>", "", clean)
            clean = _re_gbt.sub(r"^\s*\$\[\d+\]", "", clean).strip()
            clean = _re_gbt.sub(r"^\s*\[\d+\]", "", clean).strip()
            parts.append(clean)
    if not parts:
        # 极个别:导出 API 返回 data 但没有 GB/T 7714-2025 条目
        raise CnkiGBTCitationMissing(
            f"知网 GB/T 7714 导出 API 返回 data 但没有 GB/T 7714-2025 条目 "
            f"(filename={export_id[:20]}…)"
        )
    return "\n".join(parts).strip()


def fetch_gbt_citation_with_retry(html: str) -> str:
    """GB/T 7714 导出 API 的指数退避重试封装。

    分类处置(用户要求"不允许任何意外"——失败必须闭环,不能中止/静默丢):
      - CnkiGBTCitationMissing(单篇详情页结构问题):重试同一篇不会变好,直接透传
      - CnkiGBTCitationAPIFailed(API 故障/限流/cookie 抖动):按 2/4/8s 指数退避
        重试,每次失败抬升自适应限速;重试耗尽才抛给调用方
    """
    retries = int(CONFIG["runtime"].get("gbt_api_retries", 4))
    for attempt in range(retries):
        try:
            return _fetch_gbt_citation(html)
        except CnkiGBTCitationMissing:
            # 单篇结构缺失,重试无意义:透传给调用方按单篇跳过
            raise
        except CnkiGBTCitationAPIFailed as e:
            if attempt >= retries - 1:
                # 重试耗尽:仍抛 APIFailed,由上层(主流程/adapter)决策
                raise
            wait = 2.0 * (2 ** attempt)  # 2/4/8s 指数退避
            throttle_hit("GB/T 导出 API 失败")
            print(f"[引文] GB/T 导出失败({attempt + 1}/{retries}): {e}")
            print(f"[引文] 退避 {wait:.0f}s 后重试…")
            debug_log(f"[引文] GB/T 导出 API 失败，退避 {wait:.0f}s 重试({attempt + 1}/{retries})")
            sleep_jitter(wait)
    # 防御性兜底(理论上上面必然 return 或 raise)
    raise CnkiGBTCitationAPIFailed("GB/T 导出重试耗尽")


def fetch_abstract(detail_url: str) -> dict:
    # 详情页走统一入口(断路器/分层重试/代理 failover)；页面级风控语义
    # (security/login/JSON 空壳)由下方既有自愈逻辑处置
    resp = http_get(detail_url, timeout=CONFIG["http"]["timeout"], op_name="详情页")
    resp.encoding = "utf-8"
    text = resp.text

    # 详情页被拦:安全验证页=风控 → 抬升限速+退避后重拉一次,仍拦截才抛
    if classify_block_page(text) == "security":
        throttle_hit("详情页安全验证")
        print("[摘要] 详情页触发安全验证（风控），退避 8s 后重试…")
        emit_log("[详情] 该文献访问受限，正在自动重试…")
        sleep_jitter(8)
        resp = http_get(detail_url, timeout=CONFIG["http"]["timeout"], op_name="详情页")
        resp.encoding = "utf-8"
        text = resp.text
        if classify_block_page(text) == "security":
            throttle_hit("详情页连续 2 次安全验证")
            raise RuntimeError("详情页连续 2 次触发安全验证（风控），请降低请求频率或稍后重试（delay 调大）")
    # 登录页=cookie 失效:v9 先自动续期(游客态重访首页即可种回 KNS2COOKIE),续期后重拉
    if classify_block_page(text) == "login":
        if not refresh_cookies("详情页返回登录页"):
            raise CnkiCookieError(
                "知网返回登录页，cookie 自动续期无效（可能要求登录或本机 IP 受限）——请检查本机网络后重试"
            )
        resp = http_get(detail_url, timeout=CONFIG["http"]["timeout"], op_name="详情页")
        resp.encoding = "utf-8"
        text = resp.text
        if classify_block_page(text) == "login":
            raise CnkiCookieError(
                "知网返回登录页，cookie 自动续期无效（可能要求登录或本机 IP 受限）——请检查本机网络后重试"
            )

    # 反爬/异常时返回 JSON：抬升限速,补齐全部字段键,避免 CSV 缺列
    if text.startswith("{") or text.startswith("["):
        throttle_hit("详情页返回 JSON 空壳")
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
        parsed = _parse_detail(text, detail_url)
    except Exception as e:
        # 解析失败时保留页面供调试，避免每抓一篇都覆盖写盘
        try:
            debug_path = CONFIG["paths"]["debug_abstract_html"]
            Path(debug_path).write_text(text, encoding="utf-8")
        except Exception:
            pass
        raise RuntimeError(f"_parse_detail 失败: {e!r}")

    global _empty_abstract_streak
    # 三代模板选择器都没取到摘要:落盘页面样本取证(同 URL 只存一份,总量上限 10 份)
    if not (parsed.get("abstract") or "").strip():
        try:
            debug_base = Path(CONFIG["paths"]["debug_abstract_html"])
            url_tag = md5(detail_url.encode("utf-8")).hexdigest()[:8]
            dump_path = debug_base.with_name(f"debug_abstract_{url_tag}.html")
            if (not dump_path.exists()
                    and len(list(debug_base.parent.glob("debug_abstract_*.html"))) < 10):
                dump_path.write_text(text, encoding="utf-8")
                debug_log(f"[摘要] 摘要为空,已保存页面样本 {dump_path.name}(详情页模板可能又改版)")
        except Exception:
            pass
        # v9:连续空摘要哨兵 —— 单篇偶发空可跳过;连续 30 篇空 = 详情页模板改版,
        # 自动处置已穷尽,上抛交前端横幅告知,勿静默烧完整个清单
        with _empty_abstract_lock:
            _empty_abstract_streak += 1
            streak = _empty_abstract_streak
        if streak >= 30:
            raise CnkiRevisionError(
                f"连续 {streak} 篇摘要解析为空（详情页模板疑似改版），样本已落盘——请反馈开发者更新解析模板"
            )
    else:
        # 本篇摘要解析成功:哨兵计数清零
        with _empty_abstract_lock:
            _empty_abstract_streak = 0

    # GB/T 7714-2025 引文:走指数退避重试封装(Missing=单篇结构问题透传 /
    # APIFailed=退避重试耗尽才传播),不再一遇 API 抖动就中止整次检索
    gbt = fetch_gbt_citation_with_retry(text)
    parsed["gbt_citation"] = gbt
    # 本篇详情页全流程成功:自适应限速可回落
    throttle_ok()
    return parsed

    # 下面的死代码保留以防 _fetch_gbt_citation 之外的旧 fall-back 路径(实际不会执行)
    if False:
        # 仅解析失败时保留页面供调试，避免每抓一篇都覆盖写盘
        try:
            with open(CONFIG["paths"]["debug_abstract_html"], "w", encoding="utf-8") as f:
                f.write(text)
        except Exception:
            pass
        raise


def _parse_detail(text: str, detail_url: str) -> dict:
    # v9.6:etree.HTML("") 返回 None 会 AttributeError,空树兜底走字段全空路径
    tree = etree.HTML(text) or etree.Element("html")

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
        # kcms2 新模板:摘要容器为 div#ChDivSummary / div.abstract-text(老选择器全部落空的根因)
        '//div[@id="ChDivSummary"]',
        '//div[contains(@class,"abstract-text")]',
        # 最终兜底:meta description 通常是摘要(可能截断)
        '//meta[@name="description"]/@content',
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
    """保存失败清单（供 --retry-failed 补抓）。

    空清单时删除旧文件:防止上一次运行遗留的陈旧清单误导后续补抓
    (陈旧清单里的 URL 可能已在新一轮全量抓取中成功,重抓纯属浪费)。
    """
    if not failed_items:
        try:
            if os.path.exists(path):
                os.remove(path)
                print(f"[保存] 失败清单已清空，删除旧文件 {path}")
        except Exception:
            pass
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


def _normalize_loaded_rows(rows: list) -> list:
    """CSV 读回的行,列表字段(authors 等)是 "a | b" 串;写回前归一化回 list。

    否则 save_results 对 CSV 再做一次 " | ".join 会按字符拆分字符串。
    """
    for r in rows:
        if not isinstance(r, dict):
            continue
        for field in ("authors", "orgs", "keywords", "funds"):
            v = r.get(field)
            if isinstance(v, str):
                r[field] = [x.strip() for x in v.split(" | ") if x.strip()]
    return rows


# ========================== 完整性校验与自动补抓闭环 ==========================
# 记录不完整(摘要/引文为空)时的最大补抓次数:
# 个别文献类型本身无摘要,限次收录防死循环
EMPTY_ABSTRACT_MAX_ATTEMPTS = 2
# 补抓闭环连续零进展轮次的熔断阈值:
# cookie 过期/打码余额耗尽属人工故障,自动空转只会烧超级鹰余额
FILL_LOOP_ZERO_PROGRESS_LIMIT = 3
# 补抓轮次间隔(秒):按轮次线性递增,封顶 300s,给风控窗口留恢复时间
FILL_LOOP_BASE_INTERVAL = 30.0
FILL_LOOP_MAX_INTERVAL = 300.0


def _record_complete(record: dict) -> bool:
    """记录完整性校验:gbt_citation 与 abstract 必须同时非空。

    任一为空即判定不完整 → 该 URL 进补抓队列(不允许静默丢数据)。
    """
    return bool((record.get("gbt_citation") or "").strip()) and \
        bool((record.get("abstract") or "").strip())


def auto_fill_loop(failed_path: str, output: str, force: bool = True) -> dict:
    """失败清单自动补抓闭环:循环补抓直到清单清空或触发熔断。

    确定性保障(对应"不允许任何意外"的最终一致性要求):
      - 每轮结束立即落盘(结果合并写回 + 剩余清单写回),任意时刻中断不丢进度
      - 连续 FILL_LOOP_ZERO_PROGRESS_LIMIT 轮零进展即熔断——
        cookie 过期/打码余额耗尽是人工故障,继续空转只会烧余额
      - 记录仍不完整时限量补抓 EMPTY_ABSTRACT_MAX_ATTEMPTS 次,
        超限标记 abstract_missing 收录(可见、可查,绝不静默丢弃)

    返回 {"recovered", "remaining", "rounds", "circuit_break"}
    """
    stats = {"recovered": 0, "remaining": 0, "rounds": 0, "circuit_break": False}
    if not force:
        return stats
    remaining: list = []
    zero_streak = 0
    round_no = 0
    while True:
        # 读清单(文件不存在或为空即闭环完成)
        try:
            with open(failed_path, encoding="utf-8") as f:
                failed = json.load(f)
        except FileNotFoundError:
            failed = []
        except Exception as e:
            print(f"[补抓] 读取失败清单出错: {e}，终止闭环(清单保留待人工处理)")
            emit_log("[补抓] 补全记录读取异常，自动补全已暂停，已获取的数据不受影响")
            stats["remaining"] = -1
            return stats
        if not failed:
            print("[补抓] 失败清单已清空，闭环完成")
            stats["rounds"] = round_no
            break
        round_no += 1
        wait = min(FILL_LOOP_BASE_INTERVAL * round_no, FILL_LOOP_MAX_INTERVAL)
        if round_no > 1:
            print(f"[补抓] 第 {round_no} 轮:剩余 {len(failed)} 条，{wait:.0f}s 后开始")
            emit_log(f"[补全] 第 {round_no} 轮：剩余 {len(failed)} 条待补全")
            sleep_jitter(wait)
        print(f"[补抓] 第 {round_no} 轮开始:共 {len(failed)} 条待补")
        emit_log(f"[补全] 第 {round_no} 轮开始：共 {len(failed)} 条待补全")

        recovered_this_round = 0
        remaining = []
        new_results = []
        for item in failed:
            url = item.get("url") if isinstance(item, dict) else str(item)
            if not url:
                continue
            try:
                d = fetch_abstract(url)
            except Exception as e:
                # 仍失败:更新错误信息留清单,下一轮再试
                if isinstance(item, dict):
                    item["error"] = str(e)
                else:
                    item = {"url": url, "error": str(e)}
                remaining.append(item)
                print(f"[补抓] 仍失败 {url[:70]}: {e}")
                sleep_jitter(effective_delay())
                continue
            if _record_complete(d):
                new_results.append(d)
                recovered_this_round += 1
                print(f"[补抓] 成功 {url[:70]}")
            else:
                # 仍不完整:限量补抓,超限标记收录(可见,不静默丢)
                attempts = int(item.get("attempts", 0)) + 1
                if attempts >= EMPTY_ABSTRACT_MAX_ATTEMPTS:
                    d["abstract_missing"] = True
                    new_results.append(d)
                    recovered_this_round += 1
                    print(f"[补抓] 补抓 {attempts} 次仍不完整，标记 abstract_missing 收录 {url[:60]}")
                    emit_log("[补全] 该文献摘要暂时无法获取，已收录其余信息")
                else:
                    item = dict(item) if isinstance(item, dict) else {}
                    item["url"] = url
                    item["kind"] = "empty_abstract"
                    item["attempts"] = attempts
                    item.pop("error", None)
                    remaining.append(item)
                    print(f"[补抓] 记录不完整(第 {attempts} 次)，留清单再补 {url[:70]}")
            sleep_jitter(effective_delay())

        # 本轮落盘:结果合并写回(绝不覆盖已有数据) + 剩余清单写回
        if new_results:
            existing, _ = load_existing_results(output)
            existing = _normalize_loaded_rows(existing)
            existing.extend(new_results)
            save_results(existing, output)
        save_failed(remaining, failed_path)
        stats["rounds"] = round_no
        stats["recovered"] += recovered_this_round
        print(f"[补抓] 第 {round_no} 轮结束:恢复 {recovered_this_round} 条，剩余 {len(remaining)} 条")
        emit_log(f"[补全] 第 {round_no} 轮完成：补全 {recovered_this_round} 条，剩余 {len(remaining)} 条")
        if not remaining:
            break
        # 零进展熔断:连续 N 轮无一恢复,继续空转只会烧打码余额
        if recovered_this_round == 0:
            zero_streak += 1
            if zero_streak >= FILL_LOOP_ZERO_PROGRESS_LIMIT:
                print(f"[补抓] 连续 {zero_streak} 轮零进展，熔断闭环(请检查 cookie / 超级鹰余额 / 风控状态)")
                emit_log("[补全] 连续多轮无进展，已停止自动补全，已获取的数据不受影响")
                stats["circuit_break"] = True
                break
        else:
            zero_streak = 0
    stats["remaining"] = len(remaining)
    return stats


def cmd_retry_failed(failed_path: str, output: str) -> bool:
    """读取失败清单补抓:复用 auto_fill_loop,结果合并写回。

    v9.0 修复:旧版 save_results(results, output) 是覆盖写,
    会把主流程已抓到的数据整个冲掉——改为合并写回。
    """
    try:
        with open(failed_path, encoding="utf-8") as f:
            failed = json.load(f)
    except FileNotFoundError:
        print("[重试] 失败清单不存在（此前已全部恢复），无需补抓")
        return False
    except Exception as e:
        print(f"[错误] 读取失败清单 {failed_path} 失败: {e}")
        return False
    if not failed:
        print("[重试] 失败清单为空，无需补抓")
        return False
    stats = auto_fill_loop(failed_path, output, force=True)
    print(f"[重试] 补抓闭环结束:恢复 {stats['recovered']} 条，剩余 {stats['remaining']} 条"
          f"（共 {stats['rounds']} 轮{',已熔断' if stats['circuit_break'] else ''}）")
    return stats["recovered"] > 0 or stats["remaining"] == 0


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


# cookie 自动续期节流:3 线程并发下 10s 内只允许真正刷新一次
_cookie_refresh_lock = threading.Lock()
_cookie_refresh_ts = 0.0

# v9:连续空摘要哨兵状态(详情页模板改版检测,3 线程并发安全)
_empty_abstract_streak = 0
_empty_abstract_lock = threading.Lock()


def refresh_cookies(reason: str = "") -> bool:
    """cookie 过期自动续期(游客态程序可解,无需人工)。

    原理:KNS2COOKIE 由知网在访问首页时以 Set-Cookie 下发,过期后重新
    访问首页/高级检索页即可续期。保留旧 cookie 中服务端未重种的字段
    (如 Ecp_ClientId 客户端指纹),避免签名一致性漂移。
    10s 节流防止 3 线程并发重复刷新。返回 True 表示续期后验证可用。
    """
    global _cookie_refresh_ts
    with _cookie_refresh_lock:
        if time.time() - _cookie_refresh_ts < 10:
            # 刚有线程刷新过:直接用当前 session 复验(续期已由它完成)
            return check_cookies()
        _cookie_refresh_ts = time.time()
    print(f"[cookies] 检测到 cookie 失效({reason or '未知原因'})，自动续期中…")
    emit_log("[登录] 检测到登录状态已过期，正在自动恢复…")
    base = CONFIG["endpoints"]["base"]
    search_url = CONFIG["endpoints"].get("search") or base
    try:
        session.get(base, timeout=CONFIG["http"]["timeout"])
        session.get(search_url, timeout=CONFIG["http"]["timeout"])
    except Exception as e:
        print(f"[cookies] 自动续期请求失败: {e}")
        debug_log(f"[cookies] 自动续期请求失败: {e}")
        return False
    new_cookies = session.cookies.get_dict()
    if not new_cookies.get("KNS2COOKIE"):
        # 服务端未种新 KNS2COOKIE(可能 IP 受限):合并旧值再验证一次
        session.cookies.update({k: v for k, v in COOKIES.items() if k not in new_cookies})
    if check_cookies():
        save_cookies(session.cookies.get_dict(), CONFIG["paths"]["cookies_file"])
        print("[cookies] cookie 已自动续期")
        emit_log("[登录] 登录状态已恢复，任务继续")
        return True
    print("[cookies] 自动续期后校验仍失败（知网可能强制登录/IP 受限）")
    emit_log("[登录] 自动恢复未成功，任务即将停止。请检查网络后重试；如持续出现请联系技术支持")
    return False


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
            # v9.1:预热顺手刷新 turnpage 会话令牌并落盘 —— 保证首搜就带有效令牌，
            # 避免重启后旧令牌触发"查询对象结构错误"被误判为限流
            refresh_turnpage("cookie 预检")
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
    parser.add_argument("--expert", "-e", default="", help="专业检索式，如 SU=('主题词A'+'主题词B')*'主题词C'（优先于 --keyword）")
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
    parser.add_argument("--no-auto-retry", action="store_true",
                        help="关闭主流程末尾的失败清单自动补抓闭环（默认开启）")
    parser.add_argument("--setup-cookies", action="store_true", help="重新录入 cookie（退出爬虫流程）")
    args = parser.parse_args()

    # setup-cookies 模式
    if args.setup_cookies:
        sys.exit(0 if cmd_setup_cookies() else 1)

    # 跨设备使用：每次启动都校验 cookie（必需字段 + 真实可用性探测）
    if not check_cookies():
        sys.exit(3)

    # 超级鹰余额预警:余额耗尽=验证码必然过不去(人工故障,必须提前暴露而非中途炸)
    try:
        _score = cj.get_score()
        if isinstance(_score, dict) and _score.get("err_no") == 0:
            _tifen = int(_score.get("tifen") or 0)
            print(f"[余额] 超级鹰题分余额: {_tifen}")
            if _tifen < 200:
                print("[警告] 超级鹰余额低于 200，验证码识别可能中途失败，请尽快充值！")
                emit_log(f"[警告] 验证码识别服务（超级鹰）余额不足：{_tifen}，请尽快充值，否则部分文献可能无法获取")
        else:
            print(f"[警告] 超级鹰余额查询失败: {_score}（不阻塞主流程）")
    except Exception as e:
        print(f"[警告] 超级鹰余额查询异常: {e}（不阻塞主流程）")

    # 补抓模式：只重抓失败清单里的详情页，不重新检索
    if args.retry_failed:
        # retry 模式也走规范化输出路径,保证补抓结果与已有输出可合并
        _retry_output = args.output or f"{CONFIG['paths']['default_output_prefix']}_{int(time.time())}.json"
        sys.exit(0 if cmd_retry_failed(args.retry_failed, _retry_output) else 1)

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
    # 输出路径规范化:不指定时固定为带时间戳的文件,全流程复用同一文件
    # (否则增量保存/补抓合并各自生成不同时间戳文件,闭环合并会失效)
    output_path = args.output or f"{CONFIG['paths']['default_output_prefix']}_{int(time.time())}.json"
    overall_start = time.time()

    # 断点续传：加载已有输出文件，跳过已抓详情页
    if args.resume:
        prev_results, prev_urls = load_existing_results(output_path)
        all_results = list(prev_results)
        seen_urls = set(prev_urls)
        if prev_results:
            print(f"[续传] 已从 {output_path} 载入 {len(prev_results)} 条，跳过已抓 URL")

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

        try:
            items = fetch_all_list(
                query_json=query_json,
                max_count=max_default,
                force_captcha=args.force_captcha,
            )
        except CnkiServerBusyError as exc:
            print(f"\n[中止] {exc}\n建议:降低请求频率(--delay)或稍后重试,必要时刷新 cookies.json")
            sys.exit(1)
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
            ok = False
            try:
                d = fetch_abstract(it["url"])
                # 完整性校验:摘要与引文必须同时非空,否则进补抓队列(不静默丢)
                if _record_complete(d):
                    all_results.append(d)
                    ok = True
                else:
                    print(f"\n[摘要] 记录不完整(摘要/引文为空) {it['url'][:60]}，进入补抓队列")
                    emit_log("[补全] 该文献信息暂不完整，已加入自动补全队列")
                    failed_items.append({
                        "url": it["url"], "keyword": kw,
                        "kind": "empty_abstract", "attempts": 1,
                    })
            except Exception as e:
                print(f"\n[摘要] 失败 {it.get('url','')[:60]}: {e}")
                failed_items.append({"url": it["url"], "error": str(e), "keyword": kw})
            progress.update(success=ok)
            # 自适应限速:风控后自动放大,连续成功后回落
            sleep_jitter(effective_delay())
        progress.finish()

        # 每个关键词完成后增量写盘，中断/异常时不丢已抓数据
        try:
            save_results(all_results, output_path)
            save_failed(failed_items, failed_path)
        except Exception as e:
            print(f"[警告] 保存结果失败: {e}")

    # 全部结束后兜底保存
    try:
        save_results(all_results, output_path)
        save_failed(failed_items, failed_path)
    except Exception as e:
        print(f"[警告] 最终保存失败: {e}")

    # ---------- 确定性闭环:失败清单自动补抓(--no-auto-retry 可关闭) ----------
    fill_stats = {"recovered": 0, "remaining": 0, "rounds": 0, "circuit_break": False}
    if failed_items and not args.no_auto_retry:
        print(f"\n[闭环] 有 {len(failed_items)} 条失败记录，启动自动补抓闭环…")
        emit_log(f"[补全] 自动补全已启动：待补全 {len(failed_items)} 条")
        fill_stats = auto_fill_loop(failed_path, output_path, force=True)
        if fill_stats["recovered"] > 0:
            # 补抓结果已由闭环合并落盘,重载内存副本以获得准确的最终统计
            loaded, _ = load_existing_results(output_path)
            all_results = _normalize_loaded_rows(loaded)
    elif args.no_auto_retry and failed_items:
        print(f"[闭环] 已按 --no-auto-retry 跳过自动补抓"
              f"（{len(failed_items)} 条失败留待手动: --retry-failed {failed_path}）")

    # ---------- 最终报告 ----------
    abstract_missing = sum(1 for r in all_results if isinstance(r, dict) and r.get("abstract_missing"))
    elapsed_total = time.time() - overall_start
    avg_speed = len(all_results) / elapsed_total if elapsed_total > 0 else 0
    print(f"\n{'=' * 70}")
    print(f"=== 全部完成，共 {len(all_results)} 条 ===")
    if dup_count > 0:
        print(f"=== 跨关键词去重跳过 {dup_count} 条重复 ===")
    if abstract_missing > 0:
        print(f"=== 其中 {abstract_missing} 条摘要不完整（已标记 abstract_missing 收录） ===")
    if fill_stats["remaining"] > 0:
        print(f"=== 仍有 {fill_stats['remaining']} 条未恢复，清单: {failed_path} ===")
        print(f"=== 修复 cookie/余额后运行: --retry-failed {failed_path} -o {output_path} ===")
    if fill_stats["circuit_break"]:
        print("=== 补抓因连续零进展熔断：请检查 cookie / 超级鹰余额 / 风控状态后手动补抓 ===")
    print(f"=== 总耗时 {elapsed_total:.1f}s，平均 {avg_speed:.2f} 条/s ===")
    cur_delay = effective_delay()
    if cur_delay > throttle_base() * 1.5:
        print(f"=== 提示:本次因风控信号自适应限速已升至 {cur_delay:.1f}s"
              f"（基准 {throttle_base():.1f}s），建议调大 config 的 delay_seconds ===")
    print(f"{'=' * 70}")


# 模块导入时自动初始化（保存配置后调用 init() 可热重载）
init()

if __name__ == "__main__":
    main()
