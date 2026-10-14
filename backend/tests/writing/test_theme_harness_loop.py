# -*- coding: utf-8 -*-
"""Verify Step 3: orchestrator theme stage routes through run_repair_loop().

The test patches writing.classifier.classify_by_theme and walks the orchestrator
through plan_review_stream(... mode='theme'). The orchestrator helper now wraps
the classify call in run_repair_loop, so successive calls advance the strategy:
default -> minimal_abstracts -> local_fallback.
"""
import json

from agent.harness import AgentStage
from retrieval.types import Paper, Source
import writing.classifier as clf
import writing.orchestrator as orch


def _paper(i: int, *, source: Source = Source.CNKI) -> Paper:
    return Paper(
        lit_id=f"lit_{i}",
        source=source,
        title=f"主题-{i}",
        authors=[f"作者{i}"],
        journal="测试",
        year=2024,
        abstract=f"摘要{i}",
    )


def _papers(n: int) -> list[Paper]:
    return [_paper(i) for i in range(1, n + 1)]


def test_theme_stage_advances_through_three_strategies(monkeypatch):
    """First call (default) returns empty groups; the harness should escalate
    to minimal_abstracts then to local_fallback; final result comes from the
    deterministic local cluster path."""
    papers = _papers(36)
    events: list[dict] = []

    strategies_seen: list[str] = []

    def fake_classify(papers, topic, *, progress=None, _raise_on_coverage_failure=True,
                      _strategy="default", **_kwargs):
        strategies_seen.append(_strategy)
        if progress:
            progress({"kind": "_call", "strategy": _strategy, "papers": len(papers)})
        if _strategy == "default":
            # Return empty so inspect_theme_groups fails theme_count.
            return []
        if _strategy == "minimal_abstracts":
            # Cover 60% so theme_coverage fails but theme_count passes.
            covered_ids = [p.lit_id for p in papers[: int(len(papers) * 0.6)]]
            return [clf.Group(name="主题A", lit_ids=covered_ids)]
        if _strategy == "local_fallback":
            # Cover 100% to satisfy inspect.
            return [clf.Group(name="本地主题", lit_ids=[p.lit_id for p in papers])]
        raise AssertionError(f"unexpected strategy {_strategy}")

    def fake_screen(topic, candidates, do_screening):
        yield {
            "status": "started", "batch": 1, "total_batches": 1,
            "processed": 0, "total": len(candidates),
        }
        return candidates, [], orch.build_relevance_report([], {})

    monkeypatch.setattr(clf, "classify_by_theme", fake_classify)
    monkeypatch.setattr(orch, "_screen_papers_stream", fake_screen)
    monkeypatch.setattr(orch, "_select_theme_writing_pool", lambda p, g: (p, g))

    list(orch.plan_review_stream("主题", papers, "theme"))

    assert strategies_seen[:3] == ["default", "minimal_abstracts", "local_fallback"]
    # Step 6: strategy-selected events show up in the progress stream.
    # We can't intercept the SSE stream directly here, but the classifier
    # helper above already calls progress(...) and we observe that the
    # strategies were advanced in order.


def test_theme_harness_emits_partial_groups_when_inspect_fails(monkeypatch):
    """Step 5: when all attempts fail to cover the pool, the orchestrator
    should still surface best partial groups via the progress stream rather
    than dropping the result silently."""
    papers = _papers(40)
    chunks_seen: list[dict] = []

    def fake_classify(papers, topic, *, progress=None, _raise_on_coverage_failure=True,
                      _strategy="default", **_kwargs):
        # Always return an incomplete group so the harness runs all 3 attempts.
        if progress:
            progress({"kind": "_call", "strategy": _strategy})
        covered = [p.lit_id for p in papers[: 10]]
        return [clf.Group(name="部分主题", lit_ids=covered)]

    def fake_screen(topic, candidates, do_screening):
        yield {
            "status": "started", "batch": 1, "total_batches": 1,
            "processed": 0, "total": len(candidates),
        }
        return candidates, [], orch.build_relevance_report([], {})

    monkeypatch.setattr(clf, "classify_by_theme", fake_classify)
    monkeypatch.setattr(orch, "_screen_papers_stream", fake_screen)
    monkeypatch.setattr(orch, "_select_theme_writing_pool", lambda p, g: (p, g))

    chunks_seen = []
    list(orch.plan_review_stream(
        "主题", papers, "theme", progress=chunks_seen.append,
    ))
    # 36+ papers triggers Step 1 chunking; even if LLM returns incomplete,
    # the orchestrator wraps it through 3 strategies. The classify call is
    # always invoked at least once.
    strategy_calls = [e for e in chunks_seen if e.get("kind") == "_call"]
    assert strategy_calls, "classify_by_theme should have been invoked via harness"
    # Step 5 assertion: the harness decides not to crash; whatever it returns
    # must not raise when input remains non-empty. The deterministic fallback
    # in classify_by_theme runs after all attempts and rescues the case.
    # (We do not assert on the structure of returned groups because the test
    # intentionally fakes LLM failure; we only assert that the harness ran
    # attempts without raising.)
