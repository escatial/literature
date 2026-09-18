from qa.article_lint import check_article_text, repair_article_text, repair_unbound_author_year
from writing.section_writer import SectionResult, _looks_like_non_article
from retrieval.types import Paper, Source


def test_article_lint_detects_journal_name_leak():
    section = SectionResult(
        key="theme_1", title="主题",
        content="张三（2024）在《Applied Sciences》中提出了相关模型。",
        citations=["lit_openalex_1"],
    )
    paper = Paper(
        lit_id="lit_openalex_1", source=Source.OPENALEX, title="A", authors=["张三"],
        journal="Applied Sciences", year=2024,
    )
    result = check_article_text([section], papers=[paper])
    assert any(issue.code == "ARTICLE_JOURNAL_LEAK" for issue in result.issues)


def test_article_lint_repairs_journal_name_leak_including_comment_section():
    section = SectionResult(
        key="comment", title="文献述评",
        content="Smith（2024）在《Applied Sciences》中提出了相关模型。",
        citations=["lit_openalex_1"],
    )
    paper = Paper(
        lit_id="lit_openalex_1", source=Source.OPENALEX, title="A", authors=["Smith"],
        journal="Applied Sciences", year=2024,
    )
    assert repair_article_text([section], papers=[paper])
    assert "Applied Sciences" not in section.content
    assert "在其研究中" in section.content
    assert not any(
        issue.code == "ARTICLE_JOURNAL_LEAK"
        for issue in check_article_text([section], papers=[paper]).issues
    )


def test_article_lint_does_not_treat_field_phrase_as_journal_leak():
    section = SectionResult(
        key="theme_1", title="主题",
        content="近年来，应急物流研究逐渐转向多目标决策。",
    )
    paper = Paper(
        lit_id="lit_cnki_1", source=Source.CNKI, title="A", authors=["张三"],
        journal="物流研究", year=2024,
    )
    assert not any(
        issue.code == "ARTICLE_JOURNAL_LEAK"
        for issue in check_article_text([section], papers=[paper]).issues
    )


def test_article_lint_detects_malformed_author_and_stuck_citation():
    section = SectionResult(
        key="theme_1",
        title="主题",
        content="胡，大，伟等（2023）在研究中发现效率提升。算法赵林林等（2026）[3]。",
        citations=["lit_cnki_1"],
    )
    result = check_article_text([section])
    codes = {issue.code for issue in result.issues}
    assert "ARTICLE_SPLIT_AUTHOR" in codes
    assert "ARTICLE_STUCK_AUTHOR_YEAR" in codes
    assert result.status.value == "fail"


def test_article_lint_detects_dense_citation_chain():
    section = SectionResult(
        key="theme_1",
        title="主题",
        content="研究表明（参见：甲（2023）[1]；乙（2024）[2]；丙（2025）[3]）。",
        citations=["a", "b", "c"],
    )
    result = check_article_text([section])
    assert any(issue.code == "ARTICLE_DENSE_CITATIONS" for issue in result.issues)


def test_article_lint_repairs_deterministic_formatting():
    section = SectionResult(
        key="theme_1", title="主题",
        content="胡，大，伟等（2023）在研究中发现效率提升[2]。算法赵林林等（2026）[3]。",
        citations=["lit_cnki_1"],
    )
    repairs = repair_article_text([section])
    assert repairs
    assert "胡大伟" in section.content
    assert "。" in section.content
    assert not check_article_text([section]).issues


def test_repair_unbound_author_year_adds_lit_anchor_for_unique_match():
    section = SectionResult(
        key="theme_1", title="主题",
        content="张三（2023）指出治理有效。",
        citations=[],
    )
    paper = Paper(
        lit_id="lit_cnki_aa01", source=Source.CNKI, title="治理研究",
        authors=["张三"], journal="测试学报", year=2023,
    )
    repairs = repair_unbound_author_year([section], [paper])
    assert repairs
    assert "[lit_cnki_aa01]" in section.content


def test_article_lint_rejects_model_internal_reasoning_leak():
    section = SectionResult(
        key="theme_1", title="主题",
        content="Let me check which authors are in the literature pool:\n- 张三 (2024) - lit_cnki_1",
        citations=[],
    )
    result = check_article_text([section])
    assert any(issue.code == "ARTICLE_META_REASONING" for issue in result.issues)
    assert result.status.value == "fail"


def test_valid_inline_lit_anchors_are_not_treated_as_catalog_leak():
    text = (
        "张三（2024）比较了两类方法[lit_cnki_1]。"
        "李四（2025）补充了不同场景的证据[lit_cnki_2]。"
        "Wang et al.（2023）讨论了方法边界[lit_openalex_3]。"
    )
    assert _looks_like_non_article(text) is False


def test_raw_lit_id_catalog_is_still_treated_as_non_article():
    text = "\n".join([
        "- 文献甲 lit_cnki_1",
        "- 文献乙 lit_cnki_2",
        "- 文献丙 lit_openalex_3",
    ])
    assert _looks_like_non_article(text) is True


def test_article_lint_rejects_fallback_only_theme_section():
    section = SectionResult(
        key="theme_1",
        title="主题",
        content="\n".join([
            "本章围绕主题展开。",
            "补充证据显示，张三（2024）讨论了相关问题[1]。",
            "补充证据显示，李四（2025）讨论了相关问题[2]。",
            "补充证据显示，王五（2026）讨论了相关问题[3]。",
        ]),
        citations=["a", "b", "c"],
    )
    result = check_article_text([section])
    assert any(issue.code == "ARTICLE_FALLBACK_ONLY" for issue in result.issues)
    assert result.status.value == "fail"


def test_article_lint_rejects_unbound_author_year_citation():
    section = SectionResult(
        key="theme_1", title="主题",
        content="张三（2024）比较了两类方法，但正文没有绑定编号。",
        citations=[],
    )
    result = check_article_text([section])
    assert any(issue.code == "ARTICLE_UNBOUND_AUTHOR_YEAR" for issue in result.issues)
    assert result.status.value == "fail"
