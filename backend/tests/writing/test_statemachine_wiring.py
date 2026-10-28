# -*- coding: utf-8 -*-
"""状态机接线回归(C3)。

- 7 节点链:PLANNING → SCREENED → OUTLINED → DRAFTED → QA_VERIFIED →
  RENDERED → DELIVERED;
- 段型阶段异常自动 fail;时点型阶段完成时点 record pass;
- 重跑幂等(record 覆盖未终态行,can_enter 重入);
- 前序缺失宽容降级(状态机不阻断写作主链路)。

测试直接驱动 StateMachine + _StageSpan(orchestrator 的接线原语),
不起真实 LLM 流——生成器全链的 SSE 事件由既有 writing 测试覆盖。
"""
import pytest

import db.session as db_session
from db.models import TaskStageModel
from db.session import Base
from statemachine.controller import STAGES, StateMachine
from writing.orchestrator import _StageSpan, _writing_sm


@pytest.fixture()
def sm_db(tmp_path):
    url = f"sqlite:///{tmp_path / 'sm_wiring.db'}"
    db_session.reset_engine(url)
    Base.metadata.create_all(bind=db_session.engine)
    yield
    db_session.engine.dispose()


def test_stages_chain_is_seven_node():
    assert STAGES == [
        "PLANNING", "SCREENED", "OUTLINED", "DRAFTED",
        "QA_VERIFIED", "RENDERED", "DELIVERED",
    ]


def test_stage_span_pass_records_pass(sm_db):
    """成功路径:enter→exit_ok 落 pass(首个阶段恒可进入)。"""
    sm = _writing_sm("task-ok-1")
    span = _StageSpan(sm, "PLANNING")
    span.__enter__()
    span.exit_ok()
    with db_session.SessionLocal() as session:
        rows = (
            session.query(TaskStageModel)
            .filter_by(task_id="task-ok-1", stage="PLANNING").all()
        )
    assert len(rows) == 1
    assert rows[0].status == "pass"
    assert rows[0].exited_at is not None


def test_stage_span_exception_records_fail(sm_db):
    """异常路径:exit_failed 落 fail 且带 detail(exception)。"""
    sm = _writing_sm("task-fail-1")
    span = _StageSpan(sm, "PLANNING")
    span.__enter__()
    span.exit_failed("章节写作失败:LLM 全失败")
    with db_session.SessionLocal() as session:
        row = (
            session.query(TaskStageModel)
            .filter_by(task_id="task-fail-1", stage="PLANNING").one()
        )
    assert row.status == "fail"
    assert "LLM 全失败" in (row.detail or {}).get("exception", "")


def test_unfulfilled_prerequisite_degrades_to_noop(sm_db):
    """宽容语义锁定:DRAFTED 前序(PLANNING/SCREENED/OUTLINED)未 pass 时,
    _stage_ctx 降级 nullcontext —— 状态机不阻断写作主链路,也不落行。"""
    sm = _writing_sm("task-gate-1")
    span = _StageSpan(sm, "DRAFTED")
    span.__enter__()          # nullcontext 降级,不抛
    span.exit_failed("不应有行")
    span2 = _StageSpan(sm, "DRAFTED")
    span2.__enter__()
    span2.exit_ok()           # 同样 no-op
    with db_session.SessionLocal() as session:
        rows = (
            session.query(TaskStageModel)
            .filter_by(task_id="task-gate-1").all()
        )
    assert rows == []


def test_full_chain_pass_then_next_stage_can_enter(sm_db):
    """全链正路:按序 pass 后,下一阶段 can_enter 满足并可正常落行。"""
    sm = _writing_sm("task-chain-1")
    for stage in ("PLANNING", "SCREENED", "OUTLINED", "DRAFTED"):
        sm.record(stage=stage, status="pass")
    assert sm.can_enter("QA_VERIFIED") is True
    span = _StageSpan(sm, "QA_VERIFIED")
    span.__enter__()
    span.exit_ok()
    with db_session.SessionLocal() as session:
        row = (
            session.query(TaskStageModel)
            .filter_by(task_id="task-chain-1", stage="QA_VERIFIED").one()
        )
    assert row.status == "pass"


def test_rerun_is_idempotent_and_can_reenter(sm_db):
    """重跑:PLANNING 重入(首个阶段恒可进),旧 fail 保留、新 pass 单行。"""
    sm = _writing_sm("task-rerun-1")
    span1 = _StageSpan(sm, "PLANNING")
    span1.__enter__()
    span1.exit_failed("中途崩溃")
    span2 = _StageSpan(sm, "PLANNING")
    span2.__enter__()
    span2.exit_ok()
    with db_session.SessionLocal() as session:
        rows = (
            session.query(TaskStageModel)
            .filter_by(task_id="task-rerun-1", stage="PLANNING")
            .order_by(TaskStageModel.stage_id.asc()).all()
        )
    statuses = [r.status for r in rows]
    assert "fail" in statuses and "pass" in statuses
    assert statuses.count("pass") == 1  # 无重复 pass 行


def test_null_task_id_disables_wiring():
    """task_id 缺省:不接线(None),测试/直调零负担。"""
    assert _writing_sm(None) is None
    assert _writing_sm("") is None
