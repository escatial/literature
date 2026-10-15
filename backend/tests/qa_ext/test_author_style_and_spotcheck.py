# -*- coding: utf-8 -*-
"""夹注作者表达一致性 + 同名同年预警 + 抽查回源测试(v9.10)。

前两项对应 Kimi literature-review 流程的成熟实践:
- 作者人数表达(2 人用"和/and",3 人以上用"等/et al.")必须与文献真实
  作者数一致,否则读者会误判文献规模;
- 池内同名同年多篇时,正文夹注("张三(2023)")无法唯一区分,应提前预警。
第三项是"至少3次核查且有记录"的第 3 次:成稿后固定种子抽查回源,
题名逐字比对,网络失败留痕不冒充通过。
"""
import json
import urllib.error

import pytest

import qa.author_style as author_style
import qa.spotcheck as spotcheck
from qa.accuracy import check_citation_accuracy
from qa.author_style import check_author_et_al, check_same_author_year
from retrieval.types import Paper, Source
from writing.orchestrator import SectionResult


def _paper(
    lit_id, authors, year=2024, *, title="题名", source=Source.CNKI, doi=None,
):
    return Paper(
        lit_id=lit_id, source=source, title=title, authors=authors,
        journal="测试学报", year=year, abstract="摘要", doi=doi,
    )


def _section(content, citations, key="theme_1"):
    return SectionResult(key=key, title="章节", content=content, citations=citations)


# ---------- 等/和 与真实作者数一致性 ----------

def test_two_authors_cited_with_et_al_flagged():
    """两人文献被写成"等" → 与真实作者数不符,告警。"""
    papers = [_paper("lit_a", ["张三", "李四"])]
    s = _section("张三和李四等（2024）指出有效[1]。", ["lit_a"])
    result = check_author_et_al([s], papers)
    codes = [i.code for i in result.issues]
    assert "AUTHOR_STYLE_MISMATCH" in codes


def test_three_authors_cited_with_et_al_passes():
    papers = [_paper("lit_a", ["张三", "李四", "王五"])]
    s = _section("张三等（2024）指出有效[1]。", ["lit_a"])
    result = check_author_et_al([s], papers)
    assert result.issues == []
    assert result.metrics["citations_checked"] == 1


def test_two_authors_with_he_passes():
    papers = [_paper("lit_a", ["张三", "李四"])]
    s = _section("张三和李四（2024）指出有效[1]。", ["lit_a"])
    result = check_author_et_al([s], papers)
    assert result.issues == []


def test_en_three_authors_et_al_passes_and_two_authors_and_flagged():
    papers = [_paper("lit_a", ["Smith J", "Jones K", "Lee M"], source=Source.OPENALEX)]
    ok = _section("Smith et al.（2024）found gains[1]。", ["lit_a"])
    assert check_author_et_al([ok], papers).issues == []
    bad = _section("Smith and Jones（2024）found gains[1]。", ["lit_a"])
    result = check_author_et_al([bad], papers)
    assert any(i.code == "AUTHOR_STYLE_MISMATCH" for i in result.issues)


def test_single_author_cited_correctly():
    papers = [_paper("lit_a", ["张三"])]
    s = _section("张三（2024）指出有效[1]。", ["lit_a"])
    assert check_author_et_al([s], papers).issues == []


# ---------- 同名同年预警 ----------

def test_same_author_year_warns():
    """同一第一作者同年两篇不同题名 → 夹注无法唯一区分,必须预警。"""
    papers = [
        _paper("lit_a", ["张三", "李四"], year=2023, title="题目甲"),
        _paper("lit_b", ["张三", "王五"], year=2023, title="题目乙"),
    ]
    result = check_citation_accuracy(papers)
    codes = [i.code for i in result.issues]
    assert "ACCURACY_SAME_AUTHOR_YEAR" in codes
    issue = next(i for i in result.issues if i.code == "ACCURACY_SAME_AUTHOR_YEAR")
    assert issue.severity.value == "warn"


def test_same_author_different_year_no_warn():
    papers = [
        _paper("lit_a", ["张三"], year=2023, title="题目甲"),
        _paper("lit_b", ["张三"], year=2024, title="题目乙"),
    ]
    result = check_citation_accuracy(papers)
    assert all(i.code != "ACCURACY_SAME_AUTHOR_YEAR" for i in result.issues)


# ---------- 固定种子抽查回源 ----------

def test_spotcheck_seed_stable(monkeypatch):
    papers = [
        _paper(f"lit_{i:03d}", ["张三"], doi=f"10.1000/test{i}", source=Source.OPENALEX)
        for i in range(40)
    ]
    monkeypatch.delenv("QA_SPOTCHECK_SEED", raising=False)
    seed_a = spotcheck._stable_seed(papers)
    seed_b = spotcheck._stable_seed(papers)
    assert seed_a == seed_b
    monkeypatch.setenv("QA_SPOTCHECK_SEED", "42")
    assert spotcheck._stable_seed(papers) == 42


def test_spotcheck_matches_and_mismatches(monkeypatch):
    papers = [
        _paper("lit_ok", ["张三"], doi="10.1000/ok", source=Source.OPENALEX,
               title="Aquaculture marketing strategies"),
        _paper("lit_bad", ["李四"], doi="10.1000/bad", source=Source.OPENALEX,
               title="Local fish market survey"),
    ]
    monkeypatch.setenv("QA_SPOTCHECK_SEED", "7")

    def fake_fetch(doi):
        if doi.endswith("ok"):
            return "Aquaculture Marketing Strategies"  # 归一化后一致
        return "A completely different paper title"

    monkeypatch.setattr(spotcheck, "_fetch_crossref_title", fake_fetch)
    result = spotcheck.check_source_spotcheck(papers)
    assert result.metrics["sampled"] == 2
    assert result.metrics["matched"] >= 1
    assert result.metrics["mismatched"] >= 1
    assert any(i.code == "SPOTCHECK_TITLE_MISMATCH" for i in result.issues)
    assert result.status.value == "warn"
    # 留痕:records 里两条都有核查状态
    statuses = {r["lit_id"]: r["status"] for r in result.metrics["records"]}
    assert statuses == {"lit_ok": "matched", "lit_bad": "mismatched"}


def test_spotcheck_unreachable_not_faked_as_pass(monkeypatch):
    """网络不可达时留痕为 unreachable,不冒充通过。"""
    papers = [_paper("lit_a", ["张三"], doi="10.1000/x", source=Source.OPENALEX)]

    def boom(doi):
        raise urllib.error.URLError("proxy down")

    monkeypatch.setattr(spotcheck, "_fetch_crossref_title", boom)
    result = spotcheck.check_source_spotcheck(papers)
    assert result.metrics["unreachable"] == 1
    assert result.metrics["records"][0]["status"] == "unreachable"
    assert result.status.value == "warn"


def test_spotcheck_skipped_without_doi():
    papers = [_paper("lit_a", ["张三"], source=Source.CNKI)]
    result = spotcheck.check_source_spotcheck(papers)
    assert result.status.value == "skipped"
