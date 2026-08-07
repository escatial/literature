"""英文检索后台任务服务。"""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import select

from db.models import PaperModel, RetrievalTaskModel
from db.session import SessionLocal
from retrieval.filters import by_min_citations, by_year, deduplicate
from retrieval.openalex_adapter import OpenAlexAdapter
from retrieval.query_planner import plan_query
from retrieval.reranker import rerank


def _utcnow() -> datetime:
    """统一的 UTC 当前时间(Python 3.12+ datetime.utcnow 已弃用)。"""
    return datetime.now(timezone.utc)


TERMINAL_STATUSES = {"succeeded", "failed"}


def create_task(
    topic: str,
    year_start: int = 2020,
    year_end: int = 2026,
    min_citations: int = 0,
    limit: int = 50,
    use_rerank: bool = True,
    run_inline: bool = False,
) -> RetrievalTaskModel:
    """创建检索任务。默认后台线程执行;测试可用 run_inline 同步执行。"""
    task_id = str(uuid4())
    with SessionLocal() as db:
        task = RetrievalTaskModel(
            task_id=task_id,
            topic=topic,
            status="pending",
            progress=0,
            year_start=year_start,
            year_end=year_end,
            min_citations=min_citations,
            limit=limit,
            use_rerank=use_rerank,
            updated_at=_utcnow(),
        )
        db.add(task)
        db.commit()
        db.refresh(task)

    if run_inline:
        run_task(task_id)
    else:
        thread = threading.Thread(target=run_task, args=(task_id,), daemon=True)
        thread.start()

    with SessionLocal() as db:
        return db.get(RetrievalTaskModel, task_id)


def list_tasks(limit: int = 20) -> list[RetrievalTaskModel]:
    with SessionLocal() as db:
        stmt = select(RetrievalTaskModel).order_by(RetrievalTaskModel.created_at.desc()).limit(limit)
        return list(db.execute(stmt).scalars().all())


def get_task(task_id: str) -> RetrievalTaskModel | None:
    with SessionLocal() as db:
        return db.get(RetrievalTaskModel, task_id)


def delete_task(task_id: str) -> dict:
    """删除单个检索任务,并把该任务当年入库的英文文献从文献池一并清除。

    返回 {"task_deleted": bool, "papers_deleted": int,
           "task_status": "...", "papers_existed": int}
    用于前端展示与调试。

    重要说明:
    - 仅删除 source='openalex'(本次自动入库)的论文
    - 用户手动补过 source='user_imported' 或 'crossref' 的论文不受影响
    - 仅删除 lit_id 仍在 task.papers 中的论文(避免误删用户后加入池的同 lit_id 文献)
    """
    with SessionLocal() as db:
        task = db.get(RetrievalTaskModel, task_id)
        if not task:
            return {"task_deleted": False, "papers_deleted": 0,
                    "task_status": None, "papers_existed": 0}

        # 1. 收集该任务入库到文献池的论文 lit_id
        papers_existed = 0
        papers_deleted = 0
        if task.papers:
            lit_ids = [p.get("lit_id") for p in task.papers if p.get("lit_id")]
            # 仅删 source='openalex' 且仍在 papers 表内的
            rows = (
                db.query(PaperModel)
                .filter(PaperModel.lit_id.in_(lit_ids))
                .filter(PaperModel.source == "openalex")
                .all()
            )
            papers_existed = len(rows)
            for r in rows:
                db.delete(r)
            papers_deleted = papers_existed

        # 2. 删任务
        task_status = task.status
        db.delete(task)
        db.commit()

        return {
            "task_deleted": True,
            "papers_deleted": papers_deleted,
            "task_status": task_status,
            "papers_existed": papers_existed,
        }


def _update(task_id: str, **values) -> None:
    values["updated_at"] = _utcnow()
    with SessionLocal() as db:
        task = db.get(RetrievalTaskModel, task_id)
        if not task:
            return
        for key, value in values.items():
            setattr(task, key, value)
        db.commit()


def run_task(task_id: str) -> None:
    """执行单个检索任务,结果写回数据库。"""
    task = get_task(task_id)
    if not task or task.status in TERMINAL_STATUSES:
        return

    try:
        _update(task_id, status="running", progress=10)
        planned = plan_query(task.topic, default_year_start=task.year_start)
        keywords = planned.get("keywords_en") or [task.topic]
        query_used = " ".join(keywords)
        topic_summary = planned.get("topic_summary", task.topic)
        _update(
            task_id,
            progress=30,
            topic_summary=topic_summary,
            query_used=query_used,
        )

        raw = OpenAlexAdapter().search(
            query=query_used,
            year_range=(task.year_start, task.year_end),
            per_page=task.limit,
        )
        _update(task_id, progress=60, total_before_filter=len(raw))

        papers = by_year(raw, (task.year_start, task.year_end))
        papers = by_min_citations(papers, task.min_citations)
        papers = deduplicate(papers)
        _update(task_id, progress=75, total_after_filter=len(papers))

        if task.use_rerank and papers:
            papers = rerank(papers, topic_summary, top_n=min(task.limit, 50))
        paper_dicts = [dict(p.to_dict(), selected=True) for p in papers]

        # 检索完成自动批量入池(upsert,保留已有 selected 状态)
        _upsert_papers_to_pool(paper_dicts)

        _update(
            task_id,
            status="succeeded",
            progress=100,
            total_after_filter=len(paper_dicts),
            papers=paper_dicts,
            error=None,
        )
    except Exception as exc:
        _update(task_id, status="failed", progress=100, error=str(exc))


def _upsert_papers_to_pool(paper_dicts: list[dict]) -> None:
    """把检索结果批量写入文献池。

    已存在的 lit_id 只更新元数据,不覆盖用户的 selected 勾选状态。
    """
    if not paper_dicts:
        return
    with SessionLocal() as db:
        for d in paper_dicts:
            lit_id = d.get("lit_id")
            if not lit_id:
                continue
            existing = db.get(PaperModel, lit_id)
            # 只写可变元数据字段,created_at/selected 由库内原值或默认值决定
            meta = {
                k: v for k, v in d.items()
                if k not in ("lit_id", "created_at", "selected")
            }
            if existing:
                for k, v in meta.items():
                    setattr(existing, k, v)
            else:
                db.add(PaperModel(lit_id=lit_id, selected=d.get("selected", True), **meta))
        db.commit()
