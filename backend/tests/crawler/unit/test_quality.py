# -*- coding: utf-8 -*-
"""单元测试:数据质量保障(标准化/确定性补全/完整性评分/管线聚合)。"""
from automation.cnki.quality import (
    QualityReport,
    aggregate_score,
    assess,
    fill_missing,
    normalize_fields,
    quality_pipeline,
)


def _perfect_record():
    """全字段合规的样本(断言满分基线)。"""
    return {
        "title": "分布式系统一致性协议研究",
        "authors": ["张三", "李四"],
        "abstract": "本文研究了分布式系统中一致性问题,提出了一种新的协议并完成实验验证与分析。" * 1,
        "journal": "计算机学报",
        "year": 2020,
        "doi": "10.1234/j.abc.2020.001",
        "source_url": "https://kns.cnki.net/kcms2/detail/xx.html",
    }


# ========================== 字段标准化 ==========================
def test_normalize_only_existing_keys():
    """只清洗已有键:绝不为 record 新增键(与 DB 列约束兼容)。"""
    record = {"title": "  深度\t学习  综述  "}
    changed = normalize_fields(record)
    assert changed == ["title"]
    assert record["title"] == "深度 学习 综述"
    assert set(record.keys()) == {"title"}


def test_normalize_text_fields_strip_tags_and_entities():
    record = {
        "title": "干净标题",
        "abstract": "<p>你好<br>世界&nbsp;&amp;更多</p>",
        "abstract_text": "  折叠   空白  ",
        "journal": "计算机\t学报",
        "quote_text": "引文  内容",
    }
    changed = normalize_fields(record)
    assert set(changed) == {"abstract", "abstract_text", "journal", "quote_text"}
    assert record["abstract"] == "你好世界 &更多"      # 标签删除 + 实体还原 + 空白折叠
    assert record["abstract_text"] == "折叠 空白"
    assert record["title"] == "干净标题"               # 无变化不报


def test_normalize_raw_citation_keeps_line_structure():
    record = {"raw_citation": "张三. 标题[J].   刊名,  2020.\n  第二行  引文 "}
    changed = normalize_fields(record)
    assert changed == ["raw_citation"]
    assert record["raw_citation"] == "张三. 标题[J]. 刊名, 2020.\n第二行 引文"


def test_normalize_authors_dedup_and_clean():
    record = {"authors": ["  张三 ", "", "张三", "李四\t"]}
    changed = normalize_fields(record)
    assert changed == ["authors"]
    assert record["authors"] == ["张三", "李四"]       # 去空、去重、保序


def test_normalize_doi_lowercase_and_prefix_strip():
    record = {"doi": " HTTPS://DOI.ORG/10.1234/ABC.X "}
    assert normalize_fields(record) == ["doi"]
    assert record["doi"] == "10.1234/abc.x"
    record2 = {"doi": "doi：10.1/XYZ"}
    normalize_fields(record2)
    assert record2["doi"] == "10.1/xyz"


# ========================== 确定性补全 ==========================
def test_fill_missing_doi_from_citation():
    record = {"raw_citation": "张三. 标题[J]. 刊名, 2020. DOI:10.1000/j.abc.2020.1."}
    filled = fill_missing(record)
    assert filled == ["doi"]
    assert record["doi"] == "10.1000/j.abc.2020.1"    # 尾部标点已剥除


def test_fill_missing_no_match_no_invention():
    record = {"raw_citation": "没有 DOI 的普通引文"}
    assert fill_missing(record) == []
    assert "doi" not in record                         # 提取不到绝不编造
    record2 = {"doi": "10.1/exists", "raw_citation": "DOI:10.2/other"}
    assert fill_missing(record2) == []                 # 已有值不覆盖


# ========================== 完整性校验与评分 ==========================
def test_assess_perfect_record_full_score():
    report = assess(_perfect_record(), now_year=2026)
    assert report.score == 100
    assert report.flags == []


def test_assess_empty_record_zero_score_all_flags():
    report = assess({}, now_year=2026)
    assert report.score == 0
    assert set(report.flags) == {
        "title_too_short", "authors_empty", "missing_abstract",
        "missing_journal", "suspicious_year", "missing_traceable",
    }


def test_assess_partial_fields_score_math():
    record = _perfect_record()
    record["abstract"] = "太短"                        # 1-29 字:不满足也不算缺失
    report = assess(record, now_year=2026)
    assert report.flags == ["abstract_too_short"]
    assert report.score == 70                          # 100 - 25(abstract) - 5(flag)

    record2 = _perfect_record()
    record2["doi"] = "not-a-doi"                       # 有值但非法:丢溯源分 + flag
    report2 = assess(record2, now_year=2026)
    assert report2.flags == ["doi_format_invalid"]
    assert report2.score == 85                         # 100 - 10 - 5


def test_assess_year_boundary():
    base = _perfect_record()
    for year, ok in ((1900, True), (2026, True), (2027, True), (1899, False), (2028, False), ("2020", False)):
        record = dict(base)
        record["year"] = year
        report = assess(record, now_year=2026)
        assert ("suspicious_year" in report.flags) is (not ok), f"year={year!r}"


def test_assess_source_url_as_traceable_fallback():
    record = _perfect_record()
    record.pop("doi")
    report = assess(record, now_year=2026)             # 无 DOI 但有 source_url:可溯源
    assert report.score == 100
    assert report.flags == []


# ========================== 一站式管线与聚合 ==========================
def test_quality_pipeline_in_place_and_no_flags_leak():
    record = {"title": "  Test   Title ", "raw_citation": "引文 DOI:10.1234/abcdef"}
    result, report = quality_pipeline(record, now_year=2026)
    assert result is record                            # 原地修改
    assert set(record.keys()) == {"title", "raw_citation", "doi"}  # 补全 doi,flags 不入 record
    assert report.filled == ["doi"]
    assert "title" in report.normalized
    assert report.score >= 0


def test_quality_report_to_dict():
    report = QualityReport(score=88, flags=["missing_doi"], filled=["doi"], normalized=["title"])
    data = report.to_dict()
    assert set(data.keys()) == {"score", "flags", "filled", "normalized", "ts"}
    assert data["flags"] == ["missing_doi"]            # 拷贝而非引用


def test_aggregate_score():
    empty = aggregate_score([])
    assert empty == {"count": 0, "avg_score": 0, "flags": {}, "min_score": 0}
    reports = [
        QualityReport(score=90, flags=["missing_doi"]),
        QualityReport(score=80, flags=["missing_doi", "missing_journal"]),
    ]
    agg = aggregate_score(reports)
    assert agg["count"] == 2
    assert agg["avg_score"] == 85.0
    assert agg["min_score"] == 80
    assert agg["flags"] == {"missing_doi": 2, "missing_journal": 1}
