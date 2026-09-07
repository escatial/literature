# -*- coding: utf-8 -*-
"""集成测试:fetch_abstract 全链路(mock 知网详情页 + GB/T 导出 API)。

覆盖:正常全字段 / JSON 空壳降级 / 安全验证退避重试与连续熔断 /
登录墙 cookie 续期 / 连续空摘要哨兵 / 空摘要样本落盘。
"""
import pytest

from automation.cnki import crawler
from helpers import (
    LOGIN_PAGE_HTML,
    SECURITY_PAGE_HTML,
    make_detail_html,
    make_gbt_response,
    script_details,
)

DETAIL_PATH = "/kcms2/article/abstract"


def detail_url(env, tag="P0001"):
    return f"{env.kns.base_url}{DETAIL_PATH}?v=abc&filename={tag}&dbcode=CAPJ"


def register_detail(env, responses):
    env.kns.details[DETAIL_PATH] = script_details(responses)


def _detail_with_gbt(env, **kw):
    """详情页 + 指向 mock 的绝对导出地址。"""
    kw.setdefault("export_url", f"{env.kns.base_url}/dm8/API/GetExport")
    return make_detail_html(**kw)


# ========================== 正常全字段 ==========================
def test_fetch_abstract_full_flow(crawler_env):
    register_detail(crawler_env, [_detail_with_gbt(crawler_env)])
    crawler_env.kns.push_export(make_gbt_response([
        "张三, 李四. 测试文献标题[J]. 测试学报, 2024, (1): 1-12.",
    ]))

    rec = crawler.fetch_abstract(detail_url(crawler_env))
    assert rec["title"] == "测试文献标题"
    assert rec["authors"] == ["张三", "李四"]
    assert rec["abstract"].startswith("这是一段用于测试的摘要")
    assert rec["keywords"] == ["个人信贷", "风险管控"]
    assert rec["gbt_citation"].startswith("张三, 李四.")
    assert rec["url"] == detail_url(crawler_env)
    # 全流程成功:限速可回落信号(throttle_ok)被触发,不抬升
    assert crawler.effective_delay() == crawler.throttle_base()


# ========================== JSON 空壳降级 ==========================
def test_fetch_abstract_json_shell_degrades(crawler_env):
    register_detail(crawler_env, [(200, "application/json", '{"error":"busy"}')])
    rec = crawler.fetch_abstract(detail_url(crawler_env))
    # 字段键齐全(避免 CSV 缺列),abstract 承载原始 JSON 摘要
    for key in ("title", "authors", "orgs", "source", "abstract",
                "keywords", "funds", "doi", "album", "topic", "clc_code",
                "publish_time", "url"):
        assert key in rec
    assert rec["abstract"] == "{'error': 'busy'}"
    assert crawler.effective_delay() > crawler.throttle_base()  # JSON 空壳 = 风控信号


# ========================== 安全验证(风控) ==========================
def test_fetch_abstract_security_retry_then_success(crawler_env):
    ok = _detail_with_gbt(crawler_env)
    register_detail(crawler_env, [SECURITY_PAGE_HTML, ok])
    crawler_env.kns.push_export(make_gbt_response(["张三. 题[J]. 刊, 2024."]))

    rec = crawler.fetch_abstract(detail_url(crawler_env))
    assert rec["title"] == "测试文献标题"
    assert crawler_env.sleeps == [8.0]  # 一次 8s 退避
    assert crawler.effective_delay() > crawler.throttle_base()


def test_fetch_abstract_security_twice_raises_runtime_error(crawler_env):
    register_detail(crawler_env, [SECURITY_PAGE_HTML, SECURITY_PAGE_HTML])
    with pytest.raises(RuntimeError, match="连续 2 次触发安全验证"):
        crawler.fetch_abstract(detail_url(crawler_env))
    assert crawler_env.sleeps == [8.0]


# ========================== 登录墙 cookie 续期 ==========================
def test_fetch_abstract_login_wall_recovers_via_refresh(crawler_env, tmp_path, monkeypatch):
    monkeypatch.setattr(crawler, "_DATA_DIR", tmp_path)
    ok = _detail_with_gbt(crawler_env)
    register_detail(crawler_env, [LOGIN_PAGE_HTML, ok])
    crawler_env.kns.push_export(make_gbt_response(["张三. 题[J]. 刊, 2024."]))

    rec = crawler.fetch_abstract(detail_url(crawler_env))
    assert rec["title"] == "测试文献标题"
    assert any(r["path"] == "/" for r in crawler_env.kns.requests_log)  # 续期访问了首页


def test_fetch_abstract_login_wall_refresh_fails_raises_cookie_error(crawler_env, monkeypatch):
    monkeypatch.setattr(crawler, "refresh_cookies", lambda reason="": False)
    register_detail(crawler_env, [LOGIN_PAGE_HTML])
    with pytest.raises(crawler.CnkiCookieError):
        crawler.fetch_abstract(detail_url(crawler_env))


# ========================== 连续空摘要哨兵(模板改版检测) ==========================
def test_fetch_abstract_empty_streak_30_raises_revision(crawler_env):
    base = crawler_env.kns.base_url
    empty = make_detail_html(abstract="", export_url=f"{base}/dm8/API/GetExport")
    register_detail(crawler_env, [empty])
    # export_sticky 默认 {"code":1,"data":[]} → 每篇抛 Missing(前 29 篇)
    url = detail_url(crawler_env, "EMPTY01")
    for i in range(29):
        with pytest.raises(crawler.CnkiGBTCitationMissing):
            crawler.fetch_abstract(url)
    with pytest.raises(crawler.CnkiRevisionError, match="连续 30 篇"):
        crawler.fetch_abstract(url)  # 第 30 篇触发哨兵


def test_fetch_abstract_empty_summary_dumps_sample(crawler_env, tmp_path):
    base = crawler_env.kns.base_url
    empty = make_detail_html(abstract="", export_url=f"{base}/dm8/API/GetExport")
    register_detail(crawler_env, [empty])
    url = detail_url(crawler_env, "EMPTY02")
    with pytest.raises(crawler.CnkiGBTCitationMissing):
        crawler.fetch_abstract(url)
    # 空摘要页面样本已落盘(同 URL 只存一份,供离线分析模板改版)
    dumps = list(tmp_path.glob("debug_abstract_*.html"))
    assert len(dumps) == 1
    assert "测试文献标题" in dumps[0].read_text("utf-8")


# ========================== 哨兵 × GB/T 异常分类共存回归 ==========================
def test_fetch_abstract_sentinel_survives_gbt_api_failed(crawler_env):
    """共存回归(风险修复):空摘要哨兵计数 × GB/T 非 dict JSON 异常分类。

    - 阶段1:摘要空 + 导出端点持续非 dict JSON(Bug B 修复后形态) →
      正确抛 APIFailed 进入重试耗尽传播,而非未分类 AttributeError,
      且哨兵计数保留不清零(清零仅发生在摘要解析成功时);
    - 阶段2:端点恢复(export_sticky 默认缺 data → Missing)后继续累计,
      总空摘要满 30 时哨兵优先抛 RevisionError——
      证明 Bug B 修复不改变"前 29 篇 Missing / 第 30 篇哨兵"路径语义。
    """
    base = crawler_env.kns.base_url
    # 自定义导出路径(避开 MockKNS 对 */GetExport 的内建路由,details 优先命中)
    empty = make_detail_html(abstract="", export_url=f"{base}/gbt-nd")
    register_detail(crawler_env, [empty])
    url = detail_url(crawler_env, "EMPTY03")

    # 阶段1:非 dict JSON → APIFailed 正确分类并传播
    crawler_env.kns.details["/gbt-nd"] = (200, "application/json", '"not-a-dict"')
    with pytest.raises(crawler.CnkiGBTCitationAPIFailed):
        crawler.fetch_abstract(url)  # 哨兵 streak = 1

    # 阶段2:端点改为 Missing 形态(code=1 但无 GB/T 条目),哨兵从 1 继续累计
    # (自定义路径不走内建 GetExport 路由,须显式覆写而非 pop)
    crawler_env.kns.details["/gbt-nd"] = (200, "application/json", '{"code":1,"data":[]}')
    for _ in range(28):
        with pytest.raises(crawler.CnkiGBTCitationMissing):
            crawler.fetch_abstract(url)
    with pytest.raises(crawler.CnkiRevisionError, match="连续 30 篇"):
        crawler.fetch_abstract(url)  # 总空摘要 1+29=30 → 哨兵触发(在 GB/T 之前)
