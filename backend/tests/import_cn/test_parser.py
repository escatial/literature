"""GB/T 7714-2025 引文解析器单元测试。"""
from __future__ import annotations

from import_cn.parser import parse_batch, parse_one


# ── 主格式(最常见) ───────────────────────────────────────────────────────

def test_user_cnki_example():
    """来自用户截图的真实示例。"""
    text = (
        "刘泽宇,姚璐,王倩莹. 混合式学习环境下中职计算机学生的学习行为分析[J]. "
        "信息与电脑, 2025, 37(6): 227-229."
    )
    r = parse_one(text)
    assert r.parsed_ok
    assert r.authors == "刘泽宇,姚璐,王倩莹"
    assert "混合式学习" in r.title
    assert r.journal == "信息与电脑"
    assert r.year == 2025
    assert r.volume == "37"
    assert r.issue == "6"
    assert r.pages == "227-229"
    assert r.raw_text == text.strip()


def test_with_full_volume_no_issue():
    text = "张三, 李四. 某研究的综述[J]. 计算机学报, 2022, 38: 100-110."
    r = parse_one(text)
    assert r.parsed_ok
    assert r.volume == "38"
    assert r.issue is None
    assert r.pages == "100-110"


def test_just_year_no_volume():
    text = "某作者. 某题[J]. 某刊, 2023: 5-10."
    r = parse_one(text)
    assert r.parsed_ok
    assert r.volume is None
    assert r.issue is None
    assert r.pages == "5-10"


def test_only_volume_no_pages():
    text = "某作者. 某题[J]. 某刊, 2020, 12."
    r = parse_one(text)
    assert r.parsed_ok
    assert r.volume == "12"
    assert r.issue is None
    assert r.pages is None


def test_only_pages_no_volume():
    text = "某作者. 某题[J]. 某刊, 2020, 45-50."
    r = parse_one(text)
    assert r.parsed_ok
    assert r.volume is None
    assert r.pages == "45-50"


# ── 异常 / 无效输入 ─────────────────────────────────────────────────────

def test_invalid_format_returns_error():
    text = "这是一段没有格式的文字"
    r = parse_one(text)
    assert not r.parsed_ok
    assert r.error


def test_empty_after_strip_returns_error():
    r = parse_one("   ")
    assert not r.parsed_ok


def test_missing_year_returns_error():
    text = "作者. 题名[J]. 刊名, no-year."
    r = parse_one(text)
    assert not r.parsed_ok


# ── 批量 ────────────────────────────────────────────────────────────────

def test_batch_skips_empty_lines():
    lines = [
        "",
        "刘泽宇,姚璐,王倩莹. 混合式学习环境下中职计算机学生的学习行为分析[J]. "
        "信息与电脑, 2025, 37(6): 227-229.",
        "",
        "bad line",  # 非引文行:跳过
    ]
    results = parse_batch(lines)
    assert len(results) == 1
    assert results[0].parsed_ok


def test_batch_skips_abstract_lines():
    """摘要/关键词/作者简介等非引文行应被跳过。"""
    lines = [
        "[1]吴亮. 高职院校水产市场营销课程思政建设探索[J]. 黑龙江水产, 2025, 44 (5): 668-673.",
        "摘要:全面推进课程思政建设是落实立德树人根本任务的战略举措。",
        "关键词:高职院校;市场营销;课程思政;",
        "[2]陈丽叶,王慧婷. \"互联网+\"背景下福清沙埔镇海产品内容营销策略研究[J]. 农业装备与智能技术, 2025, (2): 42-46.",
        "摘要:以福建省福清市沙埔镇为研究对象。",
    ]
    results = parse_batch(lines)
    assert len(results) == 2
    assert all(r.parsed_ok for r in results)
    assert results[0].authors == "吴亮"
    assert results[1].authors == "陈丽叶,王慧婷"


def test_batch_preserves_raw_text():
    text = "作者A. 题X[J]. 刊Y, 2020, 5(3): 10-20."
    r = parse_one(text)
    assert r.raw_text == text


# ── 知网"查新(引文格式)"带摘要的鲁棒性 ────────────────────────────────

def test_cnki_with_abstract_suffix():
    """知网复制时常把摘要也带上,应只截引文部分解析。"""
    text = (
        "[1]吴亮. 高职院校水产市场营销课程思政建设探索[J]. 黑龙江水产, 2025, 44 (5): 668-673.\n"
        "摘要:全面推进课程思政建设是落实立德树人根本任务的战略举措。文章分析了高职院校水产"
        "市场营销课程思政建设的意义及存在问题,并以厦门海洋职业技术学院为例提出课程思政建设的"
        "具体举措。"
    )
    r = parse_one(text)
    assert r.parsed_ok, r.error
    assert r.authors == "吴亮"
    assert "高职院校水产市场营销课程思政建设探索" in r.title
    assert r.journal == "黑龙江水产"
    assert r.year == 2025
    assert r.volume == "44"
    assert r.issue == "5"
    assert r.pages == "668-673"
    assert "摘要" in r.raw_text  # raw_text 保留原文


def test_cnki_bracket_index_stripped():
    """[N] 编号不影响解析。"""
    text = "[1]吴亮. 高职院校水产市场营销课程思政建设探索[J]. 黑龙江水产, 2025, 44 (5): 668-673."
    r = parse_one(text)
    assert r.parsed_ok
    assert r.authors == "吴亮"
    assert r.year == 2025
