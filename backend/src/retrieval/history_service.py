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

from sqlalchemy import func, select

import db.session as _db_session
from db.models import PaperModel, RetrievalHistoryModel
from retrieval.paper_identity import build_identity_key, repair_paper_fields
from retrieval.types import Paper


_HISTORY_KEEP = 5  # 仅保留最近 5 条


def _pool_total(db, task_id: str | None, fallback: int) -> int:
    """v8.6 统一口径:历史「文献总数」= 文献池实际行数(按 task_id 数 papers 表)。

    检索侧合并去重(lit_id)与入库侧二次去重(identity_key)+ provenance 淘汰
    存在差值(如检索 135 / 入库 132),此前两处显示打架;
    现在与汇总面板「入库文献」同一数据源。无 task_id(老记录)回退历史存量值。
    """
    if not task_id:
        return fallback
    return db.execute(
        select(func.count()).select_from(PaperModel).where(PaperModel.task_id == task_id)
    ).scalar() or 0


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
    run_id: str | None = None,
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
            run_id=run_id,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        out = {
            "id": row.id,
            "topic": row.topic,
            "sources": list(row.sources or []),
            "total_count": _pool_total(db, row.task_id, int(row.total_count or 0)),
            "failed_sources": dict(row.failed_sources or {}),
            "papers_snapshot": list(row.papers_snapshot or []),
            "task_id": row.task_id,
            "run_id": row.run_id,
            "created_at": _iso_utc(row.created_at),
        }
        _trim(db, keep=keep)
        return out


def upsert_history_by_run_id(
    *,
    run_id: str,
    topic: str,
    sources: list[str],
    papers: list[Paper],
    failed_sources: dict[str, int] | None = None,
    task_id: str | None = None,
    keep: int = _HISTORY_KEEP,
) -> dict:
    """按 run_id 幂等写入检索历史:已有同 run 记录则更新,没有则插入。

    v8.2 统计口径修正:一次「启动自动检索」= 一条历史记录。
    中文/英文两半可能分批到达(聚合器先到先写、迟到半边补写),
    必须落回同一条记录合并总数,而不是拆成两条各记一半。
    """
    snapshot = [_paper_to_snapshot(p) for p in papers]
    with _db_session.SessionLocal() as db:
        row = None
        if run_id:
            row = (
                db.execute(
                    select(RetrievalHistoryModel)
                    .where(RetrievalHistoryModel.run_id == run_id)
                    .order_by(RetrievalHistoryModel.created_at.desc())
                    .limit(1)
                )
                .scalars()
                .first()
            )
        if row is None:
            row = RetrievalHistoryModel(topic=topic, run_id=run_id)
            db.add(row)
        row.topic = topic
        row.sources = list(sources)
        row.total_count = len(papers)
        row.failed_sources = dict(failed_sources or {})
        row.papers_snapshot = snapshot
        row.task_id = task_id
        db.commit()
        db.refresh(row)
        out = {
            "id": row.id,
            "topic": row.topic,
            "sources": list(row.sources or []),
            "total_count": _pool_total(db, row.task_id, int(row.total_count or 0)),
            "failed_sources": dict(row.failed_sources or {}),
            "papers_snapshot": list(row.papers_snapshot or []),
            "task_id": row.task_id,
            "run_id": row.run_id,
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
        "volume": p.volume,
        "issue": p.issue,
        "pages": p.pages,
        "abstract": p.abstract,
        "source": str(p.source.value if hasattr(p.source, "value") else p.source),
        "doi": p.doi or "",
        "source_url": p.source_url or "",
        "cited_by_count": int(p.cited_by_count or 0),
        "journal_level": p.journal_level,
        "relevance_score": p.relevance_score,
        "provenance": p.provenance,
        "raw_citation": p.raw_citation,
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
                "total_count": _pool_total(db, r.task_id, int(r.total_count or 0)),
                "failed_sources": dict(r.failed_sources or {}),
                "papers_snapshot": list(r.papers_snapshot or []),
                "task_id": r.task_id,
                "run_id": r.run_id,
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
            "total_count": _pool_total(db, r.task_id, int(r.total_count or 0)),
            "failed_sources": dict(r.failed_sources or {}),
            "papers_snapshot": list(r.papers_snapshot or []),
            "task_id": r.task_id,
            "run_id": r.run_id,
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


def restore_to_pool(history_id: int, pool_task_id: str | None = None) -> int:
    """把某条历史检索的文献快照恢复到文献池(先清空池再写入),返回恢复条数。

    语义:文献池是「当前工作区」,查看历史即加载该条历史的文献快照。
    v7.1:pool_task_id 非空时,只清该任务的池、写入打上 task_id 标签,
    否则恢复后文献池按 X-Task-Id 过滤会显示 0 条。
    """
    rec = get_history(history_id)
    if not rec:
        raise ValueError(f"history {history_id} not found")
    snapshot = rec.get("papers_snapshot") or []
    with _db_session.SessionLocal() as db:
        legacy_ids = [
            s.get("lit_id")
            for s in snapshot
            if s.get("lit_id")
            and not (s.get("abstract") or s.get("abstract_text"))
        ]
        existing_abstracts = {
            row.lit_id: row.abstract_text or row.abstract
            for row in db.execute(
                select(PaperModel).where(PaperModel.lit_id.in_(legacy_ids))
            ).scalars()
            if row.abstract_text or row.abstract
        }
        del_q = db.query(PaperModel)
        if pool_task_id:
            del_q = del_q.filter(PaperModel.task_id == pool_task_id)
        del_q.delete(synchronize_session=False)
        for s in snapshot:
            payload = repair_paper_fields(dict(s))
            payload["abstract"] = payload.get("abstract") or existing_abstracts.get(s.get("lit_id")) or None
            source = str(payload.get("source") or "openalex")
            db.add(
                PaperModel(
                    lit_id=payload.get("lit_id") or "",
                    identity_key=build_identity_key(
                        source=source, title=str(payload.get("title") or ""),
                        authors=list(payload.get("authors") or []),
                        year=int(payload.get("year") or 0), doi=str(payload.get("doi") or ""),
                    ),
                    source=source,
                    title=payload.get("title") or "",
                    authors=list(payload.get("authors") or []),
                    journal=payload.get("journal") or "",
                    year=int(payload.get("year") or 0),
                    volume=payload.get("volume") or None,
                    issue=payload.get("issue") or None,
                    pages=payload.get("pages") or None,
                    abstract=payload.get("abstract"),
                    doi=payload.get("doi") or "",
                    source_url=payload.get("source_url") or "",
                    cited_by_count=int(payload.get("cited_by_count") or 0),
                    journal_level=payload.get("journal_level") or None,
                    relevance_score=payload.get("relevance_score"),
                    provenance=payload.get("provenance"),
                    raw_citation=payload.get("raw_citation") or None,
                    quote_text=payload.get("quote_text") or None,
                    abstract_text=payload.get("abstract_text") or payload.get("abstract"),
                    task_id=pool_task_id,
                    selected=True,
                )
            )
        db.commit()
    return len(snapshot)


def delete_history_record(history_id: int, pool_task_id: str | None = None) -> bool:
    """删除一条检索历史,并把该次检索导入文献池的文献一并删干净。

    v7.2:此前只删历史行,papers 表残留该次检索的全部文献。
    - 按快照中的 lit_id 匹配 papers;
    - pool_task_id(X-Task-Id)非空时限定只删当前任务池,绝不误伤
      __legacy__ / 其他任务的文献;
    - pool_task_id 为空时只删历史行(无任务上下文不做大范围删除)。
    返回是否删除成功。
    """
    with _db_session.SessionLocal() as db:
        row = db.get(RetrievalHistoryModel, history_id)
        if not row:
            return False
        if pool_task_id:
            lit_ids = [
                (s or {}).get("lit_id")
                for s in (row.papers_snapshot or [])
                if (s or {}).get("lit_id")
            ]
            if lit_ids:
                db.query(PaperModel).filter(
                    PaperModel.task_id == pool_task_id,
                    PaperModel.lit_id.in_(lit_ids),
                ).delete(synchronize_session=False)
        db.delete(row)
        db.commit()
    return True


__all__ = [
    "record_history", "list_recent", "get_history",
    "restore_to_pool", "delete_history_record", "_HISTORY_KEEP",
]
