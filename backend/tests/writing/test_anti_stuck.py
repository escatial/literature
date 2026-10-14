# -*- coding: utf-8 -*-
"""Step 8 — anti-stuck: heartbeat progress events fire during long LLM waits."""
from __future__ import annotations

import json
import time as _t

from retrieval.types import Paper, Source
import writing.classifier as clf


def _paper(i: int) -> Paper:
    return Paper(
        lit_id=f"lit_cnki_{i:016x}", source=Source.CNKI,
        title=f"心题纸 {i}",
        authors=[f"作家{i}"], journal="测试", year=2024,
        abstract=f"摘要 {i}",
    )


def test_heartbeat_fires_during_slow_llm_call(monkeypatch):
    """A slow messages_create should still let progress events flow. The UI
    counter resets on every still_alive event."""
    papers = [_paper(i) for i in range(1, 26)]  # 25 papers, below chunking threshold.
    events: list[dict] = []

    def slow_create(**kwargs):
        # Sleep long enough that we'd miss any heartbeat that wasn't firing
        # in the background. With interval=5.0 the helper should emit at
        # least one still_alive within this 7-second wait.
        _t.sleep(7.0)
        return json.dumps(
            {"groups": [{"name": "协同配送", "ids": list(range(1, 14))},
                        {"name": "路径优化", "ids": list(range(14, 26))}]},
            ensure_ascii=False,
        )

    monkeypatch.setattr(clf, "messages_create", slow_create)
    started = _t.monotonic()
    clf.classify_by_theme(papers, "联合配送", progress=events.append)
    elapsed = _t.monotonic() - started

    heartbeats = [e for e in events if e.get("kind") == "still_alive"]
    assert heartbeats, f"no heartbeat during {elapsed:.1f}s slow call: events={events!r}"
    # We expect at least one heartbeat at >=5s elapsed_since_call.
    assert any(h.get("elapsed_since_call", 0) >= 5.0 for h in heartbeats)


def test_format_repair_also_emits_heartbeat(monkeypatch):
    """If the LLM returns invalid JSON, format_repair runs a second LLM call;
    that call must also keep the UI counter alive."""
    papers = [_paper(i) for i in range(1, 26)]
    events: list[dict] = []
    call_log: list[str] = []

    def flaky_create(**kwargs):
        call_log.append(kwargs.get("user", "")[:40])
        if len(call_log) == 1:
            # First call returns markdown (not valid JSON) so format_repair fires.
            return "## Some markdown that won't parse as JSON"
        # Subsequent calls (format_repair) sleep so we can observe heartbeats.
        _t.sleep(6.0)
        return json.dumps(
            {"groups": [{"name": "协同配送", "ids": list(range(1, 14))},
                        {"name": "路径优化", "ids": list(range(14, 26))}]},
            ensure_ascii=False,
        )

    monkeypatch.setattr(clf, "messages_create", flaky_create)
    clf.classify_by_theme(papers, "联合配送", progress=events.append)

    repair_heartbeats = [
        e for e in events
        if e.get("kind") == "still_alive" and e.get("stage") == "format_repair"
    ]
    assert repair_heartbeats, (
        "format_repair should also emit still_alive heartbeats; saw events:"
        f" {events!r}"
    )


def test_harness_loop_emits_progress_during_axis_planning(monkeypatch):
    """90 篇走两遍式:定轴/归组的慢调用期间必须能看到 still_alive 心跳,
    定轴完成与归组进度事件也要到达 orchestrator,供前端展示。"""
    import writing.orchestrator as orch

    papers = [_paper(i) for i in range(1, 91)]  # 90 papers, two-pass path.
    events: list[dict] = []

    def slow_create(**kwargs):
        # Sleep > heartbeat interval (5s) so at least one still_alive
        # event fires from _heartbeat_progress.
        _t.sleep(7.0)
        body = (kwargs.get("system") or "") + "\n" + (kwargs.get("user") or "")
        if '"themes"' in body:
            return json.dumps({"themes": [
                {"name": "协同配送", "research_question": "如何协同配送"},
                {"name": "路径优化", "research_question": "如何优化路径"},
            ]}, ensure_ascii=False)
        return json.dumps(
            {"groups": [{"name": "协同配送", "ids": list(range(1, 46))},
                        {"name": "路径优化", "ids": list(range(46, 91))}]},
            ensure_ascii=False,
        )

    monkeypatch.setattr(clf, "messages_create", slow_create)

    # Bypass the screening step which would otherwise hit real LLM providers.
    def fake_screen(topic, candidates, do_screening):
        yield {
            "status": "started", "batch": 1, "total_batches": 1,
            "processed": 0, "total": len(candidates),
        }
        return candidates, [], orch.build_relevance_report([], {})

    monkeypatch.setattr(orch, "_screen_papers_stream", fake_screen)

    list(orch.plan_review_stream("主题", papers, "theme", progress=events.append))

    # Even with both passes slow, we must observe still_alive events.
    still_alive = [e for e in events if e.get("kind") == "still_alive"]
    assert still_alive, f"no still_alive events with 90-paper pool; saw {len(events)} events"

    # Two-pass lifecycle events reach the orchestrator so the UI can show
    # which stage is running.
    kinds = [e.get("kind") for e in events]
    assert "axis_plan_started" in kinds
    assert "axis_plan_done" in kinds
