# -*- coding: utf-8 -*-
import writing.orchestrator as orch
from retrieval.types import Paper, Source
from writing.classifier import Group
from writing.section_writer import SectionResult, ensure_section_evidence


def _paper(index: int, source: Source, title: str) -> Paper:
    return Paper(
        lit_id=f"lit_{index}",
        source=source,
        title=title,
        authors=[f"作者{index}"],
        journal="测试期刊",
        year=2025,
        abstract=f"{title}的研究对象、方法与结论摘要。",
    )


def test_theme_plan_screens_before_classification(monkeypatch):
    papers = [
        _paper(i, Source.CNKI, f"相关问题{i}")
        for i in range(6)
    ] + [_paper(99, Source.OPENALEX, "Unrelated climate model")]
    screened = papers[:6]
    classified_ids: list[str] = []

    def fake_screen(topic, candidates, do_screening):
        yield {
            "status": "started", "batch": 1, "total_batches": 1,
            "processed": 0, "total": len(candidates),
        }
        return screened, [papers[-1].lit_id], orch.build_relevance_report([], {})

    def fake_classify_by_theme(candidates, topic, *, progress=None, **kwargs):
        classified_ids.extend(p.lit_id for p in candidates)
        return [Group(name="相关问题分类", lit_ids=[p.lit_id for p in candidates])]

    monkeypatch.setattr(orch, "_screen_papers_stream", fake_screen)
    # The theme stage now goes through _run_theme_classification_loop()
    # which directly delegates to writing.classifier.classify_by_theme
    # (no longer the wrapper classify()). Monkey-patch that exact target.
    import writing.classifier as _clf_mod
    monkeypatch.setattr(_clf_mod, "classify_by_theme", fake_classify_by_theme)
    monkeypatch.setattr(orch, "_select_theme_writing_pool", lambda p, g: (p, g))

    list(orch.plan_review_stream("目标主题", papers, "theme"))

    assert classified_ids == [p.lit_id for p in screened]
    assert papers[-1].lit_id not in classified_ids


def test_theme_pool_never_moves_papers_between_language_groups():
    chinese = [_paper(i, Source.CNKI, f"中文研究问题{i}") for i in range(6)]
    english = [
        _paper(100 + i, Source.OPENALEX, f"English research problem {i}")
        for i in range(6)
    ]
    groups = [
        Group(name="中文问题路径", lit_ids=[p.lit_id for p in chinese]),
        Group(name="English problem models", lit_ids=[p.lit_id for p in english]),
    ]

    _, selected_groups = orch._select_theme_writing_pool(chinese + english, groups)

    assert set(selected_groups[0].lit_ids) == {p.lit_id for p in chinese}
    assert set(selected_groups[1].lit_ids) == {p.lit_id for p in english}


def test_numbering_does_not_duplicate_visible_author_year():
    paper = Paper(
        lit_id="lit_cnki_li",
        source=Source.CNKI,
        title="协同配送模型",
        authors=["李世熙", "王培栋"],
        journal="测试期刊",
        year=2026,
        abstract="摘要",
    )
    section = SectionResult(
        key="theme_1",
        title="协同优化",
        content="李世熙等（2026）构建了协同配送模型[lit_cnki_li]。",
        citations=[paper.lit_id],
    )
    orch.apply_citation_numbering([section], [paper])
    assert section.content == "李世熙等（2026）构建了协同配送模型[1]。"
    assert section.content.count("李世熙") == 1


def test_numbering_adds_author_year_when_anchor_has_no_visible_citation():
    paper = Paper(
        lit_id="lit_cnki_li",
        source=Source.CNKI,
        title="协同配送模型",
        authors=["李世熙", "王培栋"],
        journal="测试期刊",
        year=2026,
        abstract="摘要",
    )
    section = SectionResult(
        key="theme_1",
        title="协同优化",
        content="结构化题录：[lit_cnki_li]。",
        citations=[paper.lit_id],
    )
    orch.apply_citation_numbering([section], [paper])
    assert section.content == "结构化题录：李世熙等（2026）[1]。"


def test_numbering_is_idempotent_after_internal_anchors_are_rendered():
    paper = _paper(1, Source.CNKI, "主题研究")
    section = SectionResult(
        key="theme_1",
        title="主题",
        content="讨论了主题[lit_1]。",
        citations=[paper.lit_id],
    )
    first_ref, first_map = orch.apply_citation_numbering([section], [paper])
    first_content = section.content
    second_ref, second_map = orch.apply_citation_numbering([section], [paper])
    assert section.content == first_content
    assert section.content.endswith("[1]。")
    assert first_ref == second_ref
    assert first_map == second_map == {"lit_1": 1}


def test_restore_internal_anchors_allows_qa_repair_after_numbering():
    paper = _paper(1, Source.CNKI, "主题研究")
    section = SectionResult(
        key="theme_1",
        title="主题",
        content="作者1等（2025）讨论了主题[1]。",
        citations=[paper.lit_id],
    )
    orch.restore_internal_citation_anchors([section])
    assert section.content.endswith("[lit_1]。")


def test_english_family_initials_are_not_rendered_as_initials():
    from writing.section_writer import _paper_display_author_year
    paper = Paper(
        lit_id="lit_pubmed_gauba",
        source=Source.PUBMED,
        title="Blood delivery",
        authors=["Gauba P", "Nangia A"],
        journal="Test Journal",
        year=2025,
    )
    assert _paper_display_author_year(paper) == "Gauba et al.（2025）"


def test_section_evidence_rebuilds_stale_citations_and_adds_real_anchors():
    papers = [
        _paper(i, Source.CNKI, f"主题研究{i}")
        for i in range(6)
    ]
    # Simulate a repair pass that replaced the body but left the old mutable
    # citations field behind.  The post-condition must use body anchors, not
    # stale state, and then fill the missing evidence from real metadata.
    section = SectionResult(
        key="theme_1",
        title="主题",
        content="重写后的章节没有保留旧锚点。",
        citations=[papers[0].lit_id, papers[1].lit_id],
    )
    ensure_section_evidence(section, papers)
    assert len(section.citations) == 5
    assert all(f"[{paper.lit_id}]" in section.content for paper in papers[:5])
    assert "主题研究0" in section.content
    assert section.density_warning is False


def test_comment_section_never_gets_evidence_fallback():
    papers = [_paper(i, Source.CNKI, f"评论研究{i}") for i in range(6)]
    section = SectionResult(
        key="comment", title="文献述评", content="综合评述。", citations=[]
    )
    ensure_section_evidence(section, papers)
    assert section.citations == []
    assert "lit_" not in section.content
