"""检索任务 v2 API:SearchIntent + Controller 流程。

POST /api/retrieval/v2/tasks     起新流程任务
GET  /api/retrieval/v2/tasks     列表
GET  /api/retrieval/v2/tasks/:id 详情
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from retrieval.task_service import (
    create_task_v2, get_task, list_tasks,
)

router = APIRouter(prefix="/retrieval/v2/tasks", tags=["retrieval-v2"])


class V2TaskCreate(BaseModel):
    topic: str = Field(..., min_length=1)
    sources: list[str] = Field(default_factory=lambda: ["pubmed", "openalex"])
    use_snowball: bool = False
    year_start: int | None = None
    year_end: int | None = None


class V2TaskCreated(BaseModel):
    task_id: str
    status: str


class V2TaskOut(BaseModel):
    task_id: str
    topic: str
    status: str
    progress: int
    year_start: int
    year_end: int
    topic_summary: str = ""
    query_used: str = ""
    total_before_filter: int = 0
    total_after_filter: int = 0
    papers: list[dict] = []
    error: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


@router.post("", response_model=V2TaskCreated)
def create_v2_task(req: V2TaskCreate):
    task = create_task_v2(
        topic=req.topic, sources=req.sources,
        use_snowball=req.use_snowball,
        year_start=req.year_start, year_end=req.year_end,
    )
    return V2TaskCreated(task_id=task.task_id, status=task.status)


@router.get("", response_model=list[V2TaskOut])
def list_v2_tasks(limit: int = Query(default=20, ge=1, le=100)):
    return list_tasks(limit=limit)


@router.get("/{task_id}", response_model=V2TaskOut)
def get_v2_task(task_id: str):
    task = get_task(task_id)
    if not task:
        raise HTTPException(404, f"task {task_id} not found")
    return task
