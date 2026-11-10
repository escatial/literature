# -*- coding: utf-8 -*-
"""写作任务档案存取(J1 任务化改造,2026-11)。

设计要点:
- 每次事件落一行(writing_job_events),append-only;(job_id, seq) 主键,
  seq 流内单调 —— 断线重连按 since_seq 续传,重放即恢复全部界面状态;
- job 状态机: running → awaiting_confirm → running(确认后) → done/failed/stopped;
- 全部走独立短会话(worker 线程调用,不占用请求作用域会话);
- 非法迁移状态不在这里校验(调用方是流式 worker,顺序天然正确)。
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

TERMINAL_STATUSES = ("done", "failed", "stopped")


def create_job(
    job_id: str,
    user_id: str | None,
    topic: str,
    classify_mode: str,
    req_snapshot: dict | None,
) -> None:
    from db.models import WritingJobModel
    from db.session import SessionLocal

    with SessionLocal() as db:
        db.add(WritingJobModel(
            job_id=job_id,
            user_id=user_id,
            topic=topic or "",
            classify_mode=classify_mode or "theme",
            status="running",
            phase="planning",
            req_snapshot=req_snapshot,
        ))
        db.commit()
    _prune_old_jobs()


def append_event(job_id: str, seq: int, event: str, payload: dict | None) -> None:
    """落一个事件;job 不存在(旧路径直调)时静默跳过,不影响交付。"""
    from db.models import WritingJobEventModel
    from db.session import SessionLocal

    try:
        with SessionLocal() as db:
            db.add(WritingJobEventModel(
                job_id=job_id, seq=seq, event=event, payload=payload,
            ))
            db.commit()
    except Exception as exc:  # noqa: BLE001  事件落库失败不阻断写作主链
        log.warning("job event 落库失败 job=%s seq=%s: %s", job_id, seq, exc)


def events_after(job_id: str, since_seq: int = 0, limit: int = 200) -> list[dict]:
    """按 seq 升序取 since_seq 之后的事件(重放/续传数据源)。"""
    from db.models import WritingJobEventModel
    from db.session import SessionLocal

    with SessionLocal() as db:
        rows = (
            db.query(WritingJobEventModel)
            .filter(
                WritingJobEventModel.job_id == job_id,
                WritingJobEventModel.seq > since_seq,
            )
            .order_by(WritingJobEventModel.seq.asc())
            .limit(limit)
            .all()
        )
        return [
            {"seq": r.seq, "event": r.event, "payload": r.payload}
            for r in rows
        ]


def max_seq(job_id: str) -> int:
    """当前最大 seq(阶段2确认后继续编号的起点;无事件返回 0)。"""
    from db.models import WritingJobEventModel
    from db.session import SessionLocal
    from sqlalchemy import func

    with SessionLocal() as db:
        return int(
            db.query(func.max(WritingJobEventModel.seq))
            .filter(WritingJobEventModel.job_id == job_id)
            .scalar() or 0
        )


def get_job(job_id: str) -> dict | None:
    from db.models import WritingJobModel
    from db.session import SessionLocal

    with SessionLocal() as db:
        r = db.query(WritingJobModel).filter_by(job_id=job_id).first()
        if r is None:
            return None
        return {
            "job_id": r.job_id,
            "user_id": r.user_id,
            "topic": r.topic,
            "classify_mode": r.classify_mode,
            "status": r.status,
            "phase": r.phase,
            "error": r.error,
            "req_snapshot": r.req_snapshot,
            "created_at": str(r.created_at or ""),
            "updated_at": str(r.updated_at or ""),
        }


def get_job_scoped(job_id: str, user) -> dict | None:
    """按归属过滤取任务(非 admin 只能取自己名下;不存在性统一 404 语义)。"""
    job = get_job(job_id)
    if job is None:
        return None
    role = getattr(user, "role", "")
    uid = getattr(user, "user_id", None)
    if role != "admin" and job["user_id"] != uid:
        return None
    return job


def set_job_status(
    job_id: str,
    status: str,
    phase: str | None = None,
    error: str | None = None,
) -> None:
    from db.models import WritingJobModel
    from db.session import SessionLocal
    from db.models import _utcnow

    with SessionLocal() as db:
        r = db.query(WritingJobModel).filter_by(job_id=job_id).first()
        if r is None:
            return
        r.status = status
        if phase is not None:
            r.phase = phase
        r.error = error
        r.updated_at = _utcnow()
        db.commit()


def stop_job(job_id: str) -> bool:
    """落库置停止位(进程内 Event 由 API 层同步置位保证即时性)。"""
    if get_job(job_id) is None:
        return False
    set_job_status(job_id, "stopped", error=None)
    return True


def mark_stale_running_failed() -> int:
    """启动期收尾:服务重启时还在 running 的任务标记 failed(规划快照仍在,
    用户可直接重试阶段2)。awaiting_confirm 是合法持久状态,不动。"""
    from db.models import WritingJobModel
    from db.session import SessionLocal

    with SessionLocal() as db:
        rows = (
            db.query(WritingJobModel)
            .filter(WritingJobModel.status == "running")
            .all()
        )
        for r in rows:
            r.status = "failed"
            r.error = "服务重启,任务中断。阶段1规划快照已保留,可确认主题后重试写作。"
            r.updated_at = r.updated_at
        db.commit()
        return len(rows)


def _prune_old_jobs(keep: int = 100) -> None:
    """保留策略:仅最近 keep 个任务及其事件(按创建时间)。失败静默。"""
    from db.models import WritingJobEventModel, WritingJobModel
    from db.session import SessionLocal
    from sqlalchemy import delete

    try:
        with SessionLocal() as db:
            stale = (
                db.query(WritingJobModel.job_id)
                .order_by(WritingJobModel.created_at.desc())
                .offset(keep)
                .all()
            )
            ids = [r[0] for r in stale]
            if not ids:
                return
            db.execute(
                delete(WritingJobEventModel)
                .where(WritingJobEventModel.job_id.in_(ids))
            )
            db.execute(
                delete(WritingJobModel).where(WritingJobModel.job_id.in_(ids))
            )
            db.commit()
    except Exception as exc:  # noqa: BLE001
        log.debug("job 保留策略清理失败(忽略): %s", exc)
