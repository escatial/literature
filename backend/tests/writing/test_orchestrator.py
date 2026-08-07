"""orchestrator 单元测试。"""

from unittest.mock import patch

from src.retrieval.types import Paper, Source
from src.writing.classifier import Group
from src.writing.orchestrator import build_review_sections, generate_review, render_reference_list
from src.writing.section_writer import SectionResult


def _paper(lit_id: str, source: Source = Source.OPENALEX, raw_citation: str | None = None) -> Paper:
    return Paper(
        lit_id=lit_id,
        source=source,
        title=f"T-{lit_id}",
        authors=["A"],
        journal="J",
        year=2024,
        volume="1",
        issue="2",
        pages="10-20",
        abstract=None,
        doi=None,
        source_url="",
        cited_by_count=0,
        raw_citation=raw_citation,
    )


_LIT_A = "lit_" + "a" * 16
_LIT_B = "lit_" + "b" * 16


class TestBuildReviewSections:
    def test_theme_mode_uses_parallel_theme_sections_without_search_method(self):
        groups = [
            Group(name="消费行为与需求偏好", lit_ids=[_LIT_A]),
            Group(name="数字营销与传播路径", lit_ids=[_LIT_B]),
            Group(name="品牌建设与价值提升", lit_ids=[_LIT_A, _LIT_B]),
        ]
        sections = build_review_sections("theme", groups)
        titles = [s.title for s in sections]

        assert len([s for s in sections if s.key.startswith("theme_")]) == 3
        assert "二、消费行为与需求偏好" in titles
        assert "三、数字营销与传播路径" in titles
        assert "四、品牌建设与价值提升" in titles
        assert all("检索" not in s.title and "检索" not in s.instruction for s in sections)
        assert all("方法" not in s.title for s in sections)


class TestGenerateReview:
    def test_happy_path_with_screening(self):
        papers = [_paper(_LIT_A), _paper(_LIT_B)]
        with patch(
            "src.writing.orchestrator.screen_batch",
            return_value=[
                {"lit_id": _LIT_A, "relevant": True, "reason": "相关"},
                {"lit_id": _LIT_B, "relevant": False, "reason": "主题不符"},
            ],
        ) as m_screen, patch(
            "src.writing.orchestrator.write_section",
            return_value=SectionResult(
                key="k", title="t", content="c", citations=[_LIT_A], dropped_citations=[]
            ),
        ):
            result = generate_review("topic", papers, "locale", do_screening=True)
        m_screen.assert_called_once()
        assert result.screened_out_ids == [_LIT_B]
        assert len(result.sections) == 6  # 不再生成“文献检索方法”章节

    def test_screening_disabled(self):
        papers = [_paper(_LIT_A)]
        with patch("src.writing.orchestrator.screen_batch") as m_screen, patch(
            "src.writing.orchestrator.write_section",
            return_value=SectionResult(
                key="k", title="t", content="c", citations=[], dropped_citations=[]
            ),
        ):
            generate_review("topic", papers, "theme", do_screening=False)
        m_screen.assert_not_called()

    def test_invalid_mode(self):
        import pytest

        with pytest.raises(ValueError):
            generate_review("topic", [], "bogus", do_screening=False)


class TestRenderReferenceList:
    def test_chinese_uses_raw_citation(self):
        raw = "张三. 某某研究[J]. 某刊, 2024, 1(2): 10-20."
        p = _paper(_LIT_A, source=Source.USER_IMPORTED, raw_citation=raw)
        out = render_reference_list([p])
        assert out == raw  # 原文直接保留,不加工

    def test_english_rendered_from_metadata(self):
        p = _paper(_LIT_A, source=Source.OPENALEX)
        p.authors = ["Alice", "Bob"]
        p.title = "Some Study"
        p.journal = "Nature"
        out = render_reference_list([p])
        assert "[J]" in out
        assert "Alice, Bob. Some Study[J]." in out
        assert "Nature" in out
        assert "2024" in out
        assert "1(2)" in out
        assert "10-20" in out
