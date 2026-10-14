# -*- coding: utf-8 -*-
"""Step 9 — anti-stuck: theme harness MUST fall back to local semantic cluster.

If every chunked LLM call returns 0 themes (provider dead / network blocked /
server returning garbage), the system would previously raise:
    "主题方案未通过结构检查：主题数量为 0, 文献规模需要至少 2 个主题"
and the user would be stuck clicking "重新划分主题" forever.

The contract we lock in here is: the orchestrator must hand the user at least
one deterministic group that covers most of the screened pool, instead of
silently giving up.
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from retrieval.types import Paper, Source
import writing.classifier as clf
import writing.orchestrator as orch


def _paper(i: int, src=Source.CNKI) -> Paper:
    return Paper(
        lit_id=f"lit_cnki_{i:016x}",
        source=src,
        title=f"水产品营销策略研究 {i}",
        authors=[f"作者{i}"],
        journal="水产经济",
        year=2024,
        abstract=(
            f"本文研究水产品营销策略第{i}个维度。"
            f"采用调研、问卷、案例分析。"
        ),
    )


def test_theme_harness_falls_back_when_all_chunks_return_empty(monkeypatch):
    """All chunks return [] (simulating provider dead). Orchestrator must
    invoke _semantic_cluster_groups and return at least one group covering
    most of the pool, instead of returning 0 and stranding the user."""
    papers = [_paper(i) for i in range(1, 91)]

    def fake_create(**kwargs):
        # LLM is "down": always returns empty groups.
        return json.dumps({"groups": []})

    monkeypatch.setattr(clf, "messages_create", fake_create)

    groups, meta = orch._run_theme_classification_loop(
        topic="水产品营销策略",
        papers=papers,
        progress=lambda e: None,
        check_stop=lambda: None,
    )

    # The fallback MUST kick in: groups is non-empty and covers most of pool.
    assert groups, "orchestrator returned empty groups; user would be stuck"
    covered = {lid for g in groups for lid in g.lit_ids}
    assert len(covered) >= len(papers) * 0.5, (
        f"coverage {len(covered)}/{len(papers)} below 50%; the fallback should "
        "ensure at least half-coverage even when LLM is down"
    )
    # meta must report the fallback as a successful pass so the orchestrator
    # does not surface the "主题方案未通过结构检查" error to the user.
    assert meta.get("status") == "passed", (
        f"meta status is {meta.get('status')!r}, expected 'passed'; "
        "the fallback should mark the loop as passed"
    )


def test_theme_harness_does_not_trigger_fallback_for_healthy_chunks(monkeypatch):
    """When the LLM returns valid groups, the fallback path MUST stay dormant.

    Falling back when not needed wastes the LLM-generated themes.
    """
    papers = [_paper(i) for i in range(1, 31)]

    def fake_create(**kwargs):
        # Healthy response with 2 themes covering all papers.
        return json.dumps({
            "groups": [
                {"name": "品牌定位", "research_question": "Q1",
                 "ids": list(range(1, 16))},
                {"name": "渠道策略", "research_question": "Q2",
                 "ids": list(range(16, 31))},
            ],
        })

    monkeypatch.setattr(clf, "messages_create", fake_create)

    # Track whether the semantic fallback path was triggered.
    fallback_called = []

    from writing import classifier as _clf
    orig_semantic = _clf._semantic_cluster_groups

    def spy_semantic(papers, topic, progress=None):
        fallback_called.append(True)
        return orig_semantic(papers, topic, progress=progress)

    monkeypatch.setattr(_clf, "_semantic_cluster_groups", spy_semantic)

    groups, meta = orch._run_theme_classification_loop(
        topic="水产品营销策略",
        papers=papers,
        progress=lambda e: None,
        check_stop=lambda: None,
    )

    assert len(groups) >= 2
    assert not fallback_called, (
        "semantic fallback should NOT trigger when LLM returns valid groups; "
        "this protects the user from a quality regression on healthy runs"
    )
