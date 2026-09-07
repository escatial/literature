# -*- coding: utf-8 -*-
"""集成测试:fetch_all_list 全链路(mock 知网服务器)。

覆盖:正常翻页 / boolSearch 与 turnpage / 签名 4 头 / 末页停 / 游标越界 /
真零结果 / 限流空壳退避 / 参数校验前置 / turnpage 自愈 / 登录墙 cookie 续期 /
pageSize 白名单 v9.2 回归 / 英数与滑块验证码分流。
"""
import json

import pytest

from automation.cnki import crawler
from helpers import (
    CURSOR_OVERFLOW_HTML,
    EMPTY_SHELL_HTML,
    LOGIN_PAGE_HTML,
    PARAM_INVALID_HTML,
    SECURITY_PAGE_HTML,
    STRUCT_ERROR_HTML,
    VERICODE_PAGE_HTML,
    make_list_html,
    make_list_rows,
)

OK20 = make_list_html(make_list_rows("N", 20), total=40)


# ========================== 正常翻页与请求形态 ==========================
def test_fetch_all_list_two_pages_and_request_shape(crawler_env):
    crawler_env.cfg["search"]["page_size"] = 20
    page1 = make_list_html(make_list_rows("A", 20), total=45)
    page2 = make_list_html(make_list_rows("A", 5, start_no=21), total=45)
    crawler_env.kns.push_grid(page1, page2)

    items = crawler.fetch_all_list("QJSON-MARK")
    assert len(items) == 25
    assert items[0]["title"] == "A研究0001"

    reqs = crawler_env.kns.grid_requests()
    assert [r["body"]["boolSearch"] for r in reqs] == [["true"], ["false"]]  # 首搜 true 翻页 false
    assert reqs[0]["body"]["turnpage"] == ["TP_TOKEN"]
    assert reqs[0]["body"]["QueryJson"] == ["QJSON-MARK"]
    assert reqs[0]["body"]["pageNum"] == ["1"] and reqs[1]["body"]["pageNum"] == ["2"]
    assert all(r["body"]["pageSize"] == ["20"] for r in reqs)

    # 签名 4 头 + ClientID 必须在请求头中
    h = reqs[0]["headers"]
    for key in ("timestamp", "nonce", "signature", "appID", "ClientID"):
        assert key in h, f"缺少签名头 {key}"
    assert h["ClientID"] == "test-client"
    assert h["signature"] and len(h["signature"]) == 32


def test_fetch_all_list_short_last_page_stops(crawler_env):
    crawler_env.kns.push_grid(OK20, make_list_html(make_list_rows("B", 5, start_no=21), total=25))
    items = crawler.fetch_all_list("Q")
    assert len(items) == 25
    assert len(crawler_env.kns.grid_requests()) == 2  # 不满页即停,无第 3 页请求


def test_fetch_all_list_cursor_overflow_is_normal_end(crawler_env):
    crawler_env.kns.push_grid(OK20, CURSOR_OVERFLOW_HTML)
    items = crawler.fetch_all_list("Q")
    assert len(items) == 20
    assert len(crawler_env.kns.grid_requests()) == 2


def test_fetch_all_list_zero_result_normal_end(crawler_env):
    # 真零结果:含"暂无数据"但无"请稍后重试" → 静默结束,不退避不抛错
    zero = '<html><body><div class="no-content">抱歉，暂无数据。</div></body></html>'
    crawler_env.kns.push_grid(zero)
    items = crawler.fetch_all_list("Q")
    assert items == []
    assert crawler_env.sleeps == []


# ========================== 限流空壳退避 ==========================
def test_fetch_all_list_busy_shell_backoff_then_recovers(crawler_env):
    # 第 1 次空壳 → 退避 15 → 再空壳 → 退避 45 → 第 3 次成功
    crawler_env.kns.push_grid(EMPTY_SHELL_HTML, EMPTY_SHELL_HTML, OK20)
    items = crawler.fetch_all_list("Q", max_count=20)
    assert len(items) == 20
    assert crawler_env.sleeps[:2] == [15.0, 45.0]  # SERVER_BUSY_BACKOFF 前两级
    assert crawler.effective_delay() > crawler.throttle_base()  # 空壳触发限速抬升


def test_fetch_all_list_busy_shell_exhausted_raises(crawler_env):
    crawler_env.kns.push_grid(*([EMPTY_SHELL_HTML] * 4))
    with pytest.raises(crawler.CnkiServerBusyError, match="连续 3 次"):
        crawler.fetch_all_list("Q", max_count=20)
    assert crawler_env.sleeps == [15.0, 45.0, 120.0]  # 恰好 3 级退避,不无限白等


# ========================== 预防性节拍冷却(v9.4) ==========================
def test_fetch_all_list_pacing_cooldown_every_8_pages(crawler_env):
    # 每连续完成 8 页主动冷却一次:在知网滑动窗口风控成型前打断累积
    # (生产实测:2s 间隔连续翻页约第 10 页必中"请稍后重试"空壳)
    crawler_env.cfg["search"]["page_size"] = 20
    pages = [
        make_list_html(make_list_rows(f"P{i}", 20, start_no=i * 20 + 1), total=240)
        for i in range(12)
    ]
    crawler_env.kns.push_grid(*pages)
    items = crawler.fetch_all_list("Q", max_count=240)
    assert len(items) == 240
    # 完成第 8 页后出现 40s 节拍冷却(此前 7 次为正常翻页间隔)
    assert crawler_env.sleeps[7] == 40.0
    assert crawler_env.sleeps.count(40.0) == 1  # 12 页内仅触发一轮节拍
    assert crawler_env.sleeps[8] == crawler_env.sleeps[0]  # 节拍后仍叠加正常自适应间隔


# ========================== 参数校验前置识别(v9.2) ==========================
def test_fetch_all_list_param_invalid_raises_immediately(crawler_env):
    crawler_env.kns.push_grid(PARAM_INVALID_HTML)
    with pytest.raises(crawler.CnkiRevisionError, match="参数校验"):
        crawler.fetch_all_list("Q")
    assert crawler_env.sleeps == []  # 参数错误零退避,绝不伪装限流白等
    assert len(crawler_env.kns.grid_requests()) == 1


# ========================== turnpage 结构错误自愈 ==========================
def test_fetch_all_list_struct_error_self_heals(crawler_env, tmp_path, monkeypatch):
    monkeypatch.setattr(crawler, "_DATA_DIR", tmp_path)  # 令牌落盘隔离
    crawler_env.kns.push_adv('<html><script>turnpage = "NEWTP";</script></html>')
    crawler_env.kns.push_grid(STRUCT_ERROR_HTML, OK20)

    items = crawler.fetch_all_list("Q", max_count=20)
    assert len(items) == 20
    assert crawler._turnpage == "NEWTP"
    reqs = crawler_env.kns.grid_requests()
    assert reqs[1]["body"]["turnpage"] == ["NEWTP"]  # 重拉携带新令牌
    assert (tmp_path / "turnpage.txt").read_text("utf-8") == "NEWTP"


def test_fetch_all_list_struct_error_twice_raises_revision(crawler_env, tmp_path, monkeypatch):
    monkeypatch.setattr(crawler, "_DATA_DIR", tmp_path)
    crawler_env.kns.push_adv('<html><script>turnpage = "NEWTP";</script></html>')
    crawler_env.kns.push_grid(STRUCT_ERROR_HTML, OK20, STRUCT_ERROR_HTML)

    with pytest.raises(crawler.CnkiRevisionError, match="结构"):
        crawler.fetch_all_list("Q", max_count=40)
    # 自愈机会只有一次:第 3 次请求仍结构错误 → 立即上抛
    assert len(crawler_env.kns.grid_requests()) == 3


# ========================== 首页解析 0 条 → 改版哨兵 ==========================
def test_fetch_all_list_first_page_zero_parse_raises_revision(crawler_env):
    blank = '<html><body><div id="countPageDiv"></div></body></html>'
    crawler_env.kns.push_grid(blank, blank)
    with pytest.raises(crawler.CnkiRevisionError, match="无法解析"):
        crawler.fetch_all_list("Q")
    assert crawler_env.sleeps == [1.0]  # 重拉前仅 1s,无长退避


# ========================== 登录墙 cookie 续期 ==========================
def test_fetch_all_list_login_wall_recovers_via_refresh(crawler_env, tmp_path, monkeypatch):
    monkeypatch.setattr(crawler, "_DATA_DIR", tmp_path)
    crawler_env.kns.push_grid(LOGIN_PAGE_HTML, OK20)

    items = crawler.fetch_all_list("Q", max_count=20)
    assert len(items) == 20
    # 续期走了首页(GET /)种回 KNS2COOKIE
    assert any(r["path"] == "/" for r in crawler_env.kns.requests_log)
    # 续期后 cookies 落盘
    saved = json.loads((tmp_path / "cookies.json").read_text("utf-8"))
    assert saved["KNS2COOKIE"] == "fresh-token"


def test_fetch_all_list_login_wall_refresh_fails_raises_cookie_error(crawler_env, monkeypatch):
    monkeypatch.setattr(crawler, "refresh_cookies", lambda reason="": False)
    crawler_env.kns.push_grid(LOGIN_PAGE_HTML)
    with pytest.raises(crawler.CnkiCookieError):
        crawler.fetch_all_list("Q")
    assert len(crawler_env.kns.grid_requests()) == 1  # 不重拉


# ========================== pageSize 白名单(v9.2 回归核心) ==========================
def test_fetch_all_list_page_size_normalized_when_remainder_small(crawler_env):
    crawler_env.cfg["search"]["page_size"] = 20
    crawler_env.kns.push_grid(
        make_list_html(make_list_rows("P", 20), total=30),
        make_list_html(make_list_rows("P", 10, start_no=21), total=30),
    )
    items = crawler.fetch_all_list("Q", max_count=25)  # 余量 5 → 必须归一到 10
    assert len(items) == 25  # 末尾截断兜底
    reqs = crawler_env.kns.grid_requests()
    assert reqs[0]["body"]["pageSize"] == ["20"]
    assert reqs[1]["body"]["pageSize"] == ["10"]  # 白名单向上对齐,绝不发 pageSize=5


# ========================== 验证码分流 ==========================
def test_fetch_all_list_alnum_captcha_flow(crawler_env, monkeypatch):
    monkeypatch.setattr(crawler, "solve_vericode", lambda html: "abcd")
    monkeypatch.setattr(crawler, "submit_vericode", lambda code: True)
    crawler_env.kns.push_grid(VERICODE_PAGE_HTML, OK20)

    items = crawler.fetch_all_list("Q", max_count=20)
    assert len(items) == 20
    reqs = crawler_env.kns.grid_requests()
    # 通过后重搜:boolSearch=false 且不带 captchaVerification
    assert reqs[1]["body"]["boolSearch"] == ["false"]
    assert "captchaVerification" not in reqs[1]["body"]


def test_fetch_all_list_slider_captcha_flow(crawler_env, monkeypatch):
    slider = '<html><body><img src="/verify/home?captchaId=xyz"/></body></html>'
    monkeypatch.setattr(crawler, "trigger_captcha", lambda q: "cap-verified")
    crawler_env.kns.push_grid(slider, OK20)

    items = crawler.fetch_all_list("Q", max_count=20)
    assert len(items) == 20
    reqs = crawler_env.kns.grid_requests()
    assert reqs[1]["body"]["captchaVerification"] == ["cap-verified"]
    assert reqs[1]["body"]["boolSearch"] == ["false"]


def test_fetch_all_list_force_captcha_first_request_carries_token(crawler_env, monkeypatch):
    monkeypatch.setattr(crawler, "trigger_captcha", lambda q: "pre-verified")
    crawler_env.kns.push_grid(OK20)
    crawler.fetch_all_list("Q", max_count=20, force_captcha=True)
    req = crawler_env.kns.grid_requests()[0]
    assert req["body"]["captchaVerification"] == ["pre-verified"]
    assert req["body"]["boolSearch"] == ["false"]


# ========================== 签名可诊断性回归(风险修复) ==========================
def test_fetch_all_list_abort_on_captcha_streak(crawler_env, monkeypatch):
    """滑块验证码连续失败熔断:连续 6 次处理失败抛 CnkiCaptchaBalanceError,不再烧题分。"""
    calls = {"n": 0}

    def always_fail(q):
        calls["n"] += 1
        raise RuntimeError("超级鹰服务异常")

    monkeypatch.setattr(crawler, "trigger_captcha", always_fail)
    crawler_env.kns.push_grid(SECURITY_PAGE_HTML)  # 恒返回滑块页 → 每页都走滑块分支
    with pytest.raises(crawler.CnkiCaptchaBalanceError, match="连续 6 次"):
        crawler.fetch_all_list("Q")
    assert calls["n"] == 6  # 恰 6 次熔断,不无限重试


def test_search_grid_warns_on_missing_client_id(crawler_env, capsys):
    """签名前置自检:Ecp_ClientId 缺失时立即警告,避免静默生成必然被拒的签名头。"""
    crawler_env.kns.push_grid(make_list_html(make_list_rows("W", 3)))
    crawler.session.cookies.clear()  # 清空 → Ecp_ClientId 缺失
    html = crawler.search_grid("Q")
    out = capsys.readouterr().out
    assert "Ecp_ClientId" in out and "签名" in out
    assert html  # 警告不阻断请求本身


def test_first_page_zero_parse_signature_hint_in_server_log(crawler_env, capsys):
    """签名被拒可诊断:排查提示落服务器控制台;用户可见异常文案保持产品级(v9.5)。"""
    crawler_env.kns.push_grid(make_list_html([]))
    with pytest.raises(crawler.CnkiRevisionError) as ei:
        crawler.fetch_all_list("Q")
    # 用户文案产品级:说清异常与求助路径,不含"签名"黑话
    assert "无法解析" in str(ei.value)
    assert "签名" not in str(ei.value)
    # 诊断细节保留在服务器控制台,供运维排障
    assert "签名" in capsys.readouterr().out
