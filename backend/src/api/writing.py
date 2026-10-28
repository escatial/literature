"""综述写作 API(支持一次性 + SSE 流式)。"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from src.retrieval.types import Paper, Source, normalize_source
from src.retrieval.provenance import derive_paper_provenance
from src.writing.classifier import Group
from src.writing.orchestrator import (
    collect_cited_ids,
    generate_review,
    generate_review_stream,
    plan_review_stream,
    render_reference_list,
)
from db.session import get_db

router = APIRouter(tags=["writing"])

# 写作任务协作式取消(C2 2026-10-27:注册表按 (user_id, task_id) 键隔离)。
# 与 B6 检索侧停止的不对称:检索任务在 retrieval_tasks 表有 DB 行,owner 以
# DB 为权威;写作流没有对应 DB 行(纯进程内 SSE 生命周期),无法落库归权,
# 故以注册表键隔离 —— 流注册自己的 (user_id, task_id),stop 只置位当前用户
# 名下的键(admin bypass)。两边语义对齐(用户 A 停不掉用户 B),实现各自适配。
_WRITING_STOP_EVENTS: dict[tuple[str, str], threading.Event] = {}
_WRITING_STOP_LOCK = threading.Lock()
# D1(2026-10-27):空闲心跳间隔抽为模块常量 —— 单测注入 0.05s 即可验证心跳
# 帧格式,不必真实等 2s;生产行为不变。
_SSE_HEARTBEAT_SECONDS = 2.0


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


def _sse_stream_response(stream_fn, bind=None, stop_key=None) -> StreamingResponse:
    """SSE 通用封装:后台线程跑同步生成器,主事件循环转发并保活心跳。

    C1:bind 为可调用 —— 在 produce() worker 线程内首行执行(返回 reset
    句柄),finally 恢复。worker 线程一次性创建销毁,ContextVar 按请求
    天然隔离;线程池复用场景下 finally reset 防泄漏是硬要求。
    C2:stop_key=(user_id, task_id) 注册停止键;None 时退化为匿名键
    (无用户上下文,如测试直挂)。
    """

    async def event_gen():
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        started_at = time.monotonic()
        stop_event = threading.Event()
        reg_key = stop_key if stop_key is not None else ("__anon__", f"__{id(stop_event):x}__")
        with _WRITING_STOP_LOCK:
            _WRITING_STOP_EVENTS[reg_key] = stop_event

        def publish(kind: str, payload: Any) -> None:
            # 停止后丢弃尚未入队的进度，避免前端在点击停止后又收到旧事件。
            if stop_event.is_set() and kind == "chunk":
                return
            loop.call_soon_threadsafe(queue.put_nowait, (kind, payload))

        def produce() -> None:
            llm_reset = None
            try:
                if bind is not None:
                    # worker 线程内绑定用户 LLM 上下文(ContextVar 按线程隔离)
                    llm_reset = bind()
                for chunk in stream_fn(stop_event, publish):
                    if stop_event.is_set():
                        break
                    publish("chunk", chunk)
            except BaseException as exc:
                publish("failure", exc)
            finally:
                if llm_reset is not None:
                    try:
                        llm_reset()
                    except Exception:  # noqa: BLE001  恢复失败不影响收尾
                        pass
                # C2:流结束只清除自己的键
                with _WRITING_STOP_LOCK:
                    _WRITING_STOP_EVENTS.pop(reg_key, None)
                publish("done", None)

        worker = threading.Thread(
            target=produce,
            name="writing-stream-worker",
            daemon=True,
        )
        worker.start()

        while True:
            try:
                kind, payload = await asyncio.wait_for(
                    queue.get(), timeout=_SSE_HEARTBEAT_SECONDS
                )
            except asyncio.TimeoutError:
                heartbeat = {
                    "event": "heartbeat",
                    "data": {"elapsed_seconds": int(time.monotonic() - started_at)},
                }
                # 注释行 + padding 促使开发代理立即 flush，避免长时间无正文时浏览器收不到心跳。
                yield f": heartbeat\n: {' ' * 1024}\n\ndata: {json.dumps(heartbeat, ensure_ascii=False)}\n\n"
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
        media_type="text/event-stream; charset=utf-8",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.post("/writing/corpus-preview")
def writing_corpus_preview(req: WritingRequest):
    """预览"文献拼接大文档"(主题轴规划的真实输入,先拼接、再划分)。"""
    from writing.classifier import _build_corpus_document

    papers = [p.to_paper() for p in req.papers]
    document = _build_corpus_document(papers)
    return {
        "papers": len(papers),
        "chars": len(document),
        "document": document,
    }


@router.post("/writing/stop")
def writing_stop(request: Request = None) -> dict:
    """请求停止当前用户名下的写作流;循环会在当前 LLM/网络调用返回后退出。

    C2:非 admin 只置位自己 (user_id, *) 的键;admin 全量。
    返回体 {"stopped": n} 格式不变(n = 置位的流数)。
    """
    user = getattr(request.state, "user", None) if request is not None else None
    uid = getattr(user, "user_id", None)
    is_admin = getattr(user, "role", "") == "admin"
    with _WRITING_STOP_LOCK:
        if uid is None or is_admin:
            events = list(_WRITING_STOP_EVENTS.values())
        else:
            events = [
                ev for (key_uid, _tid), ev in _WRITING_STOP_EVENTS.items()
                if key_uid == uid
            ]
    for event in events:
        event.set()
    return {"stopped": len(events)}


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
        source = normalize_source(self.source)
        return Paper(
            lit_id=self.lit_id,
            source=source,
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
            provenance=derive_paper_provenance(source.value, self.lit_id, self.source_url),
            raw_citation=self.raw_citation,
        )


class GroupOut(BaseModel):
    """主题分组(阶段1返回/阶段2确认后回传,均复用此结构)。"""

    name: str
    lit_ids: list[str]


class WritingRequest(BaseModel):
    topic: str
    # C6:papers 可缺省 —— confirmed_groups 模式且为空时,从 X-Task-Id 对应的
    # 规划快照按 lit_ids 引用回表取数(前端不再回传全量 papers)。
    papers: list[PaperIn] = Field(default_factory=list)
    classify_mode: str  # "locale" | "theme"
    do_screening: bool = True
    # 两阶段模式·阶段2:用户确认(可编辑)后的主题分组。
    # 非 None 时文献取数见 _resolve_stage2_papers(全量回传旧路径 / 引用取数新路径)。
    confirmed_groups: list[GroupOut] | None = None
    # 阶段1返回的《文献相关性分级清单》,阶段2回传供写作带分级提示;缺省可空。
    relevance_report: dict | None = None
    # v9.9: 研究问题(RQ1/RQ2/...)与综述目标,注入写作与述评,构成论证主线。
    research_questions: list[str] | None = None
    review_goal: str | None = None


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
def writing_generate(
    req: WritingRequest,
    request: Request,
    x_task_id: str | None = Header(None, alias="X-Task-Id"),
    db: Session = Depends(get_db),
) -> WritingResponse:
    _validate_writing_request(req)
    # C1:同步端点也走请求作用域绑定 —— try/finally 恢复,线程池复用防泄漏
    from security.auth import apply_user_llm

    llm_reset = apply_user_llm(getattr(request.state, "user", None))
    try:
        header_task = (x_task_id or "").strip()
        if req.confirmed_groups is not None:
            papers = _resolve_stage2_papers(req, header_task, request, db)
        else:
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
            research_questions=req.research_questions,
            review_goal=req.review_goal,
        )
    finally:
        llm_reset()
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


def _apply_requester_llm(request) -> None:
    """把发起请求用户的 LLM 配置应用为运行时覆盖(未配置则回落系统 env)。"""
    from security.auth import apply_user_llm

    apply_user_llm(getattr(request.state, "user", None))


def _resolve_stage2_papers(
    req: WritingRequest,
    header_task: str,
    request: Request,
    db: Session,
) -> list[Paper]:
    """C6 阶段2取数双路径。

    - 旧路径(兼容):papers 非空 → 原样 to_paper,打一行 deprecation log;
    - 新路径:papers 为空 → 按 X-Task-Id 取规划快照,screened_lit_ids 引用
      回表(papers 表唯一真源),relevance_report/RQ/goal 缺省回填快照值;
      快照缺失或取数 0 条 → 400「任务文献池为空或快照缺失,请重新规划」。
    """
    import logging

    if req.papers:
        logging.getLogger(__name__).info(
            "阶段2请求携带全量 papers(旧路径,待前端切换后废弃): %d 篇",
            len(req.papers),
        )
        return [p.to_paper() for p in req.papers]

    # 新路径:引用取数
    if not header_task:
        raise HTTPException(
            400,
            "阶段2按任务取数需要 X-Task-Id 请求头(缺任务上下文,无法定位规划快照)",
        )
    from writing.plan_store import get_plan, plan_to_writing_payload
    from retrieval.pool_reader import read_pool_papers_by_ids

    user = getattr(request.state, "user", None)
    plan = get_plan(header_task, user)
    if plan is None:
        raise HTTPException(400, "任务文献池为空或快照缺失，请重新规划")
    ref = plan_to_writing_payload(plan)
    papers = read_pool_papers_by_ids(db, header_task, ref["screened_lit_ids"], user)
    if not papers:
        raise HTTPException(400, "任务文献池为空或快照缺失，请重新规划")
    # 快照缺省回填(前端显式传值优先)
    if req.relevance_report is None:
        req.relevance_report = ref.get("relevance_report")
    if req.research_questions is None:
        req.research_questions = ref.get("research_questions")
    if req.review_goal is None:
        req.review_goal = ref.get("review_goal")
    logging.getLogger(__name__).info(
        "阶段2按规划快照取数: task=%s, %d/%d 篇命中",
        header_task, len(papers), len(ref["screened_lit_ids"]),
    )
    return papers


@router.post("/writing/generate-stream")
async def writing_generate_stream(
    req: WritingRequest,
    request: Request,
    x_task_id: str | None = Header(None, alias="X-Task-Id"),
    db: Session = Depends(get_db),
):
    """SSE 流式端点:后台线程执行同步生成,主事件循环持续发送进度心跳。

    两阶段模式:confirmed_groups 非 None 时为阶段2(主题确认后的正文写作)。
    C3:X-Task-Id 作为状态机 task_id(缺省每次生成新 UUID,任务互不串档)。
    C6:confirmed 模式 papers 为空时按规划快照引用取数(_resolve_stage2_papers)。
    """
    import uuid as _uuid

    _validate_writing_request(req)
    header_task = (x_task_id or "").strip()
    if req.confirmed_groups is not None:
        papers = _resolve_stage2_papers(req, header_task, request, db)
    else:
        papers = [p.to_paper() for p in req.papers]
    confirmed = (
        _to_domain_groups(req.confirmed_groups)
        if req.confirmed_groups is not None else None
    )
    task_id = header_task or str(_uuid.uuid4())

    def publish_writing_progress(stop_event, publish):
        # 生成器挂起在同步函数(润色/重写闭环)内部时,section_step 等事件
        # 经此带外发布,与 yield 事件走同一队列、同一线程,顺序不乱。
        def emit(event: str, data: dict) -> None:
            if stop_event.is_set():
                return
            publish(
                "chunk",
                f"data: {json.dumps({'event': event, 'data': data}, ensure_ascii=False)}\n\n",
            )
        return emit

    # C1:LLM 上下文绑定移入 worker 线程(首行 set/finally reset),替代
    # 请求线程的全局 set —— 并发流不再互相覆盖配置。
    # C2:停止键 (user_id, task_id) —— 用户 A 停不掉用户 B 的流。
    from security.auth import apply_user_llm

    _bound_user = getattr(request.state, "user", None)

    return _sse_stream_response(
        lambda stop_event, publish: generate_review_stream(
            topic=req.topic,
            papers=papers,
            classify_mode=req.classify_mode,
            do_screening=req.do_screening,
            confirmed_groups=confirmed,
            relevance_report=req.relevance_report,
            stop_event=stop_event,
            research_questions=req.research_questions,
            review_goal=req.review_goal,
            progress=publish_writing_progress(stop_event, publish),
            task_id=task_id,
        ),
        bind=lambda: apply_user_llm(_bound_user),
        stop_key=(getattr(_bound_user, "user_id", None) or "__anon__", task_id),
    )


class PlanRequest(BaseModel):
    """两阶段·阶段1 主题规划请求:只做筛选+分类,不接受确认字段。"""

    topic: str
    papers: list[PaperIn]
    classify_mode: str  # "locale" | "theme"
    # v9.9: 阶段1即采集研究问题,前端确认主题时即可回显;当前仅在
    # blueprint.research_questions 存档,阶段2写作时由请求再次传入。
    research_questions: list[str] | None = None
    review_goal: str | None = None


def _classify_emit(stop_event, publish):
    """构造分类阶段带外 progress 回调(单参 dict 协议)。

    R1 通读事件(additive)升格为独立事件名:readthrough_* kind 直接作为
    SSE 事件;既有 kind 一律走 classify_progress,事件名与字段零改动。
    """

    def emit(data):
        if stop_event.is_set():
            raise RuntimeError("用户已停止写作")
        event = "classify_progress"
        kind = str((data or {}).get("kind") or "")
        if kind.startswith("readthrough_"):
            event = kind
        publish(
            "chunk",
            f"data: {json.dumps({'event': event, 'data': data}, ensure_ascii=False)}\n\n",
        )

    return emit


@router.post("/writing/plan-stream")
async def writing_plan_stream(
    req: PlanRequest,
    request: Request,
    x_task_id: str | None = Header(None, alias="X-Task-Id"),
):
    """两阶段·阶段1 SSE:筛选 + 相关性分级 + 分类,停在主题确认点。

    前端在 plan_complete 事件拿到 groups(可编辑)、筛选后 papers(暂存)、
    section_titles、relevance_report;用户确认后调 /writing/generate-stream
    传 confirmed_groups 进入阶段2正文写作。
    C3:X-Task-Id 作为状态机 task_id,与阶段2共享同一任务档案。
    """
    import uuid as _uuid

    papers = [p.to_paper() for p in req.papers]
    task_id = (x_task_id or "").strip() or str(_uuid.uuid4())
    _user = getattr(request.state, "user", None)

    def publish_classify_progress(stop_event, publish):
        return _classify_emit(stop_event, publish)

    # C5:规划上下文由闭包捕获(worker 线程内只读),供快照 UPSERT。
    plan_ctx = {
        "task_id": task_id,
        "user_id": getattr(_user, "user_id", None),
        "topic": req.topic,
        "papers": papers,
        "classify_mode": req.classify_mode,
        "progress": publish_classify_progress,
        "research_questions": req.research_questions,
        "review_goal": req.review_goal,
    }

    def _plan_stream_with_capture(stop_event, publish):
        """C5:plan-stream 包装器 —— 逐 chunk 透传,捕获 plan_complete 事件
        UPSERT 规划快照(断点恢复)。纯消费端解析,不改事件字节序。"""
        inner = plan_review_stream(
            topic=plan_ctx["topic"],
            papers=plan_ctx["papers"],
            classify_mode=plan_ctx["classify_mode"],
            stop_event=stop_event,
            progress=plan_ctx["progress"](stop_event, publish),
            research_questions=plan_ctx["research_questions"],
            review_goal=plan_ctx["review_goal"],
            task_id=plan_ctx["task_id"],
        )
        for chunk in inner:
            yield chunk
            if '"plan_complete"' not in chunk:
                continue
            try:
                envelope = json.loads(chunk[len("data:"):].strip())
                data = envelope.get("data") or {}
                from writing.plan_store import upsert_plan

                upsert_plan(
                    task_id=plan_ctx["task_id"],
                    user_id=plan_ctx["user_id"],
                    payload={
                        "topic": plan_ctx["topic"],
                        "classify_mode": plan_ctx["classify_mode"],
                        "groups": data.get("groups") or [],
                        "section_titles": data.get("section_titles") or [],
                        # lit_ids 只存引用:papers 表是唯一真源(C6 按此回表取全字段)
                        "screened_lit_ids": [
                            str(p.get("lit_id") or "")
                            for p in (data.get("papers") or []) if p.get("lit_id")
                        ],
                        "relevance_report": data.get("relevance_report"),
                        "blueprint": data.get("blueprint"),
                        "research_questions": plan_ctx["research_questions"],
                        "review_goal": plan_ctx["review_goal"],
                        # R1 通读笔记摘要(可追溯):lit_id → 研究问题/主题线索
                        "readthrough_notes": data.get("readthrough_notes") or [],
                    },
                )
            except Exception:  # noqa: BLE001
                # 快照失败不阻断流(断点恢复是增量能力,主流程不受影响)
                import logging

                logging.getLogger(__name__).warning(
                    "plan 快照 UPSERT 失败(task_id=%s)", plan_ctx["task_id"], exc_info=True,
                )

    # C1:LLM 上下文绑定移入 worker 线程(首行 set/finally reset)
    # C2:停止键 (user_id, task_id)
    from security.auth import apply_user_llm

    return _sse_stream_response(
        _plan_stream_with_capture,
        bind=lambda: apply_user_llm(_user),
        stop_key=(getattr(_user, "user_id", None) or "__anon__", task_id),
    )


@router.get("/writing/plan")
def get_writing_plan(
    request: Request,
    x_task_id: str | None = Header(None, alias="X-Task-Id"),
):
    """C5 断点恢复:取规划快照。

    - 带 X-Task-Id:按任务取(非 admin 只能取自己名下,越权按 404);
    - 无头:返回该用户最近一条(admin 为全库最近一条);
    - 都没有:404「暂无已保存的规划」。
    """
    from writing.plan_store import get_plan, latest_plan, plan_to_writing_payload

    user = getattr(request.state, "user", None)
    row = (
        get_plan((x_task_id or "").strip(), user)
        if (x_task_id or "").strip()
        else latest_plan(user)
    )
    if row is None:
        raise HTTPException(404, "暂无已保存的规划,请先完成主题规划")
    out = plan_to_writing_payload(row)
    out["created_at"] = row.created_at.isoformat() if row.created_at else None
    out["updated_at"] = row.updated_at.isoformat() if row.updated_at else None
    return {"plan": out}
