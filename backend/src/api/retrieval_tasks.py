"""英文检索后台任务 API。"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from retrieval.task_service import create_task, delete_task, get_task, list_tasks

router = APIRouter(prefix="/retrieval/tasks", tags=["retrieval-tasks"])


class RetrievalTaskCreate(BaseModel):
    topic: str = Field(..., min_length=1)
    year_start: int = 2020
    year_end: int = 2026
    min_citations: int = 0
    limit: int = 50
    use_rerank: bool = True


class RetrievalTaskOut(BaseModel):
    task_id: str
    topic: str
    status: str
    progress: int
    year_start: int
    year_end: int
    min_citations: int
    limit: int
    use_rerank: bool
    topic_summary: str = ""
    query_used: str = ""
    total_before_filter: int = 0
    total_after_filter: int = 0
    papers: list[dict] = []
    error: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class RetrievalTaskCreated(BaseModel):
    task_id: str
    status: str


@router.post("", response_model=RetrievalTaskCreated)
def create_retrieval_task(req: RetrievalTaskCreate):
    task = create_task(
        topic=req.topic,
        year_start=req.year_start,
        year_end=req.year_end,
        min_citations=req.min_citations,
        limit=req.limit,
        use_rerank=req.use_rerank,
    )
    return RetrievalTaskCreated(task_id=task.task_id, status=task.status)


@router.get("", response_model=list[RetrievalTaskOut])
def list_retrieval_tasks(limit: int = 20):
    return list_tasks(limit=limit)


@router.get("/{task_id}", response_model=RetrievalTaskOut)
def get_retrieval_task(task_id: str):
    task = get_task(task_id)
    if not task:
        raise HTTPException(404, f"retrieval task {task_id} not found")
    return task


class RetrievalTaskDeleteOut(BaseModel):
    """删除任务的返回,前端用于提示用户清理了多少篇文献。"""
    task_deleted: bool
    papers_deleted: int = 0
    task_status: str | None = None
    papers_existed: int = 0


@router.delete("/{task_id}", response_model=RetrievalTaskDeleteOut)
def delete_retrieval_task(task_id: str):
    """删除单个检索任务;连带清除当年由该任务入库到文献池的英文文献。
    不会影响用户手动加入池的论文。
    """
    return delete_task(task_id)
