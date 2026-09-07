# -*- coding: utf-8 -*-
"""单元测试:解析层(列表 parse_list / 详情 _parse_detail / GB/T 引文 / 完整性校验)。

注意:
- _fetch_gbt_citation 序号剥离用例按"正确行为"断言(剥 $[N]/[N] 前缀),
  若实现存在双反斜杠正则失效,用例红 → 暴露 Bug A。
- 导出地址必须是绝对 URL(requests 不接受相对路径)。
"""
import json

import pytest

from automation.cnki import crawler
from helpers import (
    LOGIN_PAGE_HTML,
    SECURITY_PAGE_HTML,
    VERICODE_PAGE_HTML,
    make_detail_html,
    make_gbt_response,
    make_list_html,
    make_list_rows,
    script_details,
)


def gbt_detail(env, **kw):
    """构造带绝对 GB/T 导出地址的详情页(指向 mock 服务器)。"""
    kw.setdefault("export_url", f"{env.kns.base_url}/dm8/API/GetExport")
    return make_detail_html(**kw)


# ========================== parse_list ==========================
def test_parse_list_normal_rows(crawler_env):
    rows = make_list_rows("信贷", 3)
    items = crawler.parse_list(make_list_html(rows, total=100))
    assert len(items) == 3
    first = items[0]
    assert first["title"] == "信贷研究0001"
    assert "/kcms2/article/abstract" in first["url"]
    assert first["url"].startswith(crawler.CONFIG["endpoints"]["base"])  # 相对路径补 base
    # quote 剥掉 $[N] 序号
    assert first["quote_text"].startswith("张三")
    assert not first["quote_text"].startswith("$")
    assert "[1]" not in first["quote_text"][:6]


def test_parse_list_row_without_quote_cell(crawler_env):
    rows = [("无引文行", "/kcms2/article/abstract?v=x&filename=X1&dbcode=CAPJ", None)]
    items = crawler.parse_list(make_list_html(rows))
    assert len(items) == 1
    assert items[0]["quote_text"] == ""


def test_parse_list_skips_rows_without_fz14_link(crawler_env):
    html = (
        '<html><body><table id="gridTable">'
        '<tr><td class="name"><a href="/x">非 fz14 链接</a></td><td class="quote">q</td></tr>'
        "</table></body></html>"
    )
    assert crawler.parse_list(html) == []


def test_parse_list_empty_page(crawler_env):
    assert crawler.parse_list("<html><body></body></html>") == []


# ========================== _parse_detail ==========================
def test_parse_detail_full_fields(crawler_env):
    html = make_detail_html(
        title="深度测试文献",
        authors=("张三", "李四", "王五"),
        orgs=("测试大学经济学院", "某研究院"),
        abstract="这是完整摘要正文。",
        keywords=("关键词A", "关键词B"),
        funds=("国家社科基金(22BJY001)",),
        doi="10.9999/full.001",
    )
    d = crawler._parse_detail(html, "http://detail/x")
    assert d["title"] == "深度测试文献"
    assert d["authors"] == ["张三", "李四", "王五"]  # sup 上标已剥离
    assert d["orgs"] == ["测试大学经济学院", "某研究院"]  # 序号前缀已剥离
    assert d["abstract"] == "这是完整摘要正文。"
    assert d["keywords"] == ["关键词A", "关键词B"]  # 尾分号已剥离
    assert d["funds"] == ["国家社科基金(22BJY001)"]
    assert d["doi"] == "10.9999/full.001"
    assert d["album"] == "经济与管理"
    assert d["topic"] == "金融"
    assert d["clc_code"] == "F830"
    assert d["publish_time"] == "2024-01-15"
    assert d["source"] == "测试学报"  # navi.cnki.net 刊名链接
    assert d["url"] == "http://detail/x"


def test_parse_detail_abstract_input_variant(crawler_env):
    # kcms2 变体:摘要藏在 input#abstract_text 的 value
    html = make_detail_html(abstract_text="隐藏在 input 里的摘要")
    d = crawler._parse_detail(html, "http://d/y")
    assert d["abstract"] == "隐藏在 input 里的摘要"


def test_parse_detail_meta_description_fallback(crawler_env):
    # 三代模板选择器全落空 → meta description 兜底
    html = (
        '<html><head><meta name="description" content="meta 兜底摘要"/></head>'
        "<body><div class='wx-tit'><h1>兜底标题</h1></div></body></html>"
    )
    d = crawler._parse_detail(html, "http://d/z")
    assert d["abstract"] == "meta 兜底摘要"
    assert d["title"] == "兜底标题"


# ========================== _extract_hidden ==========================
def test_extract_hidden_id_before_value():
    html = '<input id="export-url" type="hidden" value="/dm8/API/GetExport"/>'
    assert crawler._extract_hidden(html, "export-url") == "/dm8/API/GetExport"


def test_extract_hidden_value_before_id():
    html = '<input type="hidden" value="P12345" id="export-id"/>'
    assert crawler._extract_hidden(html, "export-id") == "P12345"


def test_extract_hidden_missing():
    assert crawler._extract_hidden("<html></html>", "export-url") == ""


# ========================== _fetch_gbt_citation ==========================
def test_fetch_gbt_missing_hidden_raises_missing(crawler_env):
    html = make_detail_html(with_gbt_hidden=False)
    with pytest.raises(crawler.CnkiGBTCitationMissing):
        crawler._fetch_gbt_citation(html)


def test_fetch_gbt_success_strips_index_prefix(crawler_env):
    """Bug A 回归用例:$[N]/[N] 序号前缀必须剥离。"""
    crawler_env.kns.push_export(make_gbt_response([
        "$[1] 张三. 论文一[J]. 测试学报, 2024, (1): 1-10.",
        "[2] 李四. 论文二[M]. 出版社, 2023.",
    ]))
    got = crawler._fetch_gbt_citation(gbt_detail(crawler_env))
    lines = got.split("\n")
    assert lines[0] == "张三. 论文一[J]. 测试学报, 2024, (1): 1-10."  # $[1] 已剥
    assert lines[1] == "李四. 论文二[M]. 出版社, 2023."                # [2] 已剥


def test_fetch_gbt_br_to_newline_and_tag_strip(crawler_env):
    crawler_env.kns.push_export(make_gbt_response([
        "作者. 题[J]. 刊, 2024.<br/><span>噪音</span>",
    ]))
    got = crawler._fetch_gbt_citation(gbt_detail(crawler_env))
    assert got == "作者. 题[J]. 刊, 2024.\n噪音"


def test_fetch_gbt_filters_non_gbt_keys(crawler_env):
    body = {
        "code": 1,
        "data": [
            {"key": "BibTeX", "value": ["@article{noise}"]},
            {"key": "GB/T 7714-2025", "value": ["正确条目"]},
        ],
    }
    crawler_env.kns.push_export(body)
    assert crawler._fetch_gbt_citation(gbt_detail(crawler_env)) == "正确条目"


def test_fetch_gbt_code_not_1_raises_api_failed(crawler_env):
    crawler_env.kns.push_export({"code": -1, "msg": "拒绝"})
    with pytest.raises(crawler.CnkiGBTCitationAPIFailed):
        crawler._fetch_gbt_citation(gbt_detail(crawler_env))


def test_fetch_gbt_no_gbt_entry_raises_missing(crawler_env):
    crawler_env.kns.push_export({"code": 1, "data": []})
    with pytest.raises(crawler.CnkiGBTCitationMissing):
        crawler._fetch_gbt_citation(gbt_detail(crawler_env))


def test_fetch_gbt_non_json_body_raises_api_failed(crawler_env):
    """导出 API 返回 HTML 错误页(非 JSON)必须分类为 APIFailed。"""
    base = crawler_env.kns.base_url
    crawler_env.kns.details["/gbt-html"] = (200, "text/html", "<html>Service Unavailable</html>")
    html = make_detail_html(export_url=f"{base}/gbt-html")
    with pytest.raises(crawler.CnkiGBTCitationAPIFailed):
        crawler._fetch_gbt_citation(html)


def test_fetch_gbt_http_500_raises_api_failed(crawler_env):
    base = crawler_env.kns.base_url
    crawler_env.kns.details["/gbt-500"] = (500, "text/plain", "server error")
    html = make_detail_html(export_url=f"{base}/gbt-500")
    with pytest.raises(crawler.CnkiGBTCitationAPIFailed):
        crawler._fetch_gbt_citation(html)


def test_fetch_gbt_json_string_payload_raises_api_failed(crawler_env):
    """Bug B 回归用例:响应是合法 JSON 但非 dict(如纯字符串)→ 应分类为 APIFailed,
    而不是抛未分类 AttributeError 打穿重试封装。"""
    base = crawler_env.kns.base_url
    crawler_env.kns.details["/gbt-str"] = (200, "application/json", '"just-a-string"')
    html = make_detail_html(export_url=f"{base}/gbt-str")
    with pytest.raises(crawler.CnkiGBTCitationAPIFailed):
        crawler._fetch_gbt_citation(html)


# ========================== 回归补充:Bug A/B 同类形态 ==========================
@pytest.mark.parametrize("raw,expected", [
    ("$[1] 条目甲", "条目甲"),        # $[N] 单位数(标准形态)
    ("$[12] 条目乙", "条目乙"),       # $[N] 多位数
    ("[3] 条目丙", "条目丙"),         # [N] 无 $ 变体
    ("条目丁无序号", "条目丁无序号"),  # 无前缀:原样直通不误伤
])
def test_fetch_gbt_index_prefix_variants(crawler_env, raw, expected):
    """Bug A 同类回归:各种序号前缀形态都必须正确剥离且不伤正文。"""
    crawler_env.kns.push_export(make_gbt_response([raw]))
    assert crawler._fetch_gbt_citation(gbt_detail(crawler_env)) == expected


@pytest.mark.parametrize("payload", ['[1,2]', 'null', '3.14', 'true'])
def test_fetch_gbt_non_dict_json_variants_raise_api_failed(crawler_env, payload):
    """Bug B 同类回归:一切"合法 JSON 但非对象(dict)"的响应(list/null/数值/布尔)
    都必须分类为 APIFailed 进入重试,而不是抛未分类异常打穿重试封装。"""
    base = crawler_env.kns.base_url
    crawler_env.kns.details["/gbt-nd"] = (200, "application/json", payload)
    html = make_detail_html(export_url=f"{base}/gbt-nd")
    with pytest.raises(crawler.CnkiGBTCitationAPIFailed):
        crawler._fetch_gbt_citation(html)


# ========================== fetch_gbt_citation_with_retry ==========================
def test_gbt_retry_missing_passthrough_no_retry(crawler_env):
    crawler_env.kns.push_export({"code": 1, "data": []})
    with pytest.raises(crawler.CnkiGBTCitationMissing):
        crawler.fetch_gbt_citation_with_retry(gbt_detail(crawler_env))
    assert crawler_env.sleeps == []  # Missing 不退避


def test_gbt_retry_api_failed_backoff_then_success(crawler_env):
    """APIFailed 按 2/4/8s 指数退避;第 3 次成功 → sleeps 恰为 [2,4]。"""
    base = crawler_env.kns.base_url
    ok_body = (200, "application/json", json.dumps(make_gbt_response(["最终成功条目"]), ensure_ascii=False))
    crawler_env.kns.details["/gbt-flaky"] = script_details([
        (500, "text/plain", "err"), (500, "text/plain", "err"), ok_body,
    ])
    html = make_detail_html(export_url=f"{base}/gbt-flaky")
    got = crawler.fetch_gbt_citation_with_retry(html)
    assert got == "最终成功条目"
    assert crawler_env.sleeps == [2.0, 4.0]
    # 每次 APIFailed 都抬升限速(2 次)
    assert crawler.effective_delay() > crawler.throttle_base()


def test_gbt_retry_exhausted_raises_api_failed(crawler_env):
    base = crawler_env.kns.base_url
    crawler_env.kns.details["/gbt-dead"] = (500, "text/plain", "always down")
    html = make_detail_html(export_url=f"{base}/gbt-dead")
    with pytest.raises(crawler.CnkiGBTCitationAPIFailed):
        crawler.fetch_gbt_citation_with_retry(html)
    # gbt_api_retries=4 → 退避 3 次:2/4/8
    assert crawler_env.sleeps == [2.0, 4.0, 8.0]


# ========================== _record_complete ==========================
@pytest.mark.parametrize("record,expected", [
    ({"gbt_citation": "有", "abstract": "有"}, True),
    ({"gbt_citation": "", "abstract": "有"}, False),
    ({"gbt_citation": "有", "abstract": ""}, False),
    ({"gbt_citation": "有", "abstract": "  "}, False),  # 纯空白算空
    ({}, False),
])
def test_record_complete(record, expected):
    assert crawler._record_complete(record) is expected
