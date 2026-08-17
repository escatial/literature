"""统一检索历史(需求4)。

保留最近 5 条历史记录,字段包含检索时间、检索关键词、检索到的文献总数量
和文献元数据。提供:
- 记录本次检索:record_history(成功后由 task_service 自动调用)
- 列出最近 N 条:list_recent(默认 5)
- 重新发起:从历史拿 topic 重跑 create_task_v2
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

import db.session as _db_session
from db.models import PaperModel, RetrievalHistoryModel
from retrieval.types import Paper


_HISTORY_KEEP = 5  # 仅保留最近 5 条


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso_utc(dt: datetime | None) -> str | None:
    """统一把 datetime 序列化为带 Z 后缀的 UTC ISO 字符串。

    历史数据 / papers.created_at 等若存的是不带 tzinfo 的 naive datetime(由 SQLAlchemy
    写入 SQLite 时丢失 tzinfo),默认视为 UTC 补 +00:00。这样前端 new Date('...Z')
    会按 UTC 解析,再按浏览器本地时区(Asia/Shanghai)显示,避免「8 小时时差」bug。
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    # isoformat() 在带 tz 时返回 +00:00;前端期望 Z(更紧凑)
    return dt.isoformat().replace("+00:00", "Z")


def record_history(
    *,
    topic: str,
    sources: list[str],
    papers: list[Paper],
    failed_sources: dict[str, int] | None = None,
    task_id: str | None = None,
    keep: int = _HISTORY_KEEP,
) -> dict:
    """写入一条检索历史,并裁剪到最近 keep 条。

    papers_snapshot 只存关键元数据,避免无限膨胀。
    返回 dict(避免 ORM 实例在 session 关闭后 detached 触发访问失败)。
    """
    snapshot = [_paper_to_snapshot(p) for p in papers]
    with _db_session.SessionLocal() as db:
        row = RetrievalHistoryModel(
            topic=topic,
            sources=list(sources),
            total_count=len(papers),
            failed_sources=dict(failed_sources or {}),
            papers_snapshot=snapshot,
            task_id=task_id,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        out = {
            "id": row.id,
            "topic": row.topic,
            "sources": list(row.sources or []),
            "total_count": int(row.total_count or 0),
            "failed_sources": dict(row.failed_sources or {}),
            "papers_snapshot": list(row.papers_snapshot or []),
            "task_id": row.task_id,
            "created_at": _iso_utc(row.created_at),
        }
        _trim(db, keep=keep)
        return out


def _paper_to_snapshot(p: Paper) -> dict[str, Any]:
    """Paper -> 简洁字典(给前端表格展示用)。"""
    return {
        "lit_id": p.lit_id,
        "title": p.title,
        "authors": list(p.authors or []),
        "journal": p.journal or "",
        "year": int(p.year or 0),
        "source": str(p.source.value if hasattr(p.source, "value") else p.source),
        "doi": p.doi or "",
    }


def list_recent(limit: int = _HISTORY_KEEP) -> list[dict]:
    """获取最近 limit 条历史(按 created_at 倒序),返回 dict 列表。"""
    with _db_session.SessionLocal() as db:
        stmt = (
            select(RetrievalHistoryModel)
            .order_by(RetrievalHistoryModel.created_at.desc())
            .limit(limit)
        )
        rows = list(db.execute(stmt).scalars().all())
        return [
            {
                "id": r.id,
                "topic": r.topic,
                "sources": list(r.sources or []),
                "total_count": int(r.total_count or 0),
                "failed_sources": dict(r.failed_sources or {}),
                "papers_snapshot": list(r.papers_snapshot or []),
                "task_id": r.task_id,
                "created_at": _iso_utc(r.created_at),
            }
            for r in rows
        ]


def get_history(history_id: int) -> dict | None:
    with _db_session.SessionLocal() as db:
        r = db.get(RetrievalHistoryModel, history_id)
        if not r:
            return None
        return {
            "id": r.id,
            "topic": r.topic,
            "sources": list(r.sources or []),
            "total_count": int(r.total_count or 0),
            "failed_sources": dict(r.failed_sources or {}),
            "papers_snapshot": list(r.papers_snapshot or []),
            "task_id": r.task_id,
            "created_at": _iso_utc(r.created_at),
        }
    return None


def _trim(db, keep: int) -> None:
    """保留最新 keep 条,其余删除。"""
    stale = db.execute(
        select(RetrievalHistoryModel.id)
        .order_by(RetrievalHistoryModel.created_at.desc())
        .offset(keep)
    ).scalars().all()
    if not stale:
        return
    for hid in stale:
        row = db.get(RetrievalHistoryModel, hid)
        if row:
            db.delete(row)
    db.commit()


def restore_to_pool(history_id: int) -> int:
    """把某条历史检索的文献快照恢复到文献池(先清空池再写入),返回恢复条数。

    语义:文献池是「当前工作区」,查看历史即加载该条历史的文献快照。
    """
    rec = get_history(history_id)
    if not rec:
        raise ValueError(f"history {history_id} not found")
    snapshot = rec.get("papers_snapshot") or []
    with _db_session.SessionLocal() as db:
        db.query(PaperModel).delete(synchronize_session=False)
        for s in snapshot:
            db.add(
                PaperModel(
                    lit_id=s.get("lit_id") or "",
                    source=s.get("source") or "openalex",
                    title=s.get("title") or "",
                    authors=list(s.get("authors") or []),
                    journal=s.get("journal") or "",
                    year=int(s.get("year") or 0),
                    doi=s.get("doi") or "",
                    selected=True,
                )
            )
        db.commit()
    return len(snapshot)


def delete_history_record(history_id: int) -> bool:
    """删除一条检索历史(含其数据库中的快照数据)。返回是否删除成功。"""
    with _db_session.SessionLocal() as db:
        row = db.get(RetrievalHistoryModel, history_id)
        if not row:
            return False
        db.delete(row)
        db.commit()
    return True


__all__ = [
    "record_history", "list_recent", "get_history",
    "restore_to_pool", "delete_history_record", "_HISTORY_KEEP",
]
