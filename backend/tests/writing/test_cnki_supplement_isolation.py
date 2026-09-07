# -*- coding: utf-8 -*-
"""自动补中文检索的任务隔离回归测试。

历史缺陷:写作规划阶段自动补中文时,知网补充文献落库 task_id=None、
回读捞全库 source='cnki' 行 —— 历史任务/别次补充的中文文献全部混进
本次写作输入(线上实测:17 篇 task_id=None 的知网行跨任务泄漏)。
修复后:每次补充持有专属 pool_task_id,落库与回读双向隔离。
"""
from retrieval.types import Paper, Source
import writing.orchestrator as orch


def _cn(n: int) -> Paper:
    return Paper(
        lit_id=f"lit_cnki_{n:016x}", source=Source.CNKI, title=f"治理研究{n}",
        authors=["张三"], journal="学报", year=2024, abstract="摘要",
    )


def test_supplement_scoped_by_dedicated_pool_task_id(monkeypatch):
    seen: dict = {}

    def fake_plan(topic):
        return {"queries_cnki": [f'主题&"{topic}"']}

    async def fake_run(**kwargs):
        seen["run_kwargs"] = kwargs
        return {"status": "succeeded", "saved": 3}

    def fake_from_pool(pool_task_id=None):
        seen["pool_task_id"] = pool_task_id
        return [_cn(1), _cn(2), _cn(3)]

    import api.cnki as api_cnki
    import automation.cnki_adapter as adapter
    import retrieval.query_planner as qp
    monkeypatch.setattr(qp, "plan_query_strings", fake_plan)
    monkeypatch.setattr(adapter, "run_cnki_full_auto", fake_run)
    monkeypatch.setattr(api_cnki, "_cnki_papers_from_pool", fake_from_pool)

    added = orch._retrieve_chinese_papers("基层治理", 60)

    assert [p.lit_id for p in added] == ["lit_cnki_" + f"{i:016x}" for i in (1, 2, 3)]
    run_id = seen["run_kwargs"].get("pool_task_id")
    assert run_id and run_id.startswith("cnki-supplement-"), run_id
    assert seen["pool_task_id"] == run_id  # 回读与落库同一个隔离 ID


def test_supplement_failure_returns_empty(monkeypatch):
    import automation.cnki_adapter as adapter
    import retrieval.query_planner as qp
    monkeypatch.setattr(qp, "plan_query_strings", lambda t: {"queries_cnki": ["q"]})

    async def boom(**kwargs):
        raise RuntimeError("cookie 失效")

    monkeypatch.setattr(adapter, "run_cnki_full_auto", boom)
    assert orch._retrieve_chinese_papers("基层治理", 60) == []

    async def zero_saved(**kwargs):
        return {"status": "failed", "error": "saved=0"}

    monkeypatch.setattr(adapter, "run_cnki_full_auto", zero_saved)
    assert orch._retrieve_chinese_papers("基层治理", 60) == []
