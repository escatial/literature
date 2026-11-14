# -*- coding: utf-8 -*-
"""条件润色 + 综合并行 + 大组拆分守护(2026-11-14 提速重构)。"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

BACKEND_ROOT = Path(__file__).resolve().parents[2]
for p in (str(BACKEND_ROOT / "src"), str(BACKEND_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from humanizer.agent import humanize_one_round, _looks_like_draft_dump
from writing.classifier import Group, _balance_oversize_groups
from src.retrieval.types import Paper


class TestConditionalPolish:
    """_polish_and_verify_section:lint 通过跳过 humanize;不达标补 1 轮。"""

    def _mk_result(self, content="正文"):
        return SimpleNamespace(key="theme_1", content=content, citations=[],
                               dropped_citations=[], density_warning=False)

    def test_lint_pass_skips_humanize(self, monkeypatch):
        import writing.orchestrator as orch

        calls = {"humanize": 0}

        def fake_check(result, **kw):
            class R:
                status = SimpleNamespace(value="pass")
            return R()

        monkeypatch.setattr("qa.article_lint.check_article_text", fake_check)

        import humanizer.agent as H
        real_round = H.humanize_one_round

        def counting(**kw):
            calls["humanize"] += 1
            return real_round(**kw)

        monkeypatch.setattr(H, "humanize_one_round", counting)

        result = self._mk_result()
        orch._polish_and_verify_section(result, papers=[])
        assert calls["humanize"] == 0

    def test_lint_fail_runs_single_round(self, monkeypatch):
        import writing.orchestrator as orch

        calls = {"humanize": 0}

        def fake_check(result, **kw):
            class R:
                status = SimpleNamespace(value="fail")
            return R()

        monkeypatch.setattr("qa.article_lint.check_article_text", fake_check)
        monkeypatch.setattr(
            "qa.article_lint.repair_article_text", lambda *a, **k: None,
        )

        import humanizer.agent as H

        def fake_round(*, round_no, text, caller):
            calls["humanize"] += 1
            assert round_no == 1, "只允许 1 轮"
            return text, SimpleNamespace(accepted=False, detail="x")
        monkeypatch.setattr(H, "humanize_one_round", fake_round)

        result = self._mk_result("需要润色的正文")
        orch._polish_and_verify_section(result, papers=[])
        assert calls["humanize"] == 1


class TestSynthesisParallel:
    def test_parallel_and_isolated_failures(self, monkeypatch):
        import synthesis.agent as SA

        threads_seen: set[int] = set()
        started = threading.Event()

        def fake_synthesis(name, rq, theme_papers, evidence, topic, rqs):
            threads_seen.add(threading.get_ident())
            if name == "主题B":
                started.set()
                time.sleep(0.15)  # 给并发留观测窗口
            if name == "主题C":
                raise SA.SynthesisError("该主题综合超时")
            return {"consensus": [f"c-{name}"], "controversies": [], "gaps": []}

        monkeypatch.setattr(SA, "build_theme_synthesis", fake_synthesis)

        groups = [
            Group(name="主题A", lit_ids=["a1"]),
            Group(name="主题B", lit_ids=["b1"]),
            Group(name="主题C", lit_ids=["c1"]),
        ]
        papers = [Paper(lit_id="a1", source="cnki", title="A", authors=["甲"],
                        journal="刊", year=2024, abstract="x"),
                  Paper(lit_id="b1", source="cnki", title="B", authors=["乙"],
                        journal="刊", year=2024, abstract="x"),
                  Paper(lit_id="c1", source="cnki", title="C", authors=["丙"],
                        journal="刊", year=2024, abstract="x")]
        started.clear()
        t0 = time.monotonic()
        syntheses, meta = SA.build_all_syntheses(groups, papers, None, "t")
        elapsed = time.monotonic() - t0

        assert set(syntheses.keys()) == {"theme_1", "theme_2"}
        assert meta["succeeded"] == 2 and meta["failed"] == 1
        assert meta["failures"][0]["theme"] == "主题C"
        # 3 个主题至少落在 2 个线程(串行则必为 1)
        assert len(threads_seen) >= 2
        # B 的 0.15s 与其他主题重叠:总耗时显著小于串行总和(宽松断言防抖)
        assert elapsed < 0.45


class TestOversizeCap:
    def _papers(self, n):
        return [Paper(lit_id=f"p{i}", source="cnki", title=f"水产品品牌研究{i}",
                      authors=[f"作者{i}"], journal="刊", year=2024, abstract="x")
                for i in range(n)]

    def test_90_pool_giant_group_split(self):
        """90 篇池 33 篇巨型组 → 拆到 ≤25(2026-11-14 收紧,评审42 组失衡)。"""
        papers = self._papers(90)
        giant = Group(name="巨型组", lit_ids=[p.lit_id for p in papers[:33]])
        small = Group(name="正常组", lit_ids=[p.lit_id for p in papers[33:]])
        out = _balance_oversize_groups(papers, [giant, small], "水产品营销")
        sizes = [len(g.lit_ids) for g in out]
        assert max(sizes) <= 25, sizes
        assert sum(sizes) == 90

    def test_small_pool_untouched(self):
        papers = self._papers(20)
        half = Group(name="半池组", lit_ids=[p.lit_id for p in papers[:10]])
        rest = Group(name="其余", lit_ids=[p.lit_id for p in papers[10:]])
        out = _balance_oversize_groups(papers, [half, rest], "t")
        assert [len(g.lit_ids) for g in out] == [10, 10]
