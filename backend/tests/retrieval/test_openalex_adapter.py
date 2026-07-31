"""OpenAlex 适配器单元测试。

不调用真实 API,只测纯函数(摘要反序列化、lit_id 生成、字段解析)。
"""
from __future__ import annotations

import pytest

from retrieval.openalex_adapter import (
    OpenAlexAdapter,
    _make_lit_id,
    _rebuild_abstract,
)
from retrieval.types import Paper, Source


# ── _rebuild_abstract ─────────────────────────────────────────────────────

def test_rebuild_abstract_basic():
    inverted = {"Hello": [0], "world": [1]}
    assert _rebuild_abstract(inverted) == "Hello world"


def test_rebuild_abstract_multiple_positions():
    # 单词在多个位置,实现按 (position, word) 严格排序还原。
    # 位置序列 0,1,2,3,4,5,6,7 → 单词 foo, bar, baz, foo, qux, bar, zap(同一个词出现两
    # 次时,每出现一次仍按当时位置占一格)。
    inverted = {"foo": [0, 3], "bar": [1, 5], "baz": [2], "qux": [4], "zap": [6]}
    assert _rebuild_abstract(inverted) == "foo bar baz foo qux bar zap"


def test_rebuild_abstract_none():
    assert _rebuild_abstract(None) is None
    assert _rebuild_abstract({}) is None


# ── _make_lit_id ──────────────────────────────────────────────────────────

def test_lit_id_deterministic():
    a = _make_lit_id("title A", "10.123/abc")
    b = _make_lit_id("title A", "10.123/abc")
    assert a == b
    assert a.startswith("lit_")
    assert len(a) == 4 + 16


def test_lit_id_diff_on_title():
    a = _make_lit_id("title A", None)
    b = _make_lit_id("title B", None)
    assert a != b


def test_lit_id_diff_on_doi():
    a = _make_lit_id("title", "10.1/x")
    b = _make_lit_id("title", "10.1/y")
    assert a != b


def test_lit_id_handles_none():
    # None 输入不报错
    a = _make_lit_id(None, None)
    assert a.startswith("lit_")
    assert len(a) == 4 + 16


# ── OpenAlexAdapter._parse ────────────────────────────────────────────────

def make_adapter() -> OpenAlexAdapter:
    return OpenAlexAdapter(mailto="test@example.com", timeout=5.0)


def test_parse_extracts_core_fields():
    adapter = make_adapter()
    raw = {
        "id": "https://openalex.org/W123",
        "doi": "https://doi.org/10.1234/test",
        "title": "Deep Learning for Medical Imaging",
        "display_name": None,
        "publication_year": 2023,
        "cited_by_count": 42,
        "authorships": [
            {"author": {"display_name": "Alice Author"}},
            {"author": {"display_name": "Bob Writer"}},
        ],
        "biblio": {
            "volume": "12",
            "issue": "3",
            "first_page": "100",
            "last_page": "110",
        },
        "primary_location": {
            "source": {"display_name": "Journal of AI Research"},
            "landing_page_url": "https://example.org/paper/123",
        },
        "abstract_inverted_index": {
            "We": [0], "study": [1], "deep": [2],
            "learning": [3], "in": [4], "imaging": [5],
        },
    }
    paper = adapter._parse(raw)
    assert paper.lit_id == _make_lit_id("Deep Learning for Medical Imaging", "10.1234/test")
    assert paper.source == Source.OPENALEX
    assert paper.title == "Deep Learning for Medical Imaging"
    assert paper.authors == ["Alice Author", "Bob Writer"]
    assert paper.journal == "Journal of AI Research"
    assert paper.year == 2023
    assert paper.volume == "12"
    assert paper.issue == "3"
    assert paper.pages == "100-110"
    assert paper.abstract == "We study deep learning in imaging"
    assert paper.doi == "10.1234/test"
    assert paper.cited_by_count == 42
    assert paper.source_url == "https://example.org/paper/123"


def test_parse_handles_missing_fields():
    """缺字段不应崩溃,保持 None,不构造。"""
    adapter = make_adapter()
    paper = adapter._parse({})
    assert paper.title == ""
    assert paper.authors == []
    assert paper.year == 0
    assert paper.abstract is None
    assert paper.doi is None
    assert paper.volume is None
    assert paper.issue is None
    assert paper.pages is None


def test_parse_handles_empty_pages():
    """first_page 或 last_page 缺失时,pages 行为合理。"""
    adapter = make_adapter()
    raw = {
        "title": "X", "publication_year": 2020,
        "biblio": {"first_page": "100"},   # 无 last_page
    }
    paper = adapter._parse(raw)
    assert paper.pages == "100"


def test_parse_strips_doi_prefix():
    adapter = make_adapter()
    raw = {"doi": "https://doi.org/10.1234/abc"}
    paper = adapter._parse(raw)
    assert paper.doi == "10.1234/abc"


def test_parse_no_doi():
    adapter = make_adapter()
    raw = {"doi": None}
    paper = adapter._parse(raw)
    assert paper.doi is None


def test_parse_to_dict_json_safe():
    """to_dict 必须 JSON-safe(Source Enum 序列化)。"""
    adapter = make_adapter()
    paper = adapter._parse({"title": "T", "publication_year": 2024})
    d = paper.to_dict()
    assert d["source"] == "openalex"  # Enum.value 字符串
    import json
    json.dumps(d)  # 不应抛异常


# ── OpenAlexAdapter.search(网络) ───────────────────────────────────────────

@pytest.mark.skip(reason="需要联网,默认跳过")
def test_search_real_network():
    adapter = OpenAlexAdapter(mailto="you@example.com")
    papers = adapter.search("transformer", (2020, 2025), per_page=3)
    assert all(isinstance(p, Paper) for p in papers)
    assert len(papers) > 0
