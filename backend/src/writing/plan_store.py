"""阶段1规划快照存取(C5 断点恢复)。

- 写入:plan-stream 成功收尾(plan_complete 到达)时 UPSERT,按 task_id 幂等;
- 读取:阶段2断点恢复 —— 前端丢状态/换会话后,凭 X-Task-Id 取回规划,
  重新组装写作请求,无需把全量 papers 在前端反复搬运;
- lit_ids 只存引用:screened_lit_ids 指向 papers 表行(papers 是唯一真源),
  恢复取数时经 pool_reader 按 lit_id 回表取全字段;
- 归属:沿用 B6 owner 语义 —— 非 admin 只能读写自己名下的规划。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from db.models import UserModel, WritingPlanModel
# 经模块属性访问 SessionLocal:reset_engine(测试)会重绑 db.session 的全局,
# from-import 绑定会停留在旧 engine 上写穿旧库(与 security/auth.py 同一修复)。
from db import session as db_session

log = logging.getLogger(__name__)


def _is_admin(user: UserModel | None) -> bool:
    return getattr(user, "role", "") == "admin"


def upsert_plan(task_id: str, user_id: str | None, payload: dict) -> WritingPlanModel:
    """按 task_id 幂等写入/覆盖规划快照。payload 原样 JSON 落库。"""
    now = datetime.now(timezone.utc)
    with db_session.SessionLocal() as db:
        row = db.query(WritingPlanModel).filter_by(task_id=task_id).one_or_none()
        if row is not None:
            row.user_id = user_id or row.user_id
            row.topic = str(payload.get("topic") or row.topic or "")
            row.classify_mode = str(payload.get("classify_mode") or row.classify_mode or "theme")
            row.payload = payload
            row.updated_at = now
            db.commit()
            db.refresh(row)
            return row
        row = WritingPlanModel(
            task_id=task_id,
            user_id=user_id,
            topic=str(payload.get("topic") or ""),
            classify_mode=str(payload.get("classify_mode") or "theme"),
            payload=payload,
            created_at=now,
            updated_at=now,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row


def get_plan(task_id: str, user: UserModel | None) -> WritingPlanModel | None:
    """按任务取规划。非 admin 只能取自己名下;不归属按不存在处理(404 语义由调用方)。"""
    with db_session.SessionLocal() as db:
        row = db.query(WritingPlanModel).filter_by(task_id=task_id).one_or_none()
        if row is None:
            return None
        if user is not None and not _is_admin(user):
            if row.user_id != getattr(user, "user_id", None):
                return None
        # JSON 字段 detach 后仍可读(已加载);标量固定
        db.expunge(row)
        return row


def latest_plan(user: UserModel | None) -> WritingPlanModel | None:
    """取该用户最近一条规划(admin 为全库最近一条)。"""
    with db_session.SessionLocal() as db:
        q = db.query(WritingPlanModel)
        if user is not None and not _is_admin(user):
            q = q.filter(WritingPlanModel.user_id == getattr(user, "user_id", None))
        row = q.order_by(WritingPlanModel.updated_at.desc()).first()
        if row is None:
            return None
        db.expunge(row)
        return row


def plan_to_writing_payload(row: WritingPlanModel) -> dict[str, Any]:
    """快照 → C6 阶段2取数所需的中间结构(lit_ids 引用 + 元数据)。"""
    payload = row.payload or {}
    return {
        "task_id": row.task_id,
        "topic": row.topic,
        "classify_mode": row.classify_mode,
        "groups": payload.get("groups") or [],
        "section_titles": payload.get("section_titles") or [],
        "screened_lit_ids": payload.get("screened_lit_ids") or [],
        "relevance_report": payload.get("relevance_report"),
        "blueprint": payload.get("blueprint"),
        "research_questions": payload.get("research_questions"),
        "review_goal": payload.get("review_goal"),
    }


__all__ = [
    "upsert_plan",
    "get_plan",
    "latest_plan",
    "plan_to_writing_payload",
]
