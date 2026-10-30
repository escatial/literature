# -*- coding: utf-8 -*-
"""Verify Step 1: 90-paper pool triggers product-level chunking.

The previous 120-paper threshold silently allowed 30-119-paper pools to enter
the LLM in a single shot. The 90-paper reproduction run is the canonical case
that suffered from this; the new threshold (>= 30) routes these pools through
the chunked path and emits the structured `chunk_started`/`chunk_done` events
the orchestrator and tests rely on.
"""
import json

from retrieval.types import Paper, Source
import writing.classifier as clf


def _paper(i: int, *, source: Source = Source.CNKI, lang: str = "zh") -> Paper:
    if lang == "zh":
        return Paper(
            lit_id=f"lit_cnki_{i:016x}",
            source=source,
            title=f"主题研究问题-{i:03d}",
            authors=[f"作者{i}"],
            journal="测试学报",
            year=2020 + (i % 6),
            abstract=f"本文研究主题的第{i % 4}个维度"
                     f"并提出方法{i}。",
        )
    return Paper(
        lit_id=f"lit_openalex_{i:016x}",
        source=source,
        title=f"Topic research problem {i:03d}",
        authors=[f"Smith {i}"],
        journal="Test Journal",
        year=2024,
        abstract=f"This study explores research dimension {i % 4} using method {i}.",
    )


def _papers(n: int) -> list[Paper]:
    # Half Chinese, half English so coverage requires cross-language logic.
    items: list[Paper] = []
    for i in range(1, n + 1):
        if i % 2 == 0:
            items.append(_paper(i, lang="zh"))
        else:
            items.append(_paper(i, source=Source.OPENALEX, lang="en"))
    return items


def test_chunk_threshold_is_30(monkeypatch):
    assert clf._CHUNK_THRESHOLD == 30, "两遍式主题划分阈值默认 = 30 篇"


def _fake_two_pass_create(papers, axis_names):
    """两遍式 fake:按提示词特征区分「定轴」与「归组」两种调用。

    2026-10-30 行协议迁移后,定轴提示词的特征标记是「#序号|轴名|研究问题」。
    """
    def fake_create(**kwargs):
        body = (kwargs.get("system") or "") + "\n" + (kwargs.get("user") or "")
        if "轴名|研究问题" in body:
            return json.dumps({"themes": [
                {"name": name, "research_question": f"如何研究{name}的核心问题"}
                for name in axis_names
            ]}, ensure_ascii=False)
        buckets = [[] for _ in axis_names]
        for i in range(1, len(papers) + 1):
            buckets[(i - 1) % len(axis_names)].append(i)
        return json.dumps(
            {"groups": [{"name": axis_names[k], "ids": ids}
                        for k, ids in enumerate(buckets)]},
            ensure_ascii=False,
        )
    return fake_create


def test_90_paper_pool_uses_global_axis_planning(monkeypatch):
    """>=30 篇走两遍式:第一遍全局定轴,第二遍按固定轴归组,一篇不丢。"""
    papers = _papers(90)
    progress_events: list[dict] = []
    axis_names = ["协同主题", "绩效主题", "转型主题"]

    monkeypatch.setattr(
        clf, "messages_create",
        _fake_two_pass_create(papers, axis_names),
    )
    groups = clf.classify_by_theme(papers, "主期研究", progress=progress_events.append)

    kinds = [e.get("kind") for e in progress_events]
    assert "axis_plan_started" in kinds
    assert "axis_plan_done" in kinds
    # 旧分块路径必须彻底退场:不再有块级自造主题与跨块合并事件。
    assert "chunk_started" not in kinds
    assert "merge_candidates" not in kinds
    # 归组结果与主题轴一一对应(逐字使用轴名),覆盖一篇不丢。
    assert [g.name for g in groups] == axis_names
    covered = {lid for g in groups for lid in g.lit_ids}
    assert covered == {p.lit_id for p in papers}
    # 主题轴的 research_question 锚定到最终分组,供确认面板展示。
    assert all(g.research_question for g in groups)


def test_chunking_below_threshold_still_works(monkeypatch):
    """Smoke test: with 24 papers (< 30), the unified single-shot path still runs."""
    papers = _papers(24)
    events: list[dict] = []

    def fake_create(**kwargs):
        # 24/24 → 4 groups of 6.
        buckets = [[] for _ in range(4)]
        for i in range(1, len(papers) + 1):
            buckets[(i - 1) % 4].append(i)
        names = ["協同主题", "性能主题", "转型主题", "公共主题"]
        payload = {"groups": [{"name": names[k], "ids": ids} for k, ids in enumerate(buckets)]}
        return json.dumps(payload, ensure_ascii=False)

    monkeypatch.setattr(clf, "messages_create", fake_create)
    groups = clf.classify_by_theme(papers, "任意主题", progress=events.append)
    assert any(e["kind"] == "chunk_started" for e in events) is False
    assert any(e["kind"] == "theme_attempt" for e in events) is True
    assert {lid for g in groups for lid in g.lit_ids} == {p.lit_id for p in papers}


def test_theme_attempt_event_records_minimal_abstracts_strategy(monkeypatch):
    """Step 6: when repair escalates to strategy='minimal_abstracts', the
    emitted theme_attempt event reports abstract_limit = 250 so we can audit
    which round used which truncation level."""
    papers = _papers(24)
    events: list[dict] = []

    def fake_create(**kwargs):
        # Even with minimal abstracts we produce 4 valid Chinese-theme names
        # (pure-ASCII titles like "A"/"B"/"C" are filtered out by
        # _group_name_acceptable).
        buckets = [[] for _ in range(4)]
        for i in range(1, len(papers) + 1):
            buckets[(i - 1) % 4].append(i)
        names = [
            "协同主题", "性能主题",
            "转型主题", "公共主题",
        ]
        return json.dumps({"groups": [{"name": names[k], "ids": ids} for k, ids in enumerate(buckets)]},
                          ensure_ascii=False)

    monkeypatch.setattr(clf, "messages_create", fake_create)
    clf.classify_by_theme(
        papers, "任意主题",
        _strategy="minimal_abstracts", progress=events.append,
    )
    attempt = [e for e in events if e["kind"] == "theme_attempt"]
    assert attempt
    assert attempt[0]["strategy"] == "minimal_abstracts"
    assert attempt[0]["abstract_limit"] == 250
