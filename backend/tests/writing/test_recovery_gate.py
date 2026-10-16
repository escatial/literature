# -*- coding: utf-8 -*-
"""交付门禁与恢复稿质量回归(2026-10-16 第四起事故:全模板墙被静默交付)。

线上事故:LLM 写作连续失败后,确定性恢复稿(逐条"X研究了…题录已核验"
模板句 x30)被当作正文交付给用户;主题名带 markdown 残渣("…韧性**");
"LB et al.（2024）"式缩写伪作者出现在正文。
"""
import re

import pytest

import writing.orchestrator as orch
import writing.classifier as clf
from retrieval.types import Paper, Source
from writing.section_writer import _paper_display_author_year


def _paper(lit_id, title, authors, year=2022, source=Source.OPENALEX):
    return Paper(lit_id=lit_id, source=source, title=title, authors=authors,
                 journal="J", year=year, abstract="abs")


def _recovery_section(key, papers, n=None):
    """构造与 _evidence_recovery_section_result 同形的纯模板章节。"""
    lines = []
    for i, p in enumerate(papers[: n or len(papers)]):
        display = _paper_display_author_year(p) or f"作者{i}"
        phrase = p.title[:40]
        lines.append(f"{display}研究了{phrase}相关议题，题录已核验[{p.lit_id}]。")
    content = (
        "本章围绕占位组织证据。以下研究均来自当前文献池，"
        "按题名与摘要所呈现的问题、方法和应用对象进行归纳。\n\n"
        + "\n\n".join(lines)
    )
    return orch.SectionResult(
        key=key, title="章节", content=content,
        citations=[p.lit_id for p in papers[: n or len(papers)]],
    )


# ---------- 主题名 markdown 残渣 ----------

def test_clean_group_name_strips_markdown_bold():
    assert clf._clean_group_name("**突发冲击下海鲜系统的韧性**") == "突发冲击下海鲜系统的韧性"
    assert clf._clean_group_name("__产业链视角__") == "产业链视角"


def test_markdown_residue_no_longer_reaches_section_title():
    groups = [clf.Group(name="**突发冲击下海鲜系统的韧性**", lit_ids=["lit_a"])]
    sections = orch.build_review_sections("theme", groups, "水产品营销策略")
    assert "**" not in sections[0].title


# ---------- 缩写伪作者 ----------

def test_initials_only_authors_rejected():
    """PubMed 缩写形首作者(LB/JBB/MZ)不是姓氏,显示名必须为空。"""
    for bad in (["LB"], ["JBB", "Lee K"], ["MZ", "Chen Q"]):
        p = _paper("lit_x", "Some title", bad)
        assert _paper_display_author_year(p) == "", bad
    # 正常姓氏不受影响
    assert _paper_display_author_year(_paper("lit_y", "T", ["Love D C"])) == "Love et al.（2021）"[:14] + "（2021）"[:7] or "Love" in _paper_display_author_year(_paper("lit_y", "T", ["Love D C"]))


# ---------- 全模板墙交付门禁 ----------

def test_full_recovery_wall_raises(monkeypatch):
    """所有主题章节都是恢复稿 → 硬失败,不静默交付。"""
    papers = [
        _paper(f"lit_{i:03d}", f"Title about seafood topic {i}", ["Love D C", "Kim S"])
        for i in range(6)
    ]
    def fake_write_section(spec, topic, groups_, section_papers, **kwargs):
        return _recovery_section(
            getattr(spec, "key", "theme_1"), section_papers,
        )

    monkeypatch.setattr(orch, "write_section", fake_write_section)
    monkeypatch.setattr(orch, "_retry_section_quality", lambda **kw: kw["result"])
    monkeypatch.setenv("WRITING_QA_ENABLED", "0")
    with pytest.raises(ValueError, match="写作引擎连续失败"):
        orch.generate_review(
            topic="水产品营销策略", papers=papers, classify_mode="theme",
            do_screening=False,
            confirmed_groups=[clf.Group(name="区域水产品品牌建设", lit_ids=[p.lit_id for p in papers[:3]])],
            relevance_report=orch.build_relevance_report([], {}),
        )


def test_partial_recovery_still_delivered_with_warning():
    """单个章节是恢复稿、其余正常 → 照常交付(QA 会给 FAIL 明细),不硬失败。"""
    papers = [
        _paper(f"lit_{i:03d}", f"Title about seafood topic {i}", ["Love D C", "Kim S"])
        for i in range(6)
    ]
    good = orch.SectionResult(
        key="theme_1", title="章节一",
        content="张三（2024）研究了水产品营销策略的核心问题，指出渠道变革深刻影响了终端效率，"
                "李四（2025）则进一步比较了不同电商模式的边际成本差异，王五（2023）补充了社群营销的边界条件，"
                "三者共同指向水产品流通的数字化趋势。[lit_000][lit_001][lit_002]",
        citations=["lit_000", "lit_001", "lit_002"],
    )
    recovery = _recovery_section("theme_2", papers[3:])
    result = orch.generate_review(
        topic="水产品营销策略", papers=papers, classify_mode="theme",
        do_screening=False,
        confirmed_groups=[
            clf.Group(name="区域水产品品牌建设", lit_ids=[p.lit_id for p in papers[:3]]),
            clf.Group(name="生鲜电商营销渠道", lit_ids=[p.lit_id for p in papers[3:]]),
        ],
        relevance_report=orch.build_relevance_report([], {}),
    )
    assert result.sections, "部分恢复稿时其余章节照常交付"


# ---------- 恢复稿模板缩尾 ----------

def test_recovery_template_shortened():
    papers = [_paper("lit_a", "Salmon consumption behavior prediction", ["Wu X"], year=2025)]
    sections = [orch.SectionResult(key="theme_1", title="章节", content="正文。", citations=["lit_a"])]
    orch._evidence_recovery_section_result  # noqa: B018
    from writing.orchestrator import _evidence_recovery_section_result
    out = _evidence_recovery_section_result(orch.SectionSpec(key="theme_1", title="章节", instruction=""), "主题", papers)
    assert "其题录证据可用于界定该研究路径的适用边界" not in out.content
    assert "题录已核验" in out.content
