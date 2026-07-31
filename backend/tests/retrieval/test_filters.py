"""filters.py 单元测试。"""
from __future__ import annotations

from retrieval.filters import (
    by_journal_level,
    by_min_citations,
    by_year,
    deduplicate,
)
from retrieval.types import Paper, Source


def make(lit_id: str = "lit_x", **kw) -> Paper:
    defaults = dict(
        lit_id=lit_id,
        source=Source.OPENALEX,
        title="T",
        authors=["A"],
        journal="J",
        year=2024,
    )
    defaults.update(kw)
    return Paper(**defaults)


def test_by_year_within_range():
    papers = [
        make(lit_id="lit_a", year=2019),
        make(lit_id="lit_b", year=2020),
        make(lit_id="lit_c", year=2023),
        make(lit_id="lit_d", year=2027),
    ]
    out = by_year(papers, (2020, 2025))
    assert {p.lit_id for p in out} == {"lit_b", "lit_c"}


def test_by_year_excludes_zero_year():
    papers = [make(lit_id="lit_a", year=0), make(lit_id="lit_b", year=2022)]
    out = by_year(papers, (2020, 2025))
    assert {p.lit_id for p in out} == {"lit_b"}


def test_by_min_citations_filter():
    papers = [
        make(lit_id="lit_a", cited_by_count=0),
        make(lit_id="lit_b", cited_by_count=5),
        make(lit_id="lit_c", cited_by_count=15),
    ]
    out = by_min_citations(papers, 5)
    assert {p.lit_id for p in out} == {"lit_b", "lit_c"}
    out0 = by_min_citations(papers, 0)
    assert len(out0) == 3
    out_neg = by_min_citations(papers, -1)
    assert len(out_neg) == 3


def test_by_journal_level():
    papers = [
        make(lit_id="a", journal_level="SCI"),
        make(lit_id="b", journal_level="SSCI"),
        make(lit_id="c", journal_level=None),
    ]
    out = by_journal_level(papers, {"SCI", "AHCI"})
    assert {p.lit_id for p in out} == {"a"}
    out_empty = by_journal_level(papers, set())
    assert len(out_empty) == 3


def test_deduplicate_by_doi_and_title():
    papers = [
        make(lit_id="lit_1", doi="10.1/x", title="Same"),
        make(lit_id="lit_2", doi="10.1/x", title="Same"),  # DOI 相同
        make(lit_id="lit_3", doi=None, title="Other", authors=["A"], year=2024),
        make(lit_id="lit_4", doi=None, title="Other", authors=["A"], year=2024),  # 复合键相同
        make(lit_id="lit_5", doi=None, title="Other", authors=["A"], year=2025),  # 年份不同
    ]
    out = deduplicate(papers)
    assert {p.lit_id for p in out} == {"lit_1", "lit_3", "lit_5"}


def test_deduplicate_keeps_first():
    papers = [
        make(lit_id="lit_first", doi="10.1/x", title="X"),
        make(lit_id="lit_duplicate", doi="10.1/x", title="X"),
    ]
    out = deduplicate(papers)
    assert len(out) == 1
    assert out[0].lit_id == "lit_first"
