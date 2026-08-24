"""综述写作 API(支持一次性 + SSE 流式)。"""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from src.retrieval.types import Paper, Source
from src.writing.orchestrator import (
    collect_cited_ids,
    generate_review,
    generate_review_stream,
    render_reference_list,
)

router = APIRouter(tags=["writing"])


class PaperIn(BaseModel):
    """前端 Paper 镜像。后端不持久化,只做一次性消费。"""

    lit_id: str
    source: str  # "openalex" | "crossref" | "user_imported"
    title: str
    authors: list[str] = Field(default_factory=list)
    journal: str | None = None
    year: int | None = None
    volume: str | None = None
    issue: str | None = None
    pages: str | None = None
    abstract: str | None = None
    doi: str | None = None
    source_url: str = ""
    cited_by_count: int = 0
    journal_level: str | None = None
    relevance_score: float | None = None
    raw_citation: str | None = None

    def to_paper(self) -> Paper:
        return Paper(
            lit_id=self.lit_id,
            source=Source(self.source),
            title=self.title,
            authors=self.authors,
            journal=self.journal or "",
            year=self.year or 0,
            volume=self.volume,
            issue=self.issue,
            pages=self.pages,
            abstract=self.abstract,
            doi=self.doi,
            source_url=self.source_url,
            cited_by_count=self.cited_by_count,
            journal_level=self.journal_level,
            relevance_score=self.relevance_score,
            raw_citation=self.raw_citation,
        )


class WritingRequest(BaseModel):
    topic: str
    papers: list[PaperIn]
    classify_mode: str  # "locale" | "theme"
    do_screening: bool = True


class SectionOut(BaseModel):
    key: str
    title: str
    content: str
    citations: list[str]


class GroupOut(BaseModel):
    name: str
    lit_ids: list[str]


class WritingResponse(BaseModel):
    topic: str
    classify_mode: str
    groups: list[GroupOut]
    sections: list[SectionOut]
    reference_list: str
    screened_out_ids: list[str]
    dropped_citations: list[str]


@router.post("/writing/generate", response_model=WritingResponse)
def writing_generate(req: WritingRequest) -> WritingResponse:
    if not req.do_screening:
        raise HTTPException(400, "写作必须先完成文献筛选,不允许跳过筛选阶段")
    papers = [p.to_paper() for p in req.papers]
    result = generate_review(
        topic=req.topic,
        papers=papers,
        classify_mode=req.classify_mode,
        do_screening=req.do_screening,
    )
    # 与 generate_review 内部编号顺序保持一致(保序去重,不用 set)
    cited_ids = collect_cited_ids(result.sections)
    by_id = {p.lit_id: p for p in papers}
    cited_papers = [by_id[cid] for cid in cited_ids if cid in by_id]
    ref = render_reference_list(cited_papers)
    return WritingResponse(
        topic=result.topic,
        classify_mode=result.classify_mode,
        groups=[GroupOut(name=g.name, lit_ids=g.lit_ids) for g in result.groups],
        sections=[
            SectionOut(key=s.key, title=s.title, content=s.content, citations=s.citations)
            for s in result.sections
        ],
        reference_list=ref,
        screened_out_ids=result.screened_out_ids,
        dropped_citations=result.dropped_citations,
    )


@router.post("/writing/generate-stream")
async def writing_generate_stream(req: WritingRequest, request: Request):
    """SSE 流式端点:逐章推进实时推送给前端。

    切换 tab 不可中断:不去主动调用 request.is_disconnected() 终止生成,
    浏览器在后台短暂 idle 会导致该检测误判为断开,造成任务中途夭折。
    改为由前端用 AbortController 主动停止。
    """
    if not req.do_screening:
        raise HTTPException(400, "写作必须先完成文献筛选,不允许跳过筛选阶段")
    papers = [p.to_paper() for p in req.papers]

    import asyncio as _asyncio

    async def event_gen():
        # 每隔 15s 推送一个 SSE 注释(": ping"),既保持连接不被中间代理掐断,
        # 也让浏览器 Network 面板能看到"sse-keepalive"标识确认仍在传输。
        iterator = iter(
            generate_review_stream(
                topic=req.topic,
                papers=papers,
                classify_mode=req.classify_mode,
                do_screening=req.do_screening,
            )
        )
        while True:
            # 同时等待下一个事件与心跳定时器,先到先 yield
            try:
                chunk = next(iterator)
            except StopIteration:
                break
            yield chunk
            await _asyncio.sleep(0)
            # 简易心跳:每 yield 一个事件后,额外让出极短时间,
            # 让 asyncio 有机会将已 yield 的 chunk 刷新到套接字,
            # 避免浏览器在切换 tab 期间积压导致读 done=true。
            await _asyncio.sleep(0)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # nginx 抗缓冲
            "Connection": "keep-alive",
        },
    )