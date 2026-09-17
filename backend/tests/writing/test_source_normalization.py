from src.retrieval.types import Source, canonical_publication_year, normalize_source
from src.retrieval.types import Paper
from src.writing.classifier import Group
from src.writing.orchestrator import _sanitize_confirmed_groups
from src.retrieval.provenance import derive_paper_provenance


def test_normalize_source_accepts_enum_display_string():
    assert normalize_source("Source.OPENALEX") is Source.OPENALEX
    assert normalize_source("openalex") is Source.OPENALEX
    assert normalize_source(Source.PUBMED) is Source.PUBMED


def test_derive_provenance_from_official_history_url():
    result = derive_paper_provenance(
        "pubmed", "lit_pubmed_abc", "https://pubmed.ncbi.nlm.nih.gov/12345678/"
    )
    assert result is not None
    assert result["record_id"] == "12345678"
    assert result["derived"] is True


def test_confirmed_groups_assign_each_paper_once():
    papers = [
        Paper(lit_id="a", source=Source.OPENALEX, title="A", authors=[], journal="J", year=2024, abstract="x"),
        Paper(lit_id="b", source=Source.OPENALEX, title="B", authors=[], journal="J", year=2024, abstract="x"),
    ]
    groups = _sanitize_confirmed_groups(
        [Group(name="甲", lit_ids=["a", "b"]), Group(name="乙", lit_ids=["a"])],
        papers,
    )
    assert groups[0].lit_ids == ["a", "b"]
    assert len(groups) == 1


def test_cnki_raw_citation_publication_year_overrides_dirty_snapshot_year():
    raw = (
        "胡大伟,张世鹏.应急响应初期联合配送路径问题[J]."
        "长安大学学报,2024,44(1):105-119."
    )
    assert canonical_publication_year(Source.CNKI, 2023, raw) == 2024
    paper = Paper(
        lit_id="cnki-year", source=Source.CNKI, title="题名",
        authors=["胡大伟"], journal="期刊", year=2023,
        abstract="摘要", raw_citation=raw,
    )
    assert paper.year == 2024


def test_cnki_access_date_is_not_mistaken_for_publication_year():
    raw = (
        "作者.在线优先论文[J/OL].中国管理科学,1-12"
        "[2026-09-09].https://example.invalid."
    )
    assert canonical_publication_year(Source.CNKI, 2025, raw) == 2025
