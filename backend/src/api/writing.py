"""综述写作 API(支持一次性 + SSE 流式)。"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from src.retrieval.types import Paper, Source
from src.writing.classifier import Group
from src.writing.orchestrator import (
    collect_cited_ids,
    generate_review,
    generate_review_stream,
    plan_review_stream,
    render_reference_list,
)

router = APIRouter(tags=["writing"])


def _validate_writing_request(req: WritingRequest) -> None:
    """两阶段模式校验:阶段2带 confirmed_groups 且不可再筛选;单阶段必须筛选。"""
    if req.confirmed_groups is not None:
        if req.do_screening:
            raise HTTPException(
                400,
                "阶段2写作传入 confirmed_groups 时不可再筛选"
                "(文献已在主题规划阶段 /writing/plan-stream 完成筛选)",
            )
        if not req.confirmed_groups:
            raise HTTPException(400, "confirmed_groups 不能为空")
    elif not req.do_screening:
        raise HTTPException(400, "写作必须先完成文献筛选,不允许跳过筛选阶段")


def _to_domain_groups(groups: list[GroupOut]) -> list[Group]:
    """GroupOut → 领域模型 Group(确认后的主题分组传入 orchestrator)。"""
    return [Group(name=g.name, lit_ids=list(g.lit_ids)) for g in groups]


def _sse_stream_response(stream_fn) -> StreamingResponse:
    """SSE 通用封装:后台线程跑同步生成器,主事件循环转发并保活心跳。"""

    async def event_gen():
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        started_at = time.monotonic()

        def publish(kind: str, payload: Any) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, (kind, payload))

        def produce() -> None:
            try:
                for chunk in stream_fn():
                    publish("chunk", chunk)
            except BaseException as exc:
                publish("failure", exc)
            finally:
                publish("done", None)

        worker = threading.Thread(
            target=produce,
            name="writing-stream-worker",
            daemon=True,
        )
        worker.start()

        while True:
            try:
                kind, payload = await asyncio.wait_for(queue.get(), timeout=2.0)
            except asyncio.TimeoutError:
                heartbeat = {
                    "event": "heartbeat",
                    "data": {"elapsed_seconds": int(time.monotonic() - started_at)},
                }
                yield f"data: {json.dumps(heartbeat, ensure_ascii=False)}\n\n"
                continue

            if kind == "chunk":
                yield payload
            elif kind == "failure":
                error_event = {
                    "event": "error",
                    "data": {"message": str(payload)},
                }
                yield f"data: {json.dumps(error_event, ensure_ascii=False)}\n\n"
                break
            else:
                break

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


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
    abstract_text: str | None = None
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
            abstract=self.abstract or self.abstract_text,
            doi=self.doi,
            source_url=self.source_url,
            cited_by_count=self.cited_by_count,
            journal_level=self.journal_level,
            relevance_score=self.relevance_score,
            raw_citation=self.raw_citation,
        )


class GroupOut(BaseModel):
    """主题分组(阶段1返回/阶段2确认后回传,均复用此结构)。"""

    name: str
    lit_ids: list[str]


class WritingRequest(BaseModel):
    topic: str
    papers: list[PaperIn]
    classify_mode: str  # "locale" | "theme"
    do_screening: bool = True
    # 两阶段模式·阶段2:用户确认(可编辑)后的主题分组。
    # 非 None 时 papers 须为阶段1(/writing/plan-stream)筛选后回传的文献,
    # 且 do_screening 必须为 False(筛选已在阶段1完成,不可重入)。
    confirmed_groups: list[GroupOut] | None = None
    # 阶段1返回的《文献相关性分级清单》,阶段2回传供写作带分级提示;缺省可空。
    relevance_report: dict | None = None


class SectionOut(BaseModel):
    key: str
    title: str
    content: str
    citations: list[str]


class WritingResponse(BaseModel):
    topic: str
    classify_mode: str
    groups: list[GroupOut]
    sections: list[SectionOut]
    reference_list: str
    screened_out_ids: list[str]
    dropped_citations: list[str]
    qa_report: dict | None = None


@router.post("/writing/generate", response_model=WritingResponse)
def writing_generate(req: WritingRequest) -> WritingResponse:
    _validate_writing_request(req)
    papers = [p.to_paper() for p in req.papers]
    result = generate_review(
        topic=req.topic,
        papers=papers,
        classify_mode=req.classify_mode,
        do_screening=req.do_screening,
        confirmed_groups=(
            _to_domain_groups(req.confirmed_groups)
            if req.confirmed_groups is not None else None
        ),
        relevance_report=req.relevance_report,
    )
    # 若 orchestrator 已不再产出 reference_list 字段,这里回退重建。
    if not result.reference_list:
        cited_ids = collect_cited_ids(result.sections)
        by_id = {p.lit_id: p for p in papers}
        cited_papers = [by_id[cid] for cid in cited_ids if cid in by_id]
        result.reference_list = render_reference_list(cited_papers)
    return WritingResponse(
        topic=result.topic,
        classify_mode=result.classify_mode,
        groups=[GroupOut(name=g.name, lit_ids=g.lit_ids) for g in result.groups],
        sections=[
            SectionOut(key=s.key, title=s.title, content=s.content, citations=s.citations)
            for s in result.sections
        ],
        reference_list=result.reference_list,
        screened_out_ids=result.screened_out_ids,
        dropped_citations=result.dropped_citations,
        qa_report=result.qa_report,
    )


class QASectionIn(BaseModel):
    key: str
    title: str
    content: str
    citations: list[str] = Field(default_factory=list)


class QARequest(BaseModel):
    topic: str
    reference_list: str
    sections: list[QASectionIn]
    papers: list[PaperIn]
    grades: dict[str, str] | None = None
    ruleset_overrides: dict | None = None


class QAResponse(BaseModel):
    overall: str
    pass_rate: float
    required_pass_rate: float
    elapsed_ms: int
    summary: dict
    markdown_report: str
    json_report: str


@router.post("/writing/qa/run", response_model=QAResponse)
def writing_qa_run(req: QARequest) -> QAResponse:
    """独立端点:对已写入完成的章节与文献做一遍全流程核查。"""
    from qa.hooks import run_post_write_qa
    from qa.report import render_json_report, render_markdown_report
    from qa.runner import QARunner
    from qa.rules import QARuleSet, default_rule_set
    from writing.section_writer import SectionResult

    papers = [p.to_paper() for p in req.papers]
    sections = [
        SectionResult(
            key=s.key, title=s.title, content=s.content,
            citations=list(s.citations),
        )
        for s in req.sections
    ]
    ruleset = default_rule_set
    if req.ruleset_overrides:
        try:
            ruleset = QARuleSet(**{**ruleset.to_dict(), **req.ruleset_overrides})
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, f"ruleset_overrides 非法: {exc}")

    payload = run_post_write_qa(
        runner=QARunner(ruleset),
        papers=papers,
        sections=sections,
        reference_list=req.reference_list,
        grades=req.grades,
        on_fail=None,
    )
    summary = payload["summary"]
    return QAResponse(
        overall=summary["overall"],
        pass_rate=summary["pass_rate"],
        required_pass_rate=summary["required_pass_rate"],
        elapsed_ms=summary["elapsed_ms"],
        summary=summary,
        markdown_report=payload["markdown_report"],
        json_report=payload["json_report"],
    )


@router.post("/writing/generate-stream")
async def writing_generate_stream(req: WritingRequest):
    """SSE 流式端点:后台线程执行同步生成,主事件循环持续发送进度心跳。

    两阶段模式:confirmed_groups 非 None 时为阶段2(主题确认后的正文写作)。
    """
    _validate_writing_request(req)
    papers = [p.to_paper() for p in req.papers]
    confirmed = (
        _to_domain_groups(req.confirmed_groups)
        if req.confirmed_groups is not None else None
    )
    return _sse_stream_response(lambda: generate_review_stream(
        topic=req.topic,
        papers=papers,
        classify_mode=req.classify_mode,
        do_screening=req.do_screening,
        confirmed_groups=confirmed,
        relevance_report=req.relevance_report,
    ))


class PlanRequest(BaseModel):
    """两阶段·阶段1 主题规划请求:只做筛选+分类,不接受确认字段。"""

    topic: str
    papers: list[PaperIn]
    classify_mode: str  # "locale" | "theme"


@router.post("/writing/plan-stream")
async def writing_plan_stream(req: PlanRequest):
    """两阶段·阶段1 SSE:筛选 + 相关性分级 + 分类,停在主题确认点。

    前端在 plan_complete 事件拿到 groups(可编辑)、筛选后 papers(暂存)、
    section_titles、relevance_report;用户确认后调 /writing/generate-stream
    传 confirmed_groups 进入阶段2正文写作。
    """
    papers = [p.to_paper() for p in req.papers]
    return _sse_stream_response(lambda: plan_review_stream(
        topic=req.topic,
        papers=papers,
        classify_mode=req.classify_mode,
    ))
