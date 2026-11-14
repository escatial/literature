# -*- coding: utf-8 -*-
"""2026-11-14 提速+质量重构守护:大纲成文 / 分级路由 / 条件润色 / 占位剥离。

对应根因(全量取证于 job 8e25ffd9,124 分钟拆账):
- 首过写作仅 2-4 分钟,失败连锁(垃圾流→整章重写 545s 失败→humanize
  3 轮→QA 修复)烧掉 33 分钟 → 大纲模式段级验收,废除整章重写;
- "每段对比≥3篇"机械模板产出目录式罗列 → 段落级论证大纲(claim+relation);
- 引用密度按池规模(34 篇要求 16 处)与单章容量脱节 → 大纲分配 claim-driven;
- 筛选/核查类高频短调用与写作共用旗舰 → tier="fast" 分级路由(可回落);
- 固定 3 轮 humanize 是每章 2-4 分钟固定开销 → lint 先行,不达标才补 1 轮;
- 评审41/42 的 22 处 `[...]` 悬空引用占位直通交付 → 咽喉点剥离 + 门禁计数。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
for p in (str(BACKEND_ROOT / "src"), str(BACKEND_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

import pytest

import writing.section_writer as SW
from writing.section_writer import (
    _CITE_PLACEHOLDER_RE,
    _outline_json_parse,
    _para_has_citation,
    _strip_citation_placeholders,
    _write_section_via_outline,
    _looks_like_non_article,
)
from writing.templates import SectionSpec
from src.retrieval.types import Paper


def _mk_papers(n: int = 5) -> list[Paper]:
    return [
        Paper(
            lit_id=f"lit_t{i}", source="cnki",
            title=f"水产品营销研究{i}", authors=[f"作者{i}"],
            journal="测试刊", year=2023, abstract="研究方法与结论。" * 10,
        )
        for i in range(n)
    ]


def _mk_section() -> SectionSpec:
    return SectionSpec(key="theme_1", title="三、消费者行为", instruction="写一章")


_OUTLINE = json.dumps({"paragraphs": [
    {"claim": "该领域研究沿三条路径演进", "lit_ids": ["lit_t0", "lit_t1"], "relation": "evolution"},
    {"claim": "质量信号是支付溢价核心机制", "lit_ids": ["lit_t2"], "relation": "consensus"},
    {"claim": "跨市场复制证据仍不足", "lit_ids": ["lit_t3", "lit_t4"], "relation": "gap"},
]}, ensure_ascii=False)


class TestOutlineJsonParse:
    def test_plain(self):
        assert _outline_json_parse(_OUTLINE) is not None

    def test_fenced(self):
        assert _outline_json_parse(f"```json\n{_OUTLINE}\n```") is not None

    def test_wrapped_with_prose(self):
        assert _outline_json_parse(f"说明\n{_OUTLINE}\n后缀") is not None

    def test_garbage_returns_none(self):
        assert _outline_json_parse("没有任何结构") is None

    def test_filters_bad_lit_ids(self):
        raw = json.dumps({"paragraphs": [
            {"claim": "a", "lit_ids": ["lit_ghost"], "relation": "x"},
            {"claim": "b", "lit_ids": ["lit_t0"], "relation": "consensus"},
            {"claim": "c", "lit_ids": ["lit_t1"], "relation": "controversy"},
        ]}, ensure_ascii=False)
        paras = _outline_json_parse(raw)
        assert paras is not None


class TestBuildOutline:
    def test_outline_uses_fast_tier(self, monkeypatch):
        """大纲调用必须走快档(tier=fast)——分级路由的质量回归点。"""
        captured = {}

        def fake_create(system, user, max_tokens=4000, **kw):
            captured["tier"] = kw.get("tier")
            return _OUTLINE

        monkeypatch.setattr(SW, "messages_create", fake_create)
        papers = _mk_papers()
        outline = SW._build_paragraph_outline(
            section=_mk_section(), topic="水产品营销", papers=papers,
            allowed={p.lit_id for p in papers}, is_comment=False,
            research_questions=None, review_goal=None, notes_block=None, model=None,
        )
        assert outline and len(outline) == 3
        assert captured["tier"] == "fast"

    def test_too_few_paragraphs_returns_none(self, monkeypatch):
        raw = json.dumps({"paragraphs": [
            {"claim": "只有一段", "lit_ids": ["lit_t0"], "relation": "consensus"},
        ]}, ensure_ascii=False)
        monkeypatch.setattr(SW, "messages_create", lambda *a, **k: raw)
        papers = _mk_papers()
        assert SW._build_paragraph_outline(
            section=_mk_section(), topic="t", papers=papers,
            allowed={p.lit_id for p in papers}, is_comment=False,
            research_questions=None, review_goal=None, notes_block=None, model=None,
        ) is None


class TestViaOutline:
    def test_required_is_claim_driven(self, monkeypatch):
        """引用目标 = 大纲实际分配的文献数(claim-driven),不是池规模比例。"""
        monkeypatch.setattr(
            SW, "messages_create",
            lambda system, user, max_tokens=4000, **kw: (
                _OUTLINE if "结构规划专家" in system
                else "张三（2023）指出这一点，李四（2022）补充了边界。"
            ),
        )
        papers = _mk_papers(34)  # 34 篇的池:旧公式会要求 16+,大纲只分配 5 篇
        content, required = _write_section_via_outline(
            section=_mk_section(), topic="水产品营销", papers=papers,
            allowed={p.lit_id for p in papers}, is_comment=False,
            research_questions=None, review_goal=None, notes_block=None, model=None,
            progress=lambda e: None,
        )
        assert required == 5
        assert len(content.split("\n\n")) == 3

    def test_uncited_paragraph_gets_single_rewrite(self, monkeypatch):
        """无引用段落触发一次单段重写(不是整章 545s 重写)。"""
        calls = {"para": 0}

        def fake(system, user, max_tokens=4000, **kw):
            if "结构规划专家" in system:
                return _OUTLINE
            calls["para"] += 1
            return "没有引用的段落"

        events: list[dict] = []
        monkeypatch.setattr(SW, "messages_create", fake)
        res = _write_section_via_outline(
            section=_mk_section(), topic="t", papers=_mk_papers(),
            allowed={p.lit_id for p in _mk_papers()}, is_comment=False,
            research_questions=None, review_goal=None, notes_block=None,
            model=None, progress=events.append,
        )
        assert res is not None
        starts = [e for e in events if e["step"] == "para" and e["status"] == "start"]
        assert len(starts) == 3
        # 每段都缺引用 → 每段各重写一次(3 首稿 + 3 重写)
        assert calls["para"] == 6

    def test_fallback_on_garbage_outline(self, monkeypatch):
        monkeypatch.setattr(SW, "messages_create", lambda *a, **k: "没有JSON")
        assert _write_section_via_outline(
            section=_mk_section(), topic="t", papers=_mk_papers(),
            allowed={p.lit_id for p in _mk_papers()}, is_comment=False,
            research_questions=None, review_goal=None, notes_block=None, model=None,
        ) is None


class TestCitationPlaceholder:
    def test_strip_cn_and_en_variants(self):
        text = "他说了这一点[...]，另有人主张[…]，还有人认为[……]。"
        cleaned, n = _strip_citation_placeholders(text)
        assert n == 3
        assert "[" not in cleaned and "]" not in cleaned

    def test_real_anchors_untouched(self):
        text = "侯子楠等（2019）的调查[lit_t0]表明了这一点。"
        cleaned, n = _strip_citation_placeholders(text)
        assert n == 0 and cleaned == text

    def test_regex_in_non_article_detector_scope(self):
        # 门禁复核用:占位符在正文里应可被统计
        assert len(_CITE_PLACEHOLDER_RE.findall("a[...]b[…]c")) == 2


class TestParaHasCitation:
    def test_cn(self):
        assert _para_has_citation("侯子楠等（2019）的调查表明……")

    def test_en(self):
        assert _para_has_citation("Galati et al.(2021) found that ...")

    def test_none(self):
        assert not _para_has_citation("这一段没有任何夹注。")


class TestOutlineContentStaysArticle:
    def test_outline_content_not_flagged_non_article(self, monkeypatch):
        monkeypatch.setattr(
            SW, "messages_create",
            lambda system, user, max_tokens=4000, **kw: (
                _OUTLINE if "结构规划专家" in system
                else "张三（2023）指出这一点，李四（2022）补充了边界。"
            ),
        )
        content, _req = _write_section_via_outline(
            section=_mk_section(), topic="t", papers=_mk_papers(),
            allowed={p.lit_id for p in _mk_papers()}, is_comment=False,
            research_questions=None, review_goal=None, notes_block=None, model=None,
        )
        assert not _looks_like_non_article(content)
