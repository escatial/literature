"""英文检索后台任务 API。"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from retrieval.task_service import create_task_v2, delete_task, get_task, list_tasks

router = APIRouter(prefix="/retrieval/tasks", tags=["retrieval-tasks"])


class RetrievalTaskCreate(BaseModel):
    topic: str = Field(..., min_length=1)
    year_start: int | None = None
    year_end: int | None = None
    min_citations: int = 0
    limit: int = 50
    use_rerank: bool = True
    # 雪球扩展(引文回溯)独立开关;默认关闭,避免主循环之外的引文文献混入池
    use_snowball: bool = False
    sources: list[str] = Field(default_factory=lambda: ["pubmed", "openalex"])


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
    # v4.1:英文检索过程日志(供前端按 db 拆开 OpenAlex / PubMed 面板)
    events: list[dict] = []
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class RetrievalTaskCreated(BaseModel):
    task_id: str
    status: str


@router.post("", response_model=RetrievalTaskCreated)
def create_retrieval_task(req: RetrievalTaskCreate):
    # v4.1:统一改走 v2 路径(SearchIntent + RetrievalController),
    # 让前端能拿到按 db 拆分的翻页事件(对称中文知网日志面板)。
    # 旧 create_task 路径不调 Controller,无 events,前端面板会一直空。
    task = create_task_v2(
        topic=req.topic,
        sources=req.sources,
        use_snowball=req.use_snowball,
        year_start=req.year_start,
        year_end=req.year_end,
    )
    return RetrievalTaskCreated(task_id=task.task_id, status=task.status)


@router.get("", response_model=list[RetrievalTaskOut])
def list_retrieval_tasks(limit: int = Query(default=20, ge=1, le=100)):
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
