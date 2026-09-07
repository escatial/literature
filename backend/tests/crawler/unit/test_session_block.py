# -*- coding: utf-8 -*-
"""会话级空壳熔断(CnkiSessionBlockedError)单元测试。

背景(2026-09-08 用户反馈):知网对整个会话/网络风控时,每条检索式的第 1 页
都返回"抱歉,暂无数据,请稍后重试"空壳(value="")。旧行为:每式烧满
15/45/120s 退避 → CnkiServerBusyError 进补漏队列 → 3 轮 60/120/300s 冷却
重试全部同样空壳 —— 40+ 分钟空转后以 0 篇收场。

v9.7 行为:
  - 退避第 2 次重试前自动更换会话凭证(refresh_cookies),不再发完全相同的请求;
  - 零进度空壳连续 2 条式子 → CnkiSessionBlockedError 快速失败;
  - 任何一次成功解析都清零连击;有进度(翻页中途)的空壳仍走单式限流语义;
  - adapter 把会话熔断列为闸口故障:立即终止,不进补漏。
"""
import pytest

from automation.cnki import crawler
from automation.cnki.crawler import (
    CnkiServerBusyError,
    CnkiSessionBlockedError,
    reset_session_block_streak,
)
from automation import cnki_adapter

BUSY_SHELL = '<div id="briefBox"><p class="no-content" value="">抱歉，暂无数据，请稍后重试。</p></div>'


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    reset_session_block_streak()
    # 退避/节流全部免睡,测试秒级完成
    monkeypatch.setattr(crawler, "sleep_jitter", lambda *_a, **_k: None)
    yield
    reset_session_block_streak()


def _shell_mode(monkeypatch, *, refresh_ok=True, refresh_calls=None):
    """search_grid 永远返回限流空壳;记录换凭证调用。"""
    monkeypatch.setattr(crawler, "search_grid", lambda *a, **k: BUSY_SHELL)
    if refresh_calls is None:
        refresh_calls = []
    monkeypatch.setattr(
        crawler, "refresh_cookies",
        lambda reason="": (refresh_calls.append(reason) or refresh_ok),
    )
    return refresh_calls


# ---------- crawler.fetch_all_list ----------

def test_first_zero_progress_shell_is_normal_busy(monkeypatch):
    _shell_mode(monkeypatch)
    with pytest.raises(CnkiServerBusyError):
        crawler.fetch_all_list(query_json="SU=测试", max_count=20)


def test_second_consecutive_zero_progress_shell_fast_fails(monkeypatch):
    _shell_mode(monkeypatch)
    with pytest.raises(CnkiServerBusyError):
        crawler.fetch_all_list(query_json="SU=测试", max_count=20)
    # 第 2 条式子:连击=2 → 会话级熔断,不再烧 CnkiServerBusyError 语义
    with pytest.raises(CnkiSessionBlockedError) as ei:
        crawler.fetch_all_list(query_json="SU=另一式", max_count=20)
    assert "风控" in str(ei.value) or "重试" in str(ei.value)


def test_busy_retries_refresh_cookies_once_at_second_attempt(monkeypatch):
    refresh_calls = _shell_mode(monkeypatch)
    with pytest.raises(CnkiServerBusyError):
        crawler.fetch_all_list(query_json="SU=测试", max_count=20)
    # 3 次退避重试中,只在第 2 次(attempt==2)换一次凭证,不过度刷新
    assert refresh_calls == ["零进度空壳自愈"]


def test_successful_parse_resets_streak(monkeypatch):
    _shell_mode(monkeypatch)
    with pytest.raises(CnkiServerBusyError):
        crawler.fetch_all_list(query_json="SU=测试", max_count=20)
    # 中间一次成功(正常返回数据页):模拟有 1 条结果的列表 HTML
    good_html = """
    <table><tr><td class="name">
      <a class="fz14" href="/kcms2/detail/Test">治理研究</a>
    </td><td class="quote">张三.治理研究[J].学报,2024,(1):1-10.</td></tr></table>
    """
    monkeypatch.setattr(crawler, "search_grid", lambda *a, **k: good_html)
    crawler.fetch_all_list(query_json="SU=成功式", max_count=20)
    # 连击已清零:再遇零进度空壳只是普通 busy,不熔断
    monkeypatch.setattr(crawler, "search_grid", lambda *a, **k: BUSY_SHELL)
    with pytest.raises(CnkiServerBusyError):
        crawler.fetch_all_list(query_json="SU=再来", max_count=20)


def test_mid_crawl_shell_does_not_trip_session_block(monkeypatch):
    """翻页中途(已有进度)的空壳:单式限流语义,不累计会话连击。"""
    # 第 1 页满页(pageSize=20)才会翻第 2 页;第 2 页返回空壳
    row = ('<tr><td class="name"><a class="fz14" href="/kcms2/detail/T{i}">治理研究{i}</a></td>'
           '<td class="quote">张三.治理研究[J].学报,2024,(1):1-10.</td></tr>')
    full_page1 = "<table>" + "".join(row.format(i=i) for i in range(20)) + "</table>"
    pages = {1: full_page1}
    monkeypatch.setattr(
        crawler, "search_grid",
        lambda query_json, page_num=1, **k: pages.get(page_num, BUSY_SHELL),
    )
    monkeypatch.setattr(crawler, "refresh_cookies", lambda reason="": True)
    with pytest.raises(CnkiServerBusyError):
        crawler.fetch_all_list(query_json="SU=测试", max_count=40)
    # 有进度的空壳不应累计连击:下一次零进度空壳仍是普通 busy
    monkeypatch.setattr(crawler, "search_grid", lambda *a, **k: BUSY_SHELL)
    with pytest.raises(CnkiServerBusyError):
        crawler.fetch_all_list(query_json="SU=下一式", max_count=20)


# ---------- adapter 补漏调度:会话熔断不进补漏 ----------

def test_session_block_is_gate_failure_not_rescued():
    """fetcher 抛 CnkiSessionBlockedError → 立即上抛,不烧补漏冷却。"""
    calls: list[str] = []
    sleeps: list[float] = []

    def fetcher(query: str) -> list[dict]:
        calls.append(query)
        if query == "q2":
            raise CnkiSessionBlockedError("连续 2 条检索式零进度空壳(会话风控)")
        return [{"title": query, "url": f"https://kns.cnki.net/{query}"}]

    with pytest.raises(CnkiSessionBlockedError):
        cnki_adapter._run_with_rescue(
            ["q1", "q2", "q3"], fetcher,
            emit_fn=lambda msg: None,
            sleep_fn=lambda s: sleeps.append(s),
            check_stopped=lambda: None,
            delay_seconds=0.0,
            target_count=100,
            rescue_cooldowns=(9999.0,),  # 哨兵:熔断生效则绝不出现
        )
    # q2 熔断即停:q3 不再提交,补漏冷却一次都没睡(式间 0s 延迟除外)
    assert calls == ["q1", "q2"]
    assert all(s < 100 for s in sleeps), sleeps


def test_busy_still_rescued_after_fix():
    """普通单式限流(CnkiServerBusyError)仍走补漏,产品级不丢单语义不变。"""
    calls: list[str] = []

    def fetcher(query: str) -> list[dict]:
        calls.append(query)
        if len(calls) == 1:
            raise CnkiServerBusyError("第1页连续 3 次空壳")
        return [{"title": query, "url": f"https://kns.cnki.net/{query}"}]

    merged, missing = cnki_adapter._run_with_rescue(
        ["q1"], fetcher,
        emit_fn=lambda msg: None,
        sleep_fn=lambda s: None,
        check_stopped=lambda: None,
        delay_seconds=0.0,
        target_count=100,
        rescue_rounds=1,
        rescue_cooldowns=(0.0,),
    )
    assert missing == []
    assert len(merged) == 1
