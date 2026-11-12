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
import os
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
            {
                "seq": r.seq,
                "event": r.event,
                "payload": r.payload,
                "created_at": str(r.created_at) if r.created_at else None,
            }
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


def _confirm_ttl_minutes() -> int:
    """确认窗口 TTL(分钟):awaiting_confirm 超过此时长即过期。

    无限期的确认窗口会让旧方案被恢复机制反复端给用户(2026-11-11:
    10 小时前的 8 主题旧方案被端回,用户困惑"规则去哪了")。
    """
    try:
        return max(5, int(os.environ.get("WRITING_CONFIRM_TTL_MINUTES", "120")))
    except ValueError:
        return 120


def expire_stale_awaiting() -> int:
    """过期滞留的确认任务:awaiting_confirm 超过 TTL 即置 failed 并清快照。

    在服务启动与活跃任务查询时调用(自清理);过期任务不再被恢复机制
    端给用户,规划快照一并清空(过期方案不作恢复)。
    """
    from db.models import _utcnow, WritingJobModel
    from db.session import SessionLocal
    from datetime import timedelta

    ttl = timedelta(minutes=_confirm_ttl_minutes())
    cutoff = _utcnow() - ttl
    with SessionLocal() as db:
        rows = (
            db.query(WritingJobModel)
            .filter(
                WritingJobModel.status == "awaiting_confirm",
                WritingJobModel.updated_at < cutoff,
            )
            .all()
        )
        for r in rows:
            r.status = "failed"
            r.error = (
                f"主题确认已超时过期(超过 {_confirm_ttl_minutes()} 分钟未确认),"
                "请重新划分主题。"
            )
            r.req_snapshot = {}
        db.commit()
        return len(rows)


def supersede_awaiting_except(user_id: str | None, keep_job_id: str) -> int:
    """新划分任务创建时,同属主旧的 awaiting_confirm 任务一并取代(置 stopped)。

    用户重新划分意味着旧方案作废 —— 旧任务不该继续滞留在确认态,
    也不该再被恢复机制端出来。
    """
    from db.models import WritingJobModel
    from db.session import SessionLocal

    with SessionLocal() as db:
        q = db.query(WritingJobModel).filter(
            WritingJobModel.status == "awaiting_confirm",
            WritingJobModel.job_id != keep_job_id,
        )
        if user_id:
            q = q.filter(WritingJobModel.user_id == user_id)
        rows = q.all()
        for r in rows:
            r.status = "stopped"
            r.error = "已被新的主题划分任务取代"
            r.req_snapshot = {}
        db.commit()
        return len(rows)


def build_quality_report(job_id: str) -> dict:
    """run 质量单:从事件流合成一次运行的结构化质量数据。

    商业化可观测性底座:客服/排查凭此单定位任意一次客户运行的
    阶段耗时 / 重试次数 / 闸门触发 / 覆盖率 / 交付清单,无需复现现场。
    纯事件流合成,零 LLM 成本;字段对缺失事件容错(旧任务/半途任务)。
    """
    from datetime import datetime

    # 全量分页读取(并行写作的逐 token 事件量可能远超单批 limit)
    events: list[dict] = []
    cursor = 0
    while True:
        batch = events_after(job_id, cursor, limit=500)
        if not batch:
            break
        events.extend(batch)
        cursor = batch[-1]["seq"]
    by_name: dict[str, list[dict]] = {}
    for e in events:
        by_name.setdefault(e["event"], []).append(e)

    def first(name: str) -> dict | None:
        lst = by_name.get(name)
        return lst[0] if lst else None

    def last(name: str) -> dict | None:
        lst = by_name.get(name)
        return lst[-1] if lst else None

    def _ts(e: dict | None):
        if not e or not e.get("created_at"):
            return None
        try:
            return datetime.fromisoformat(str(e["created_at"]))
        except ValueError:
            return None

    def _secs(a, b):
        if not a or not b:
            return None
        return round((b - a).total_seconds(), 1)

    # ── 阶段时间轴 ──
    milestones = [
        "start", "screening_started", "screening_done", "classify_started",
        "classify_done", "writing_started", "qa_started", "qa_done", "complete",
    ]
    stages: list[dict] = []
    prev_name, prev_ts = None, None
    for name in milestones:
        e = first(name)
        if not e:
            continue
        t = _ts(e)
        if prev_name is not None:
            stages.append({
                "stage": prev_name, "until_event": name,
                "seconds": _secs(prev_ts, t),
            })
        stages.append({
            "stage": name,
            "at": str(e.get("created_at") or ""),
        })
        prev_name, prev_ts = name, t
    total_duration = _secs(_ts(first("start")), _ts(last("complete")))

    # ── 筛选 ──
    sd = last("screening_done")
    screening = None
    if sd:
        p = sd["payload"] or {}
        screening = {
            "kept": p.get("kept"),
            "screened_out": len(p.get("screened_out") or []),
            "quota_cut": p.get("quota_cut", 0),
        }

    # ── 通读 ──
    rd = last("readthrough_done")
    readthrough = None
    if rd:
        p = rd["payload"] or {}
        readthrough = {
            "papers": p.get("papers"), "chunks": p.get("chunks"),
            "failed_chunks": p.get("failed_chunks"), "notes": p.get("notes"),
        }

    # ── 定轴 + 归组尝试/闸门/降级 ──
    axis_themes = None
    gate_fired = None
    attempts: list[dict] = []
    fallback_used = False
    for e in by_name.get("classify_progress", []):
        p = e["payload"] or {}
        kind = p.get("kind")
        if kind == "axis_plan_done":
            axis_themes = p.get("themes")
        elif kind == "axis_quality_gate":
            gate_fired = p.get("themes")
        elif kind == "llm_result":
            attempts.append({
                "attempt": p.get("attempt"),
                "parsed_groups": p.get("parsed_groups"),
                "valid_groups": p.get("valid_groups"),
                "covered": p.get("covered"),
                "papers": p.get("papers"),
                "decision": p.get("decision"),
            })
        elif kind == "assign_fallback_started":
            fallback_used = True
    grouping = {
        "axis_themes_planned": axis_themes,
        "axis_quality_gate": gate_fired,
        "attempts": attempts,
        "fallback_used": fallback_used,
    }

    # ── 写作章节 ──
    sections = [
        {
            "title": (e["payload"] or {}).get("title"),
            "chars": len((e["payload"] or {}).get("content") or ""),
            "citations": len((e["payload"] or {}).get("citations") or []),
        }
        for e in by_name.get("section_done", [])
    ]

    # ── QA ──
    qa = None
    qd = last("qa_done")
    if qd:
        p = qd["payload"] or {}
        qa = {
            "overall": p.get("overall"),
            "pass_rate": p.get("pass_rate"),
            "auto_repair_count": p.get("auto_repair_count", 0),
        }

    # ── 交付清单 ──
    cm = last("complete")
    manifest = (cm["payload"] or {}).get("manifest") if cm else None

    # ── 错误 ──
    errors = [
        (e["payload"] or {}).get("message", "")
        for e in by_name.get("error", [])
    ]

    job = get_job(job_id) or {}
    return {
        "job_id": job_id,
        "status": job.get("status"),
        "total_events": len(events),
        "total_duration_seconds": total_duration,
        "stages": stages,
        "screening": screening,
        "readthrough": readthrough,
        "grouping": grouping,
        "writing_sections": sections,
        "qa": qa,
        "manifest": manifest,
        "errors": errors,
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
    }


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
