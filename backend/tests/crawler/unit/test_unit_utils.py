# -*- coding: utf-8 -*-
"""单元测试:纯工具函数(pageSize 归一/turnpage 提取/签名/检索式/拦截页分类/抖动)。"""
import json
from urllib.parse import quote, unquote

import pytest

from automation.cnki import crawler


# ========================== _normalize_page_size(v9.2 回归核心) ==========================
@pytest.mark.parametrize("raw,expected", [
    (10, 10), (20, 20), (50, 50),          # 白名单原值直通
    (1, 10), (5, 10), (9, 10),             # 小余量向上对齐
    (11, 20), (19, 20), (21, 50), (49, 50),  # 中间值向上对齐
    (51, 50), (100, 50), (10000, 50),      # 超界封顶 50
])
def test_normalize_page_size_whitelist(crawler_env, raw, expected):
    assert crawler._normalize_page_size(raw) == expected


# ========================== extract_turnpage ==========================
@pytest.mark.parametrize("html,expected", [
    ("<script>var turnpage = 'abc123';</script>", "abc123"),
    ("<script>turnpage='x-y_z!!';</script>", "x-y_z!!"),
    ('<script>turnpage = "dq-token"</script>', "dq-token"),
    ("<html>无令牌页面</html>", ""),
    ("", ""),
])
def test_extract_turnpage(crawler_env, html, expected):
    assert crawler.extract_turnpage(html) == expected


# ========================== make_signature ==========================
def test_make_signature_structure_and_reproducible(crawler_env, monkeypatch):
    monkeypatch.setitem(crawler.CONFIG["sign"], "secret", "unit-secret")
    monkeypatch.setitem(crawler.CONFIG["sign"], "app_id", "LoginWap")

    url = "https://kns.cnki.net/kns8s/brief/grid"
    sign = crawler.make_signature(url, "client-xyz")

    # 结构:4 头齐全,timestamp/nonce 13 位数字,signature 32 位 hex
    assert set(sign) == {"timestamp", "nonce", "signature", "appID"}
    assert len(sign["timestamp"]) == 13 and sign["timestamp"].isdigit()
    assert len(sign["nonce"]) == 13 and sign["nonce"].isdigit()
    assert len(sign["signature"]) == 32
    int(sign["signature"], 16)  # hex 可解析
    assert sign["appID"] == "LoginWap"

    # 可复算:签名串 = timestamp+nonce+secret+排序query串+clientId
    import hashlib
    expected = hashlib.md5(
        f'{sign["timestamp"]}{sign["nonce"]}unit-secret{"client-xyz"}'.encode()
    ).hexdigest()
    assert sign["signature"] == expected


def test_make_signature_sorts_query_params_case_insensitive(crawler_env, monkeypatch):
    monkeypatch.setitem(crawler.CONFIG["sign"], "secret", "s")
    # 固定时间与随机源:排除 timestamp/nonce 动态性,只对比 query 排序的影响
    monkeypatch.setattr(crawler.time, "time", lambda: 1700000000.0)
    monkeypatch.setattr(crawler.random, "randrange", lambda n: 1234567890123)
    # B 与 a:URL 中顺序不同,但按 key 小写排序后拼接串一致 → 同签名
    sign_a = crawler.make_signature("https://x/p?a=1&B=2", "cid")
    sign_b = crawler.make_signature("https://x/p?B=2&a=1", "cid")
    assert sign_a["timestamp"] == sign_b["timestamp"]
    assert sign_a["signature"] == sign_b["signature"]
    # 精确复算:timestamp=int(1.7e9*1000)=1700000000000,nonce=1234567890123
    # 排序按 key 小写 → a 在 B 前,拼接串 "a=1B=2"
    import hashlib
    expected = hashlib.md5(
        "17000000000001234567890123sa=1B=2cid".encode()
    ).hexdigest()
    assert sign_a["signature"] == expected


# ========================== build_query / build_expert_query ==========================
def test_build_query_structure(crawler_env):
    raw = unquote(crawler.build_query("个人信贷", field="SU", operator="TOPRANK", resource="CAPJ"))
    q = json.loads(raw)
    assert q["Resource"] == "CAPJ"
    assert q["SearchType"] == 1
    assert q["Rlang"] == "CHINESE"
    item = q["QNode"]["QGroup"][0]["ChildItems"][0]["Items"][0]
    assert item["Field"] == "SU" and item["Operator"] == "TOPRANK" and item["Value"] == "个人信贷"


def test_build_query_extra_conditions(crawler_env):
    raw = unquote(crawler.build_query(
        "x", extra=[{"field": "TI", "value": "风险"}],
    ))
    q = json.loads(raw)
    children = q["QNode"]["QGroup"][0]["ChildItems"]
    assert len(children) == 2
    assert children[1]["Items"][0]["Field"] == "TI"
    assert children[1]["Items"][0]["Value"] == "风险"


def test_build_query_illegal_resource_raises(crawler_env):
    with pytest.raises(ValueError, match="未知资源代码"):
        crawler.build_query("x", resource="NO_SUCH_DB")


def test_build_expert_query_structure(crawler_env):
    raw = unquote(crawler.build_expert_query("SU=('a'+'b')*'c'", resource="CAPJ"))
    q = json.loads(raw)
    assert q["SearchType"] == 4  # 专业检索标记
    item = q["QNode"]["QGroup"][0]["Items"][0]
    assert item["Field"] == "EXPERT"
    assert item["Value"] == "SU=('a'+'b')*'c'"


def test_build_expert_query_illegal_resource_raises(crawler_env):
    with pytest.raises(ValueError, match="未知资源代码"):
        crawler.build_expert_query("SU='x'", resource="BAD")


# ========================== classify_block_page ==========================
from helpers import LOGIN_PAGE_HTML, SECURITY_PAGE_HTML  # noqa: E402


def test_classify_security_page(crawler_env):
    assert crawler.classify_block_page(SECURITY_PAGE_HTML) == "security"


def test_classify_login_page(crawler_env):
    assert crawler.classify_block_page(LOGIN_PAGE_HTML) == "login"


def test_classify_normal_large_page(crawler_env):
    # >100KB 正常大页面:即便含特征词也不误伤
    big = "<html>安全验证" + "x" * 100_001 + "</html>"
    assert crawler.classify_block_page(big) == ""


def test_classify_marker_beyond_head_ignored(crawler_env):
    # 特征词只看前 5000 字符,之后出现的忽略
    late = "<html>" + "x" * 5001 + "欢迎登录</html>"
    assert crawler.classify_block_page(late) == ""


def test_classify_empty(crawler_env):
    assert crawler.classify_block_page("") == ""


# ========================== is_captcha_required ==========================
from helpers import VERICODE_PAGE_HTML  # noqa: E402


def test_is_captcha_required_hits(crawler_env):
    assert crawler.is_captcha_required(VERICODE_PAGE_HTML) is True
    assert crawler.is_captcha_required(SECURITY_PAGE_HTML) is True
    assert crawler.is_captcha_required(
        '<html><img src="/verify/home?captchaId=x"/></html>'
    ) is True


def test_is_captcha_required_normal_page(crawler_env):
    assert crawler.is_captcha_required("<html><body>正常列表</body></html>") is False


# ========================== sleep_jitter 抖动范围(±20%) ==========================
def test_sleep_jitter_bounds(monkeypatch):
    # 不用 crawler_env(它会把 sleep_jitter 替换为记录器),直接测真实现
    real_sleeps = []
    monkeypatch.setattr(crawler.time, "sleep", lambda s: real_sleeps.append(s))
    for _ in range(50):
        crawler.sleep_jitter(2.0)
    assert len(real_sleeps) == 50
    # ±20% 抖动:全部落在 [1.6, 2.4]
    assert all(1.6 <= s <= 2.4 for s in real_sleeps)
    # 抖动确实生效(50 次全等的概率可忽略)
    assert len(set(real_sleeps)) > 1


# ========================== deep_merge / load_cookies ==========================
def test_deep_merge_nested_override():
    base = {"a": {"x": 1, "y": 2}, "b": 3}
    out = crawler.deep_merge(base, {"a": {"y": 9}})
    assert out == {"a": {"x": 1, "y": 9}, "b": 3}


def test_load_cookies_strips_comment_keys(crawler_env, tmp_path):
    p = tmp_path / "cookies.json"
    p.write_text(json.dumps({"KNS2COOKIE": "k", "_note": "注释字段", "Ecp_ClientId": "e"}), "utf-8")
    cookies = crawler.load_cookies(str(p))
    assert cookies == {"KNS2COOKIE": "k", "Ecp_ClientId": "e"}


def test_load_cookies_missing_file(crawler_env, tmp_path):
    assert crawler.load_cookies(str(tmp_path / "no.json")) == {}
