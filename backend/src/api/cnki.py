"""v4.1 知网 API 路由:start / stream(SSE) / config GET+POST / cookie 健康检查。

完整流程:主题词 → 嵌入的 HTTP 爬虫(automation/cnki,超级鹰自动识别滑块/英数验证码)→
列表抓取 → 逐条摘要 → 入库,状态以 SSE 推给前端。
不再依赖 Playwright/远程浏览器。
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import AsyncIterator
from uuid import uuid4

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

import db.session as _db_session
from db.models import PaperModel
from retrieval.history_service import record_history
from retrieval.types import Paper, Source
from src.automation.cnki_adapter import run_cnki_full_auto

log = logging.getLogger(__name__)


def _cnki_papers_from_pool() -> list[Paper]:
    """本次知网任务结束后,从文献池读 source='cnki' 的全部记录,构造 Paper 列表。

    为什么从文献池读而不是 adapter 直接返回:
    cnki_adapter.run_cnki_full_auto 目前只对外暴露 saved 计数,不在 result 中附带
    Paper 列表;若强行改 adapter 签名会影响 v4.0 测试 / SSE 流式契约。文献池在
    _persist_record 阶段已落盘,所以从 DB 读是最低耦合的做法。
    """
    with _db_session.SessionLocal() as db:
        rows = list(
            db.execute(
                select(PaperModel).where(PaperModel.source == "cnki")
            ).scalars().all()
        )
        return [
            Paper(
                lit_id=row.lit_id,
                source=Source.CNKI,
                title=row.title or "",
                authors=list(row.authors or []),
                journal=row.journal or "",
                year=int(row.year or 0),
                doi=row.doi or None,
            )
            for row in rows
        ]

router = APIRouter(tags=["cnki"])

# 任务级 SSE 队列:{task_id: asyncio.Queue}
_task_queues: dict[str, asyncio.Queue] = {}
_task_results: dict[str, dict] = {}
# 任务级取消标志:用户点「停止」后置位,爬虫循环尽快退出
_CANCEL_EVENTS: dict[str, threading.Event] = {}


def stop_cnki_tasks(task_ids: list[str] | None = None) -> list[str]:
    """置位知网任务取消标志。task_ids 为空时停止所有运行中的知网任务。"""
    stopped: list[str] = []
    if task_ids is None:
        for tid, ev in list(_CANCEL_EVENTS.items()):
            ev.set()
            stopped.append(tid)
    else:
        for tid in task_ids:
            ev = _CANCEL_EVENTS.get(tid)
            if ev is not None:
                ev.set()
                stopped.append(tid)
    return stopped


class CnkiStartRequest(BaseModel):
    topic: str = Field(..., min_length=1)
    expert_query: str = Field(..., min_length=1)
    expert_queries: list[str] = Field(..., min_length=1, max_length=5)
    target_count: int = Field(300, ge=1, le=500)
    max_pages: int = Field(10, ge=1, le=50)
    db_type: str = Field("cnki", pattern="^cnki$")


class CnkiStartResponse(BaseModel):
    task_id: str
    status: str
    db_type: str


def _schedule(coro) -> object:
    """把**已构造的协程**排入后台任务。"""
    return asyncio.create_task(coro)


@router.post("/cnki/start", response_model=CnkiStartResponse)
async def start_cnki(
    req: CnkiStartRequest,
    x_test_sync: str | None = Header(default=None, alias="X-Test-Sync"),
):
    """启动知网全自动任务,后台跑、把状态推入 SSE 队列。

    X-Test-Sync header 仅供单元测试使用:为 "1" 时同步执行,避免 TestClient hang。
    """
    task_id = uuid4().hex
    queue: asyncio.Queue = asyncio.Queue()
    _task_queues[task_id] = queue
    stop_ev = threading.Event()
    _CANCEL_EVENTS[task_id] = stop_ev

    async def _runner():
        try:
            result = await run_cnki_full_auto(
                topic=req.topic,
                expert_query=req.expert_query,
                expert_queries=req.expert_queries,
                target_count=req.target_count,
                queue=queue,
                max_pages=req.max_pages,
                db_type=req.db_type,
                stop_event=stop_ev,
            )
            if result.get("status") == "succeeded":
                try:
                    record_history(
                        topic=req.topic,
                        sources=[req.db_type],
                        papers=_cnki_papers_from_pool(),
                        failed_sources={},
                        task_id=task_id,
                    )
                except Exception as exc:
                    log.warning("写入知网检索历史失败: %s", exc)
            _task_results[task_id] = result
            return result
        finally:
            # 任务结束(成功/失败/手动停止)后清理取消标志,避免内存泄漏
            _CANCEL_EVENTS.pop(task_id, None)

    if x_test_sync == "1":
        await _runner()
    else:
        _schedule(_runner())
    return CnkiStartResponse(task_id=task_id, status="running", db_type=req.db_type)


@router.get("/cnki/stream/{task_id}")
async def stream_cnki(task_id: str):
    """SSE 推送:每个消息都是 event: cnki_progress + data: {json}。"""
    queue = _task_queues.get(task_id)
    if queue is None:
        raise HTTPException(404, f"task {task_id} not found")

    async def gen() -> AsyncIterator[str]:
        try:
            while True:
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=10)
                except asyncio.TimeoutError:
                    yield "event: keepalive\ndata: {}\n\n"
                    if _task_results.get(task_id):
                        break
                    continue
                yield f"event: cnki_progress\ndata: {json.dumps(msg, ensure_ascii=False)}\n\n"
                if msg.get("stage") in {"done", "error"}:
                    break
        finally:
            _task_queues.pop(task_id, None)

    return StreamingResponse(gen(), media_type="text/event-stream")
