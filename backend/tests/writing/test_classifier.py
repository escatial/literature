"""classifier 单元测试。"""

from unittest.mock import patch

import pytest

from src.retrieval.types import Paper, Source
from src.writing.classifier import classify, classify_by_locale, classify_by_theme


def _paper(lit_id: str, source: Source) -> Paper:
    return Paper(
        lit_id=lit_id,
        source=source,
        title=f"title-{lit_id}",
        authors=["A"],
        journal="J",
        year=2024,
        volume=None,
        issue=None,
        pages=None,
        abstract=None,
        doi=None,
        source_url=None,
        cited_by_count=None,
    )


class TestClassifyByLocale:
    def test_split_domestic_and_foreign(self):
        papers = [
            _paper("lit_a", Source.OPENALEX),
            _paper("lit_b", Source.USER_IMPORTED),
            _paper("lit_c", Source.CROSSREF),
            _paper("lit_d", Source.USER_IMPORTED),
        ]
        groups = classify_by_locale(papers)
        assert len(groups) == 2
        assert groups[0].name == "国内研究"
        assert set(groups[0].lit_ids) == {"lit_b", "lit_d"}
        assert groups[1].name == "国外研究"
        assert set(groups[1].lit_ids) == {"lit_a", "lit_c"}

    def test_only_domestic(self):
        papers = [_paper("lit_a", Source.USER_IMPORTED)]
        groups = classify_by_locale(papers)
        assert len(groups) == 1
        assert groups[0].name == "国内研究"

    def test_only_foreign(self):
        papers = [_paper("lit_a", Source.OPENALEX)]
        groups = classify_by_locale(papers)
        assert len(groups) == 1
        assert groups[0].name == "国外研究"


class TestClassifyByTheme:
    def test_empty(self):
        assert classify_by_theme([], "topic") == []

    def test_normal_response(self):
        papers = [_paper("lit_a", Source.OPENALEX), _paper("lit_b", Source.OPENALEX)]
        with patch(
            "src.writing.classifier.messages_create",
            return_value='[{"theme": "主题1", "lit_ids": ["lit_a", "lit_b"]}]',
        ):
            groups = classify_by_theme(papers, "topic")
        assert len(groups) == 1
        assert groups[0].name == "主题1"
        assert set(groups[0].lit_ids) == {"lit_a", "lit_b"}

    def test_llm_failure_fallback(self):
        papers = [_paper("lit_a", Source.OPENALEX)]
        with patch(
            "src.writing.classifier.messages_create", side_effect=RuntimeError("boom")
        ):
            groups = classify_by_theme(papers, "topic")
        assert len(groups) == 1
        assert groups[0].name == "综合研究"
        assert groups[0].lit_ids == ["lit_a"]

    def test_unknown_lit_id_skipped_and_rest_grouped(self):
        papers = [_paper("lit_a", Source.OPENALEX), _paper("lit_b", Source.OPENALEX)]
        with patch(
            "src.writing.classifier.messages_create",
            return_value='[{"theme": "T", "lit_ids": ["lit_a", "lit_hallucinated"]}]',
        ):
            groups = classify_by_theme(papers, "topic")
        assert groups[0].lit_ids == ["lit_a"]
        # lit_b 未被覆盖 → 进入"其他"
        assert groups[-1].name == "其他"
        assert groups[-1].lit_ids == ["lit_b"]


class TestClassify:
    def test_invalid_mode(self):
        with pytest.raises(ValueError):
            classify([], "topic", "bogus")
