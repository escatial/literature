# -*- coding: utf-8 -*-
"""证据补句机器质量回归(2026-10-16 英文桶第三起事故)。

线上事故:确认面板英文主题段出现
  "Ahmed et al.（2022）围绕Aquaculture Production Value Chains COVID 19
   Pandemic提供了相邻问题的研究证据N et al.（2022）[42]。"
三重缺陷:
1. "N et al.（2022）" —— 脏数据首作者只剩单字母,显示名照拼;
2. 题名短语把句中功能词(and/of/on)全剥掉,留下 "Based Bayesian" 词沙;
3. 段落补句机与节级补句机互不知情,同一模板句重复出现 2-3 次。
"""
import pytest

from retrieval.types import Paper, Source
from writing.section_writer import (
    _paper_display_author_year,
    _title_topic_phrase,
    ensure_section_evidence,
    SectionResult,
)


def _papers():
    return [
        Paper(lit_id="lit_a", source=Source.OPENALEX, title="Aquaculture production value chains and COVID-19 pandemic: impacts and policy responses", authors=["Ahmed M", "Nabil S", "Omar T"], journal="J", year=2022, abstract=""),
        Paper(lit_id="lit_b", source=Source.OPENALEX, title="Factors influencing consumers' watching purchase intentions in live streaming commerce", authors=["Chaang-Iuan H", "Ho N"], journal="J", year=2022, abstract=""),
        Paper(lit_id="lit_c", source=Source.OPENALEX, title="Salmon consumption behavior prediction based on Bayesian optimization", authors=["Wu X", "Zhang Y"], journal="J", year=2025, abstract=""),
        Paper(lit_id="lit_d", source=Source.OPENALEX, title="N eval of aqua feed", authors=["N", "Ho Q"], journal="J", year=2022, abstract=""),
    ]


# ---------- 显示名 ----------

def test_single_letter_author_rejected():
    """首作者只剩单字母(脏数据)→ 显示名返回空串,不产出 "N et al."。"""
    assert _paper_display_author_year(_papers()[3]) == ""


def test_hyphenated_surname_kept():
    """连字符合姓(Chaang-Iuan)不得被 isalpha 误杀。"""
    assert _paper_display_author_year(_papers()[1]) == "Chaang-Iuan et al.（2022）"


def test_initials_suffix_still_resolved():
    """PubMed 形态 "Family Initials" 仍取姓氏(Gauba P → Gauba)。"""
    p = Paper(lit_id="lit_g", source=Source.PUBMED, title="T", authors=["Gauba P", "Rao S"], journal="J", year=2024, abstract="")
    assert _paper_display_author_year(p) == "Gauba et al.（2024）"


# ---------- 题名短语 ----------

def test_inner_function_words_kept():
    """英文短语保留句中功能词,不产生 "Based Bayesian" 词沙。"""
    phrase = _title_topic_phrase("Salmon consumption behavior prediction based on Bayesian optimization")
    assert "based on" in phrase.lower()
    assert "Based Bayesian" not in phrase


def test_short_slogan_word_not_extracted():
    """短语门槛 6 字(由调用方保证)/截断不停在停用词上。"""
    phrase = _title_topic_phrase(
        "Factors influencing consumers' watching purchase intentions in live streaming commerce",
    )
    assert not phrase.lower().endswith((" in", " and", " of", " on", " for", " live"))


# ---------- 补句机器:去重 + 诚实不足 ----------

def test_no_duplicate_filler_and_no_fake_author():
    """同一文献的模板句全节只出现一次;单字母作者的文献被跳过。"""
    result = SectionResult(
        key="theme_2", title="水产品营销策略国际研究",
        content="Aquaculture value chains were studied by Ahmed et al.（2022）[lit_a].\n\nGlobal demand keeps rising.",
        citations=["lit_a"], dropped_citations=[],
    )
    out = ensure_section_evidence(result, _papers(), required=3)
    assert "N et al." not in out.content
    sentences = [s.strip() for s in out.content.replace("\n\n", "\n").split("。") if s.strip()]
    assert len(sentences) == len(set(sentences)), "存在重复模板句"
    # lit_d(单字母作者)必须被跳过,其锚点不得出现
    assert "lit_d" not in out.content
    assert "lit_d" not in out.citations


def test_density_shortfall_reported_honestly():
    """可补文献不足时如实置 density_warning,不靠重复引用凑数。"""
    papers = _papers()[:2]  # 只有 2 篇可用,达不到 3 源
    result = SectionResult(
        key="theme_2", title="章节",
        content="Ahmed et al.（2022）studied value chains[lit_a].",
        citations=["lit_a"], dropped_citations=[],
    )
    out = ensure_section_evidence(result, papers, required=3)
    assert out.density_warning is True
    sentences = [s.strip() for s in out.content.replace("\n\n", "\n").split("。") if s.strip()]
    assert len(sentences) == len(set(sentences))
