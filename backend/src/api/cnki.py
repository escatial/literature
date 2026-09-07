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
from retrieval.history_aggregator import add_cnki as aggregator_add_cnki
from retrieval.query_planner import normalize_cnki_query
from retrieval.types import Paper, Source
try:  # 与面板 crawler_admin/e2e 同源：automation.* 优先，确保拿到与爬虫运行
      # 实例相同的模块单例（CONFIG/registry/断路器/动态池），避免双单例分裂
    from automation.cnki_adapter import run_cnki_full_auto
except ImportError:  # 仅 backend/src 不在 sys.path 的独立脚本兜底
    from src.automation.cnki_adapter import run_cnki_full_auto

log = logging.getLogger(__name__)


def _cnki_papers_from_pool(pool_task_id: str | None = None) -> list[Paper]:
    """本次知网任务结束后,从文献池读 source='cnki' 的全部记录,构造 Paper 列表。

    为什么从文献池读而不是 adapter 直接返回:
    cnki_adapter.run_cnki_full_auto 目前只对外暴露 saved 计数,不在 result 中附带
    Paper 列表;若强行改 adapter 签名会影响 v4.0 测试 / SSE 流式契约。文献池在
    _persist_record 阶段已落盘,所以从 DB 读是最低耦合的做法。

    v7.2:传入 pool_task_id(检索发起时的 X-Task-Id)时严格按任务过滤,
    否则会把 __legacy__ 迁移的历史 cnki 数据一起算进检索记录
    (如 267 旧数据 + 58 本次 = total 325 虚高)。
    """
    with _db_session.SessionLocal() as db:
        stmt = select(PaperModel).where(PaperModel.source == "cnki")
        if pool_task_id:
            stmt = stmt.where(PaperModel.task_id == pool_task_id)
        rows = list(db.execute(stmt).scalars().all())
        return [
            Paper(
                lit_id=row.lit_id,
                source=Source.CNKI,
                title=row.title or "",
                authors=list(row.authors or []),
                journal=row.journal or "",
                year=int(row.year or 0),
                volume=row.volume,
                issue=row.issue,
                pages=row.pages,
                abstract=row.abstract_text or row.abstract,
                doi=row.doi or None,
                source_url=row.source_url or "",
                cited_by_count=int(row.cited_by_count or 0),
                relevance_score=row.relevance_score,
                provenance=row.provenance,
                raw_citation=row.raw_citation,
            )
            for row in rows
        ]

router = APIRouter(tags=["cnki"])

# 任务级 SSE 队列:{task_id: asyncio.Queue}
_task_queues: dict[str, asyncio.Queue] = {}
_task_results: dict[str, dict] = {}
# 任务级取消标志:用户点「停止」后置位,爬虫循环尽快退出
_CANCEL_EVENTS: dict[str, threading.Event] = {}
# 性能修复(P-1):任务结果/队列的保留时长。客户端从未连 SSE 的任务也要有兜底回收,
# 否则 _task_results/_task_queues 随任务数无界增长(内存泄漏)
_RESULT_TTL_SECONDS = 3600.0


def _reap_task(task_id: str) -> None:
    """到期回收任务残留:结果字典与 SSE 队列(pop 幂等,不碰仍被消费的活跃流)。"""
    _task_results.pop(task_id, None)
    _task_queues.pop(task_id, None)


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
    expert_queries: list[str] = Field(..., min_length=4, max_length=8)
    target_count: int = Field(300, ge=1, le=500)
    max_pages: int = Field(10, ge=1, le=50)
    db_type: str = Field("cnki", pattern="^cnki$")
    # 一次「启动自动检索」由前端分配的 UUID;中文 + 英文两边共享,聚合写一条历史。
    run_id: str | None = None


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
    # v7.1:前端 axios 拦截器统一注入的文献池隔离 ID,
    # 透传到爬虫入库链路,否则文献池按 X-Task-Id 过滤会显示 0 条。
    x_task_id: str | None = Header(default=None, alias="X-Task-Id"),
):
    """启动知网全自动任务,后台跑、把状态推入 SSE 队列。

    X-Test-Sync header 仅供单元测试使用:为 "1" 时同步执行,避免 TestClient hang。
    """
    if not req.run_id:
        raise HTTPException(status_code=422, detail="三库统一检索必须携带 run_id，不能单独运行中国知网")
    # Q-1 修复①:启动前同步预检检索式,非法直接 422 把原因返回给前端,
    # 不再创建「返回 running 却秒败」的僵尸任务(旧问题:任务在 adapter 的
    # registry 注册之前被检索式校验拒绝,面板永不可见,用户点启动后毫无动静)
    _precheck = [q.strip() for q in req.expert_queries if q.strip()]
    if not _precheck and req.expert_query.strip():
        _precheck = [req.expert_query.strip()]  # 与 adapter 的回退语义一致
    for _q in _precheck:
        try:
            normalize_cnki_query(_q)
        except ValueError as exc:
            raise HTTPException(
                status_code=422, detail=f"知网检索式语法无效,任务未提交: {exc}"
            )
    task_id = uuid4().hex
    queue: asyncio.Queue = asyncio.Queue()
    _task_queues[task_id] = queue
    stop_ev = threading.Event()
    _CANCEL_EVENTS[task_id] = stop_ev

    async def _runner():
        # 文献池隔离 ID:入库打标签与历史读取共用同一个值
        pool_task_id = (x_task_id or "").strip() or None

        def _persist_history() -> None:
            """v9.6:落历史挪到后台 daemon 线程——全量同步 DB 读写此前直接在
            事件循环线程执行,落库瞬间阻塞所有并发请求(含写作 SSE 心跳)。
            注意不能用 await asyncio.to_thread:请求结束后短命事件循环
            (TestClient 每请求一个循环)会冻死挂起协程,任务终态永远落不了盘;
            daemon 线程既不让出事件循环,DB 也不再阻塞循环。"""
            if req.run_id:
                # 走 aggregator:有 run_id 就走聚合,没 run_id 兼容旧调用方直接写一条
                try:
                    aggregator_add_cnki(
                        run_id=req.run_id,
                        topic=req.topic,
                        papers=_cnki_papers_from_pool(pool_task_id),
                        # v8:历史记录必须记「池 task_id」(与 papers.task_id 同源),
                        # 不能记 SSE 内部 id(uuid4().hex),否则检索记录与池脱节
                        task_id=pool_task_id or task_id,
                    )
                except Exception as exc:
                    log.warning("aggregator(知网)写入失败: %s", exc)
            else:
                try:
                    from retrieval.history_service import record_history
                    record_history(
                        topic=req.topic,
                        sources=[req.db_type],
                        papers=_cnki_papers_from_pool(pool_task_id),
                        failed_sources={},
                        task_id=pool_task_id or task_id,
                    )
                except Exception as exc:
                    log.warning("写入知网检索历史失败: %s", exc)

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
                pool_task_id=pool_task_id,
            )
            if result.get("status") == "succeeded":
                threading.Thread(
                    target=_persist_history,
                    name=f"cnki-history-{task_id[:8]}",
                    daemon=True,
                ).start()
            _task_results[task_id] = result
            return result
        finally:
            # 任务结束(成功/失败/手动停止)后清理取消标志,避免内存泄漏
            _CANCEL_EVENTS.pop(task_id, None)
            # P-1:TTL 后兜底回收结果/队列(即使客户端从未连 SSE 也不会泄漏)
            asyncio.get_running_loop().call_later(
                _RESULT_TTL_SECONDS, _reap_task, task_id
            )

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
