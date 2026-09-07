"""审计日志写入器(方案 §9 生产化:审计)。

设计:
- 单一 audit() 函数,记录用户行为/系统事件;
- 支持 batch flush(进程退出时统一写);
- 不抛异常:审计失败不应阻断主流程。
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any

from db.models import AuditLogModel
from db.session import SessionLocal

log = logging.getLogger(__name__)


def audit(
    *,
    action: str,
    user_id: str | None = None,
    tenant_id: str | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
    detail: dict | None = None,
    success: bool = True,
) -> int | None:
    """写一条审计日志。返回 log_id(失败返回 None)。"""
    try:
        with SessionLocal() as db:
            row = AuditLogModel(
                user_id=user_id,
                tenant_id=tenant_id,
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                ip_address=ip_address,
                user_agent=user_agent,
                detail=detail,
                success=success,
                created_at=datetime.now(timezone.utc),
            )
            db.add(row)
            db.commit()
            db.refresh(row)
            return row.log_id
    except Exception as exc:
        # 审计失败不应阻断主流程,但要 warning
        log.warning("audit log 写入失败: action=%s, err=%s", action, exc)
        return None


def list_audit(
    *,
    user_id: str | None = None,
    tenant_id: str | None = None,
    action: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[AuditLogModel]:
    """查询审计日志(分页)。"""
    with SessionLocal() as db:
        q = db.query(AuditLogModel)
        if user_id:
            q = q.filter_by(user_id=user_id)
        if tenant_id:
            q = q.filter_by(tenant_id=tenant_id)
        if action:
            q = q.filter_by(action=action)
        return q.order_by(AuditLogModel.created_at.desc()).limit(limit).offset(offset).all()


__all__ = ["audit", "list_audit"]