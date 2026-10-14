# -*- coding: utf-8 -*-
"""正文排版回归:书名号与英文参考文献可检索性。

线上反馈:
1. 无摘要文献的论述与自动补证据句把论文标题塞进书名号(《标题》),
   综述正文出现《》,不符合写作要求 —— 正文禁止书名号。
2. 英文参考文献条目被中文 locale 渲染成全角标点 + "et al.." 双句号,
   复制到 Google Scholar 检索不到(文献本身已抽查 Crossref 核实为真)。
契约:
- 三处确定性证据句生成器不得输出《》;
- repair_article_text 必须剥掉正文残留的书名号(代码层强制);
- 英文条目标准化后为 ASCII 标点、无 et al..、无"题名尾句号+[J]"、页码自区间去重;
- _format_one_gbt 为 API 来源条目追加 DOI,便于粘贴到 doi.org 直接验证。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from retrieval.types import Paper, Source
import writing.orchestrator as orch
from writing.section_writer import (
    SectionResult,
    _append_evidence_sentences,
    _ensure_paragraph_evidence,
    _title_topic_phrase,
)
from qa.article_lint import repair_article_text
from writing.citeproc_renderer import _normalize_english_entry


def _paper(i: int, title: str) -> Paper:
    return Paper(
        lit_id=f"lit_cnki_{i:016x}", source=Source.CNKI, title=title,
        authors=[f"作者{i}"], journal="水产经济", year=2024,
        abstract=f"本文研究{title}的策略路径。",
    )


def test_title_topic_phrase_strips_marks_and_noise():
    assert _title_topic_phrase("《水产品电商直播营销模式研究》") == "水产品电商直播营销模式"
    assert _title_topic_phrase("基于抖音平台的海产品营销策略分析") == "抖音平台的海产品营销策略"
    assert _title_topic_phrase("") == "相关议题"
    en = _title_topic_phrase("Consumers' Preferences for the Traceability Information of Seafood Safety")
    assert "《" not in en and "seafood" in en.lower()


def test_append_evidence_sentences_never_use_book_marks():
    papers = [_paper(i, f"水产品电商直播营销模式研究{i}") for i in range(1, 7)]
    result = SectionResult(key="theme_1", title="主题", content="本节综述水产品营销。", citations=[])
    _append_evidence_sentences(result, papers, required=3)
    assert "《" not in result.content and "》" not in result.content
    assert len(result.citations) >= 3


def test_paragraph_evidence_no_book_marks():
    papers = [_paper(i, f"水产市场渠道模式与消费行为研究{i}") for i in range(1, 7)]
    result = SectionResult(key="theme_1", title="主题", content="渠道变革改变了水产品流通链条。", citations=[])
    _ensure_paragraph_evidence(result, papers, minimum=3)
    assert "《" not in result.content and "》" not in result.content


def test_evidence_recovery_section_no_book_marks():
    from writing.templates import SectionSpec
    papers = [_paper(i, f"水产品品牌建设路径研究{i}") for i in range(1, 6)]
    spec = SectionSpec(key="theme_1", title="一、品牌建设", instruction="")
    result = orch._evidence_recovery_section_result(spec, "水产品营销策略", papers)
    assert "《" not in result.content and "》" not in result.content


def test_repair_article_text_strips_book_marks():
    section = SectionResult(
        key="theme_1", title="主题",
        content="王五（2020）围绕《某项渠道研究》讨论了水产品流通问题[lit_a]。",
        citations=["lit_a"],
    )
    repairs = repair_article_text([section])
    assert "《" not in section.content and "》" not in section.content
    assert "某项渠道研究" in section.content, "剥壳后应保留内文,不丢内容"
    assert any("书名号" in change for r in repairs for change in r.get("changes", []))


def test_normalize_english_entry_for_scholar_search():
    raw = (
        "Stoll J S，Harrison H L，Sousa E D，et al.. Alternative Seafood Networks "
        "During COVID-19：Implications for Resilience.[J]. Frontiers，2021，8：655-655."
    )
    out = _normalize_english_entry(raw)
    assert "，" not in out and "：" not in out
    assert "et al.." not in out and "et al." in out
    assert ".[J]" not in out
    assert "655-655" not in out and "655" in out
    assert out.startswith("Stoll J S, Harrison H L, Sousa E D, et al.")


def test_format_one_gbt_appends_doi_for_api_sources(monkeypatch):
    paper = Paper(
        lit_id="lit_pubmed_0000000000000001", source=Source.PUBMED,
        title="Consumers' Preferences for the Traceability Information of Seafood Safety.",
        authors=["Hoque M Z"], journal="Foods", year=2022,
        doi="10.3390/foods11121675",
    )
    monkeypatch.setattr(
        orch, "_citeproc_render",
        lambda p: "Hoque M Z. Consumers' Preferences for the Traceability Information of Seafood Safety.[J]. Foods, 2022, 11(12): 1675.",
    )
    out = orch._format_one_gbt(paper)
    assert "DOI:10.3390/foods11121675." in out, "英文条目必须带 DOI 便于验证"
    assert out.count("1675") >= 1

