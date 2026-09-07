"""任务状态机控制器(方案 §5 "端到端流程与状态机")。

13 节点状态机(按顺序):
  PLANNING → RETRIEVING → NORMALIZED → VERIFY_1 → SCREENED → VERIFY_2 →
  OUTLINED → DRAFTED → VERIFY_3 → CITATION_LOCKED →
  HUMANIZED_1 → HUMANIZED_2 → HUMANIZED_3 → RENDERED → DELIVERED

设计要点:
- 每个节点产生一条 task_stages 记录,记录 entered_at / exited_at / status;
- 进入下一节点时,前一节点必须 status != fail,否则阻断;
- 任意节点可调用 checkpoint() 持久化当前快照,重启任务可从 last_checkpoint 恢复;
- 节点可重入:若当前 task 在 HUMANIZED_1 阶段崩溃,
  下次启动 task 时从 HUMANIZED_1 续跑,无需重做 PLANNING~OUTLINED。
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from db.models import TaskStageModel
from db.session import SessionLocal

log = logging.getLogger(__name__)

STAGES: list[str] = [
    "PLANNING",
    "RETRIEVING",
    "NORMALIZED",
    "VERIFY_1",
    "SCREENED",
    "VERIFY_2",
    "OUTLINED",
    "DRAFTED",
    "VERIFY_3",
    "CITATION_LOCKED",
    "HUMANIZED_1",
    "HUMANIZED_2",
    "HUMANIZED_3",
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
    """13 节点状态机控制器。

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
        with SessionLocal() as db:
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
        with SessionLocal() as db:
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
        with SessionLocal() as db:
            passed = {
                r.stage for r in db.query(TaskStageModel)
                .filter(TaskStageModel.task_id == self.task_id, TaskStageModel.status == "pass")
                .all()
            }
        return all(s in passed for s in prev_stages)

    def record(self, *, stage: str, status: str, detail: dict | None = None) -> TaskStageModel:
        """落库一条 task_stage(允许幂等覆盖同一阶段最近一次状态)。"""
        with SessionLocal() as db:
            row = TaskStageModel(
                task_id=self.task_id,
                stage=stage,
                status=status,
                detail=detail,
                entered_at=datetime.now(timezone.utc),
                exited_at=datetime.now(timezone.utc) if status in ("pass", "fail", "skipped") else None,
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