"""任务状态机控制器(2026-10-27 C3 收敛为与执行链一致的 7 节点)。

7 节点状态机(按顺序):
  PLANNING → SCREENED → OUTLINED → DRAFTED → QA_VERIFIED → RENDERED → DELIVERED

语义对照(与 orchestrator 执行链一一对应):
  PLANNING    主题规划整体段(筛选+分级+分类;plan 阶段1与单阶段 generate 共用)
  SCREENED    筛选+相关性分级完成的时点
  OUTLINED    主题划分/分类完成的时点(plan_complete / classify_done)
  DRAFTED     分章写作主循环段
  QA_VERIFIED 全流程核查段(三级修复闭环)
  RENDERED    正文定稿交付给前端(sections_finalized / complete)的时点
  DELIVERED   综述交付完成的时点(docx 导出由用户在 reviews 处按需触发)

设计要点:
- 每个节点产生一条 task_stages 记录,记录 entered_at / exited_at / status;
- 进入下一节点时,前一节点必须 status != fail,否则阻断(can_enter);
- 时点型节点(SCREENED/OUTLINED/RENDERED/DELIVERED)由调用方在完成时点
  直接 record(status="pass");段型节点(PLANNING/DRAFTED/QA_VERIFIED)用
  sm.stage() with 块包裹,异常自动 fail;
- record 幂等:同一 (task_id, stage) 未终态行就地覆盖,重跑安全。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from db.models import TaskStageModel
# 经模块属性访问 SessionLocal:reset_engine(测试)会重绑 db.session 的全局,
# from-import 绑定会停留在旧 engine 上写穿旧库(与 security/auth.py 同一修复)。
from db import session as db_session

log = logging.getLogger(__name__)

STAGES: list[str] = [
    "PLANNING",
    "SCREENED",
    "OUTLINED",
    "DRAFTED",
    "QA_VERIFIED",
    "RENDERED",
    "DELIVERED",
]


@dataclass
class StageContext:
    """单个节点执行时的上下文。"""

    task_id: str
    stage: str
    status: str = "running"  # running / pass / fail / skipped
    detail: dict | None = None


class StateMachine:
    """7 节点状态机控制器。

    用法:
        sm = StateMachine(task_id="task_abc")
        with sm.stage("PLANNING") as ctx:
            ...  # 干活
            ctx.status = "pass"
    """

    def __init__(self, task_id: str):
        self.task_id = task_id

    def current_stage(self) -> str | None:
        """返回最近一个 status='running' 或最后通过的阶段名。"""
        with db_session.SessionLocal() as db:
            last_running = (
                db.query(TaskStageModel)
                .filter_by(task_id=self.task_id, status="running")
                .order_by(TaskStageModel.stage_id.desc())
                .first()
            )
            if last_running is not None:
                return last_running.stage
            last_pass = (
                db.query(TaskStageModel)
                .filter_by(task_id=self.task_id, status="pass")
                .order_by(TaskStageModel.stage_id.desc())
                .first()
            )
            return last_pass.stage if last_pass else None

    def checkpoint(self) -> str | None:
        """返回最后一个 status=pass 的阶段名(用于断点续跑)。"""
        with db_session.SessionLocal() as db:
            row = (
                db.query(TaskStageModel)
                .filter_by(task_id=self.task_id, status="pass")
                .order_by(TaskStageModel.stage_id.desc())
                .first()
            )
            return row.stage if row else None

    def can_enter(self, stage: str) -> bool:
        """判断能否进入 stage(它的前序阶段必须都 pass)。"""
        if stage not in STAGES:
            raise ValueError(f"未知阶段:{stage}")
        idx = STAGES.index(stage)
        if idx == 0:
            return True
        prev_stages = STAGES[:idx]
        with db_session.SessionLocal() as db:
            passed = {
                r.stage for r in db.query(TaskStageModel)
                .filter(TaskStageModel.task_id == self.task_id, TaskStageModel.status == "pass")
                .all()
            }
        return all(s in passed for s in prev_stages)

    def record(self, *, stage: str, status: str, detail: dict | None = None) -> TaskStageModel:
        """落库一条 task_stage(允许幂等覆盖同一阶段最近一次状态)。

        v9.6:实现真正的幂等——同一 (task_id, stage) 存在未终态(status 不在
        pass/fail/skipped)的行时就地覆盖;否则插入新行保留历史。此前只插不
        覆盖,崩溃重跑后 current_stage 查询会停留在旧阶段。
        """
        now = datetime.now(timezone.utc)
        exited = now if status in ("pass", "fail", "skipped") else None
        with db_session.SessionLocal() as db:
            row = (
                db.query(TaskStageModel)
                .filter(
                    TaskStageModel.task_id == self.task_id,
                    TaskStageModel.stage == stage,
                    TaskStageModel.status.notin_(("pass", "fail", "skipped")),
                )
                .order_by(TaskStageModel.entered_at.desc())
                .first()
            )
            if row is not None:
                row.status = status
                row.detail = detail
                row.entered_at = now
                row.exited_at = exited
                db.commit()
                db.refresh(row)
                return row
            row = TaskStageModel(
                task_id=self.task_id,
                stage=stage,
                status=status,
                detail=detail,
                entered_at=now,
                exited_at=exited,
            )
            db.add(row)
            db.commit()
            db.refresh(row)
            return row

    def stage(self, name: str):
        """上下文管理器:进入 name 阶段;退出时落库。"""
        return _StageScope(self, name)


class _StageScope:
    """Stage 上下文管理器。

    进入时:can_enter 检查 + 写 status='running';
    退出时:若 ctx.status='running'(用户没改)→ 视作 fail;
           否则按 ctx.status 写 pass/fail/skipped,并设置 exited_at。
    """

    def __init__(self, sm: StateMachine, name: str):
        self.sm = sm
        self.name = name
        self.ctx = StageContext(task_id=sm.task_id, stage=name, status="running")
        self._exited = False

    def __enter__(self) -> StageContext:
        if not self.sm.can_enter(self.name):
            raise RuntimeError(f"不能进入阶段 {self.name},前序阶段未通过")
        # 写 running
        self.sm.record(stage=self.name, status="running")
        log.info("[%s] enter stage %s", self.sm.task_id, self.name)
        return self.ctx

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        if self._exited:
            return False
        self._exited = True
        if exc_type is not None:
            self.ctx.status = "fail"
            self.ctx.detail = {
                **(self.ctx.detail or {}),
                "exception": f"{exc_type.__name__}: {exc_val}",
            }
        if self.ctx.status == "running":
            # 用户没显式设置,默认 fail(防止默默通过)
            self.ctx.status = "fail"
            self.ctx.detail = {**(self.ctx.detail or {}), "warning": "stage 未显式设置 status,默认 fail"}
        self.sm.record(
            stage=self.name,
            status=self.ctx.status,
            detail=self.ctx.detail,
        )
        log.info(
            "[%s] exit stage %s -> %s",
            self.sm.task_id, self.name, self.ctx.status,
        )
        return False  # 不吞异常


__all__ = ["StateMachine", "StageContext", "STAGES"]