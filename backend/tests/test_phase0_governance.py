# -*- coding: utf-8 -*-
"""Phase 0 治理层验收测试(P0-1~P0-6,2026-11-18)。

覆盖:
- P0-1 检索命中门槛:低于 RETRIEVAL_MIN_POOL_SIZE 判失败+行动指引;
- P0-2/P0-5 错误码归一与行动指引:分类器/格式化器/全码覆盖;
- P0-3/P0-4 上限显式配置:env 覆盖生效、调用方读取配置值;
- P0-6 耗时归因:phase_stats 绑定/快照/no-op + messages_create 计时上报
  + 检索任务落 attribution 事件。
"""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
for p in (str(BACKEND_ROOT / "src"), str(BACKEND_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

import pytest  # noqa: E402

from db.session import Base, reset_engine  # noqa: E402


# ─────────────────────────── P0-4 上限显式配置 ───────────────────────────

def test_limits_defaults_and_env_override(monkeypatch):
    """默认值来自源码常量;env 每次调用即时读取(无 import 固化陷阱)。"""
    from config import limits

    assert limits.retrieval_min_pool_size() == 30
    assert limits.writing_quality_retry_max() == 2
    assert limits.writing_lint_fix_max_rounds() == 2
    assert limits.writing_self_eval_max_rounds() == 2

    monkeypatch.setenv("RETRIEVAL_MIN_POOL_SIZE", "50")
    monkeypatch.setenv("WRITING_QUALITY_RETRY_MAX", "3")
    monkeypatch.setenv("WRITING_LINT_FIX_MAX_ROUNDS", "1")
    monkeypatch.setenv("WRITING_SELF_EVAL_MAX_ROUNDS", "4")
    monkeypatch.setenv("WRITING_QUALITY_RETRY_BOGUS", "abc")  # 非法值不影响无关项
    assert limits.retrieval_min_pool_size() == 50
    assert limits.writing_quality_retry_max() == 3
    assert limits.writing_lint_fix_max_rounds() == 1
    assert limits.writing_self_eval_max_rounds() == 4


def test_self_eval_rounds_reads_limits(monkeypatch):
    """section_writer 的自评轮数走集中配置(P0-3/P0-4 接线)。"""
    from writing import section_writer

    monkeypatch.setenv("WRITING_SELF_EVAL_MAX_ROUNDS", "5")
    assert section_writer._self_eval_max_rounds() == 5
    monkeypatch.setenv("WRITING_SELF_EVAL_MAX_ROUNDS", "junk")
    assert section_writer._self_eval_max_rounds() == 2


# ─────────────────────── P0-2/P0-5 错误码与行动指引 ───────────────────────

def test_every_error_code_has_action_hint():
    """每个错误码必须配行动指引——P0-5 的硬约束:失败消息要回答
    「用户现在要做什么」。"""
    from errors.task_errors import ACTION_HINTS, TaskErrorCode

    for code in TaskErrorCode:
        hint = ACTION_HINTS[code]
        assert hint.strip(), f"错误码 {code} 缺行动指引"
        # 指引必须含可执行动词或明确出路,不许只有「发生错误」
        assert any(k in hint for k in ("请", "建议", "重新", "检查", "稍后")), (
            f"错误码 {code} 的指引不像行动指引: {hint}"
        )


def test_format_task_error_structure():
    """统一格式:[错误码 x] 指引 | 详情: 原文——归一不吞原始信息。"""
    from errors.task_errors import TaskErrorCode, format_task_error

    out = format_task_error(TaskErrorCode.TASK_CANCELLED, detail="用户已手动停止")
    assert out.startswith("[错误码 task_cancelled]")
    assert "如需继续请重新发起" in out          # 指引段
    assert "详情: 用户已手动停止" in out        # 详情段原文保留

    out2 = format_task_error(
        TaskErrorCode.RETRIEVAL_HIT_COUNT_LOW,
        detail="实际命中 12 篇", actual=12, threshold=30,
    )
    assert "低于最低门槛 30 篇" in out2         # 占位符已填充
    assert "实际命中 12 篇" in out2


def test_classify_exception_duck_typed_code():
    """crawler 异常带 code 属性 → 直接映射,无需 import 重模块。"""
    from errors.task_errors import TaskErrorCode, classify_exception

    class FakeCnkiCookieError(Exception):
        code = "cookie_expired"

    assert classify_exception(FakeCnkiCookieError("x")) == TaskErrorCode.COOKIE_EXPIRED

    class FakeTaskCancelled(Exception):
        pass

    # TaskCancelledError 按类型名识别(避免跨模块 import)
    FakeTaskCancelled.__name__ = "TaskCancelledError"
    assert classify_exception(FakeTaskCancelled()) == TaskErrorCode.TASK_CANCELLED


def test_classify_message_llm_signatures():
    from errors.task_errors import TaskErrorCode, classify_message

    assert classify_message(
        "LLM 调用所有候选 provider 失败(a, b): boom"
    ) == TaskErrorCode.LLM_ALL_PROVIDERS_FAILED
    assert classify_message("429 rate limit hit") == TaskErrorCode.LLM_RATE_LIMIT
    assert classify_message("未知奇怪错误") == TaskErrorCode.INTERNAL_ERROR


# ─────────────────────────── P0-6 耗时归因 ───────────────────────────

def test_phase_stats_bind_snapshot_noop():
    """未绑定时 no-op;绑定后累计;reset 后恢复未绑定。"""
    from telemetry import phase_stats as ps

    assert ps.snapshot() is None
    ps.add_llm_call(1.0, True)  # no-op,不抛
    assert ps.snapshot() is None

    token = ps.bind_task_stats("task-x")
    ps.add_llm_call(1.5, True)
    ps.add_llm_call(0.5, False)
    snap = ps.snapshot()
    assert snap == {
        "task_id": "task-x",
        "llm_calls": 2,
        "llm_failures": 1,
        "llm_seconds": 2.0,
    }
    ps.reset_task_stats(token)
    assert ps.snapshot() is None


def test_messages_create_records_stats(monkeypatch):
    """messages_create 计时外壳:成功/失败各记一条,含 fallback 总耗时。"""
    import time as _time

    import llm.client as client

    calls = []

    def fake_impl(**kwargs):
        _time.sleep(0.01)
        calls.append(kwargs)
        if kwargs.get("user") == "boom":
            raise RuntimeError("LLM 调用所有候选 provider 失败(x): y")
        return "ok"

    monkeypatch.setattr(client, "_messages_create_impl", fake_impl)

    from telemetry import phase_stats as ps
    token = ps.bind_task_stats("job-stats")
    try:
        out = client.messages_create(system="s", user="fine")
        assert out == "ok"
        with pytest.raises(RuntimeError):
            client.messages_create(system="s", user="boom")
    finally:
        ps.reset_task_stats(token)

    snap = ps.snapshot()
    assert snap is None  # reset 后恢复;累计期间断言如下

    token2 = ps.bind_task_stats("job-stats2")
    try:
        client.messages_create(system="s", user="fine")
        snap = ps.snapshot()
        assert snap["llm_calls"] == 1
        assert snap["llm_seconds"] > 0
        assert snap["llm_failures"] == 0
        # 归因埋点异常不影响主链
        monkeypatch.setattr(
            "telemetry.phase_stats.add_llm_call",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")),
        )
        assert client.messages_create(system="s", user="fine") == "ok"
    finally:
        ps.reset_task_stats(token2)


# ─────────────────── P0-1 检索命中门槛(英文 v2 流程) ───────────────────

@pytest.fixture()
def fresh_db(tmp_path):
    """独立临时库:测试不写真实 data/lit_review.db。"""
    reset_engine(f"sqlite:///{tmp_path / 'gate_test.db'}")
    import db.models  # noqa: F401
    Base.metadata.create_all(bind=reset_engine(f"sqlite:///{tmp_path / 'gate_test.db'}"))
    yield
    reset_engine("sqlite:///:memory:")


def _paper(i: int):
    from retrieval.types import Paper, Source

    return Paper(
        lit_id=f"gate{i:04d}0000000000", source=Source.OPENALEX,
        title=f"门槛测试文献 {i}", authors=["张三"], journal="测试刊",
        year=2024,
    )


class _FakePool:
    def __init__(self, papers):
        self.papers = papers


class _FakeController:
    def __init__(self, *, papers_count, **kwargs):
        self._papers_count = papers_count

    async def run_async(self):
        return _FakePool([_paper(i) for i in range(self._papers_count)])


@pytest.fixture()
def planned(monkeypatch):
    """绕开 LLM 规划与 DNS 预热。"""
    import retrieval.task_service as ts

    monkeypatch.setattr(ts, "plan_query_strings", lambda topic, year=None: {
        "topic_summary": topic,
        "queries_cnki": [topic] * 3,
        "queries_openalex": [topic] * 3,
        "queries_pubmed": [topic] * 3,
    })
    monkeypatch.setattr(ts, "_warmup_dns", lambda hosts: None)


def test_retrieval_hit_gate_blocks_small_pool(monkeypatch, fresh_db, planned):
    """命中 3 篇 < 门槛 5 → failed + RETRIEVAL_HIT_COUNT_LOW + 指引。"""
    import retrieval.task_service as ts
    import config.limits as limits

    monkeypatch.setattr(limits, "retrieval_min_pool_size", lambda: 5)
    monkeypatch.setattr(ts, "RetrievalController",
                        lambda **kw: _FakeController(papers_count=3))

    # run_inline=True:同线程同步跑。run_inline=False 会起后台线程再跑一遍
    # impl,与下方直调并发双跑,终态由谁后写谁说了算——flaky 的根源。
    task = ts.create_task_v2("测试主题", run_inline=True)

    row = ts.get_task(task.task_id)
    assert row.status == "failed"
    assert "[错误码 retrieval_hit_count_low]" in (row.error or "")
    assert "低于最低门槛 5 篇" in (row.error or "")
    assert "详情: 实际命中 3 篇" in (row.error or "")
    # 已抓到的篇目仍随失败记录留存(可见不丢弃)
    assert len(row.papers or []) == 3


def test_retrieval_hit_gate_passes_healthy_pool(monkeypatch, fresh_db, planned):
    """命中 40 篇 ≥ 门槛 5 → 正常走完入库与终态,不触发门槛。"""
    import retrieval.task_service as ts
    import config.limits as limits

    monkeypatch.setattr(limits, "retrieval_min_pool_size", lambda: 5)
    monkeypatch.setattr(ts, "RetrievalController",
                        lambda **kw: _FakeController(papers_count=40))
    monkeypatch.setattr(ts.pool_writer, "upsert_with_overwrite",
                        lambda papers, **kw: {"inserted": len(papers), "updated": 0,
                                              "failed_by_source": {}})

    # run_inline=True:同线程同步跑,避免后台线程双跑竞态(见上条测试注释)
    task = ts.create_task_v2("测试主题", run_inline=True)

    row = ts.get_task(task.task_id)
    assert row.status == "succeeded"
    assert row.total_after_filter == 40
    # P0-6:归因走列不走事件流(事件流是前端契约,未知 stage 有展示风险)
    assert row.retrieval_seconds is not None and row.retrieval_seconds >= 0
    # run_task_v2 绑定了统计器;无 LLM 调用故累计为 0
    assert (row.llm_stats or {}).get("llm_calls") == 0
