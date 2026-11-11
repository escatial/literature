"""综述写作 API(支持一次性 + SSE 流式)。"""
from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from pathlib import Path
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
                "(文献已在主题规划阶段 /writing/jobs 完成筛选)",
            )
        if not req.confirmed_groups:
            raise HTTPException(400, "confirmed_groups 不能为空")
    elif not req.do_screening:
        raise HTTPException(400, "写作必须先完成文献筛选,不允许跳过筛选阶段")


def _to_domain_groups(groups: list[GroupOut]) -> list[Group]:
    """GroupOut → 领域模型 Group(确认后的主题分组传入 orchestrator)。"""
    return [Group(name=g.name, lit_ids=list(g.lit_ids)) for g in groups]


def publish_writing_progress(stop_event, publish):
    """J1:带外进度发布工厂(模块级,SSE 端点与任务 worker 共用)。"""
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


def _parse_sse_chunk(chunk: Any) -> tuple[str, dict]:
    """J1:解析 SSE data 帧为 (event, data),供任务事件落库。坏帧返回 ("unknown", {})。"""
    try:
        if isinstance(chunk, str) and chunk.startswith("data: "):
            obj = json.loads(chunk[len("data:"):].strip())
            data = obj.get("data")
            return (
                str(obj.get("event") or "unknown"),
                data if isinstance(data, dict) else {},
            )
    except Exception:  # noqa: BLE001
        pass
    return "unknown", {}


def persist_review_payload(uid: str | None, payload: dict) -> None:
    """J1:服务端权威落库的模块级实现(worker 线程调用;独立短会话)。

    编号+QA修复+摘要齐备的定稿由后端直接持久化;同 (task_id, owner) 幂等
    upsert。前端随后的 POST /reviews 是同一键的冗余副本,残品会被本副本调和。
    """
    from db.models import ReviewModel
    from db.session import SessionLocal

    with SessionLocal() as pdb:
        row = None
        if payload.get("task_id"):
            q = pdb.query(ReviewModel).filter_by(task_id=payload["task_id"])
            if uid:
                q = q.filter(ReviewModel.owner_user_id == uid)
            row = q.order_by(ReviewModel.id.desc()).first()
        if row is None:
            row = ReviewModel(
                topic=payload["topic"],
                classify_mode=payload["classify_mode"],
                owner_user_id=uid,
            )
            pdb.add(row)
        row.task_id = payload.get("task_id")
        row.sections = payload["sections"]
        row.reference_list = payload["reference_list"]
        row.screened_out_ids = payload["screened_out_ids"]
        row.dropped_citations = payload["dropped_citations"]
        row.qa_report = payload.get("qa_report")
        row.abstract = payload.get("abstract")
        row.keywords = payload.get("keywords")
        pdb.commit()


def make_plan_stream_fn(
    *,
    task_id: str,
    user_id: str | None,
    topic: str,
    papers: list,
    classify_mode: str,
    progress_factory,
    research_questions: list[str] | None = None,
    review_goal: str | None = None,
):
    """J1:阶段1 plan 流的模块级工厂(捕获 plan_complete UPSERT 规划快照)。

    与 writing_plan_stream 端点内原先的闭包同逻辑,抽为模块级供
    /writing/jobs(阶段1)与旧 SSE 端点共用。
    """
    plan_ctx = {
        "task_id": task_id,
        "user_id": user_id,
        "topic": topic,
        "papers": papers,
        "classify_mode": classify_mode,
        "progress_factory": progress_factory,
        "research_questions": research_questions,
        "review_goal": review_goal,
    }

    def _stream(stop_event, publish):
        inner = plan_review_stream(
            topic=plan_ctx["topic"],
            papers=plan_ctx["papers"],
            classify_mode=plan_ctx["classify_mode"],
            stop_event=stop_event,
            progress=plan_ctx["progress_factory"](stop_event, publish),
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
                logging.getLogger(__name__).debug("规划快照 UPSERT 失败(忽略)")

    return _stream


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
# ============================================================================
# J1(2026-11) 任务化改造:写作任务 + 可重放事件流
# ----------------------------------------------------------------------------
# 「一次 SSE 连接扛 60 分钟」的脆弱性根治:任务在服务端独立生存,事件逐条
# 落库(writing_job_events),连接只是读取视图 —— 断线重连按 since_seq 续传,
# 刷新页面凭 job_id 恢复全部进度,服务重启后任务状态可查可停。
# ============================================================================

class ConfirmGroupsBody(BaseModel):
    """阶段2确认请求体:用户确认(可编辑)后的主题分组。"""
    confirmed_groups: list[GroupOut]


def _run_writing_job_worker(
    job_id: str,
    user_id: str | None,
    stream_fn,
    bind=None,
    start_seq: int = 0,
) -> None:
    """J1:无头任务 worker —— 后台线程跑生成器,事件逐条落库。

    与 SSE 投递层同一套协作式取消(注册表)(stop_event 注册表)与 LLM
    ContextVar 绑定,但不面向连接:事件是事实来源,前端随时凭 seq 接入。
    """
    import logging

    from writing import job_store

    log = logging.getLogger(__name__)
    seq = int(start_seq)
    stop_event = threading.Event()
    reg_key = (user_id or "__anon__", job_id)
    with _WRITING_STOP_LOCK:
        _WRITING_STOP_EVENTS[reg_key] = stop_event
    job_store.set_job_status(job_id, "running")
    # J1:筛选 tap 在 4 个并行批线程经 publish 发事件,与生成器主循环
    # 并发分配 seq —— 必须互斥,否则 (job_id, seq) 主键撞车丢事件。
    seq_lock = threading.Lock()
    llm_reset = None
    terminal: tuple[str, str | None] | None = None

    def mark_terminal(name: str, data: dict) -> None:
        nonlocal terminal
        if name == "plan_complete":
            job_store.set_job_status(job_id, "awaiting_confirm", phase="await_confirm")
        elif name == "complete":
            terminal = ("done", None)
        elif name == "stopped":
            terminal = ("stopped", None)
        elif name == "error":
            terminal = ("failed", str((data or {}).get("message") or "生成失败"))

    def publish(kind: str, payload: Any) -> None:
        # 带外进度:筛选 tap 在并行批线程调用,seq 分配经 seq_lock 互斥
        nonlocal seq
        if stop_event.is_set() or kind != "chunk":
            return
        with seq_lock:
            seq += 1
            name, data = _parse_sse_chunk(payload)
            job_store.append_event(job_id, seq, name, data)
        mark_terminal(name, data)

    try:
        if bind is not None:
            llm_reset = bind()
        for chunk in stream_fn(stop_event, publish):
            if stop_event.is_set():
                break
            with seq_lock:
                seq += 1
                name, data = _parse_sse_chunk(chunk)
                job_store.append_event(job_id, seq, name, data)
            mark_terminal(name, data)
        if stop_event.is_set():
            job_store.set_job_status(job_id, "stopped")
        elif terminal is not None:
            job_store.set_job_status(job_id, terminal[0], error=terminal[1])
        else:
            # 阶段1 正常出口:生成器在 plan_complete 后结束,状态已是
            # awaiting_confirm —— 兜底只对仍卡在 running 的任务生效,
            # 不覆盖已迁移的状态(J1 测试实证的覆盖 bug)。
            cur = job_store.get_job(job_id)
            if cur is not None and cur["status"] == "running":
                job_store.set_job_status(job_id, "failed", error="生成流提前终止(未到达终态事件)")
    except BaseException as exc:  # noqa: BLE001  worker 兜底:失败必须落库可查
        log.warning("写作任务 %s 异常终止: %s", job_id, exc)
        try:
            job_store.append_event(job_id, seq + 1, "error", {"message": str(exc)})
            job_store.set_job_status(job_id, "failed", error=str(exc)[:500])
        except Exception:  # noqa: BLE001
            pass
    finally:
        if llm_reset is not None:
            try:
                llm_reset()
            except Exception:  # noqa: BLE001
                pass
        with _WRITING_STOP_LOCK:
            _WRITING_STOP_EVENTS.pop(reg_key, None)


@router.post("/writing/jobs", status_code=202)
async def create_writing_job(
    req: WritingRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """J1:启动写作任务,立即返回 job_id(任务在服务端独立运行)。

    - 请求体 = WritingRequest;confirmed_groups 非 None 即为阶段2(刷新恢复
      场景直接续跑写作,需配合 X-Task-Id = job_id 的规划快照);
    - 事件逐条落库,GET /writing/jobs/{id}/stream 随时接入。
    """
    import uuid as _uuid

    from security.auth import apply_user_llm
    from writing import job_store

    _validate_writing_request(req)
    _user = getattr(request.state, "user", None)
    uid = getattr(_user, "user_id", None)
    job_id = str(_uuid.uuid4())

    if req.confirmed_groups is not None:
        papers = _resolve_stage2_papers(req, job_id, request, db)
        confirmed = _to_domain_groups(req.confirmed_groups)
    else:
        papers = [p.to_paper() for p in req.papers]
        confirmed = None

    snapshot = req.model_dump(exclude={"papers"})  # papers 可能上千篇,快照只留引用语境
    job_store.create_job(job_id, uid, req.topic, req.classify_mode, snapshot)

    if confirmed is not None:
        start_seq = job_store.max_seq(job_id)

        def persist_final(payload: dict) -> None:
            persist_review_payload(uid, payload)

        def stream_fn(stop_event, publish):
            return generate_review_stream(
                topic=req.topic,
                papers=papers,
                classify_mode=req.classify_mode,
                do_screening=False,
                confirmed_groups=confirmed,
                relevance_report=req.relevance_report,
                stop_event=stop_event,
                research_questions=req.research_questions,
                review_goal=req.review_goal,
                progress=publish_writing_progress(stop_event, publish),
                task_id=job_id,
                persist=persist_final,
            )
    else:
        start_seq = 0

        def stream_fn(stop_event, publish):
            return make_plan_stream_fn(
                task_id=job_id,
                user_id=uid,
                topic=req.topic,
                papers=papers,
                classify_mode=req.classify_mode,
                progress_factory=_classify_emit,
                research_questions=req.research_questions,
                review_goal=req.review_goal,
            )(stop_event, publish)

    threading.Thread(
        target=_run_writing_job_worker,
        kwargs={
            "job_id": job_id, "user_id": uid, "stream_fn": stream_fn,
            "bind": lambda: apply_user_llm(_user), "start_seq": start_seq,
        },
        name=f"writing-job-{job_id[:12]}",
        daemon=True,
    ).start()
    return {"job_id": job_id, "status": "running"}


@router.post("/writing/jobs/{job_id}/confirm", status_code=202)
async def confirm_writing_job(
    job_id: str,
    body: ConfirmGroupsBody,
    request: Request,
    db: Session = Depends(get_db),
):
    """J1:阶段2确认 —— 同一 job 内从确认点继续写正文(事件 seq 连续)。"""
    from security.auth import apply_user_llm
    from writing import job_store

    _user = getattr(request.state, "user", None)
    uid = getattr(_user, "user_id", None)
    job = job_store.get_job_scoped(job_id, _user)
    if job is None:
        raise HTTPException(404, f"writing job {job_id} not found")
    if job["status"] != "awaiting_confirm":
        raise HTTPException(
            409,
            f"任务当前状态为 {job['status']},只有 awaiting_confirm 可确认",
        )
    if not body.confirmed_groups:
        raise HTTPException(400, "confirmed_groups 不能为空")

    req = WritingRequest(
        topic=job["topic"],
        classify_mode=job["classify_mode"],
        do_screening=False,
        papers=[],
        confirmed_groups=body.confirmed_groups,
        relevance_report=(job.get("req_snapshot") or {}).get("relevance_report"),
        research_questions=(job.get("req_snapshot") or {}).get("research_questions"),
        review_goal=(job.get("req_snapshot") or {}).get("review_goal"),
    )
    papers = _resolve_stage2_papers(req, job_id, request, db)
    confirmed = _to_domain_groups(body.confirmed_groups)
    start_seq = job_store.max_seq(job_id)
    job_store.set_job_status(job_id, "running", phase="writing", error=None)

    def persist_final(payload: dict) -> None:
        persist_review_payload(uid, payload)

    def stream_fn(stop_event, publish):
        return generate_review_stream(
            topic=req.topic,
            papers=papers,
            classify_mode=req.classify_mode,
            do_screening=False,
            confirmed_groups=confirmed,
            relevance_report=req.relevance_report,
            stop_event=stop_event,
            research_questions=req.research_questions,
            review_goal=req.review_goal,
            progress=publish_writing_progress(stop_event, publish),
            task_id=job_id,
            persist=persist_final,
        )

    threading.Thread(
        target=_run_writing_job_worker,
        kwargs={
            "job_id": job_id, "user_id": uid, "stream_fn": stream_fn,
            "bind": lambda: apply_user_llm(_user), "start_seq": start_seq,
        },
        name=f"writing-job-{job_id[:12]}",
        daemon=True,
    ).start()
    return {"job_id": job_id, "status": "running"}


@router.get("/writing/jobs/{job_id}")
def get_writing_job(job_id: str, request: Request):
    """J1:任务快照(status/phase/error);前端刷新恢复与重连前预检用。"""
    from writing import job_store

    _user = getattr(request.state, "user", None)
    job = job_store.get_job_scoped(job_id, _user)
    if job is None:
        raise HTTPException(404, f"writing job {job_id} not found")
    return job


@router.get("/writing/jobs/{job_id}/stream")
async def stream_writing_job(
    job_id: str,
    request: Request,
    since_seq: int = 0,
):
    """J1:可续传事件流 —— 先重放 since_seq 之后的事件,再实时 tail。

    语义:
    - 每帧带单调 seq(与 D1 协议一致),前端凭 since_seq 续传;
    - 任务到达终态(done/failed/stopped)后流结束;awaiting_confirm 保持
      连接(确认后事件在同一连接上继续到达);
    - 15s 无事件发注释心跳,防中间代理掐空闲连接。
    """
    from writing import job_store

    _user = getattr(request.state, "user", None)
    job = job_store.get_job_scoped(job_id, _user)
    if job is None:
        raise HTTPException(404, f"writing job {job_id} not found")

    async def gen():
        cursor = int(since_seq)
        loop = asyncio.get_event_loop()
        last_yield = loop.time()
        while True:
            rows = job_store.events_after(job_id, cursor, limit=200)
            for r in rows:
                envelope = {"event": r["event"], "data": r["payload"] or {}, "seq": r["seq"]}
                yield f"data: {json.dumps(envelope, ensure_ascii=False)}\n\n"
                cursor = r["seq"]
                last_yield = loop.time()
            status = job_store.get_job(job_id)["status"]
            # 先追平事件日志,再判终态(2026-11-11 恢复卡死 23 分钟的根因:
            # awaiting_confirm 休眠任务的事件数超过单批 limit=200 时,首批
            # 发完就 return,重放永远截断在半路,前端拿不到主题方案事件)
            if not rows and status in ("done", "failed", "stopped", "awaiting_confirm"):
                return
            now = loop.time()
            if not rows and now - last_yield > 15.0:
                yield ": heartbeat\n\n"
                last_yield = now
            await asyncio.sleep(0.5)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream; charset=utf-8",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.post("/writing/jobs/{job_id}/stop")
def stop_writing_job(job_id: str, request: Request):
    """J1:停止任务 —— 进程内 Event 即时置位 + 落库(重启后仍可停/可查)。"""
    from writing import job_store

    _user = getattr(request.state, "user", None)
    job = job_store.get_job_scoped(job_id, _user)
    if job is None:
        raise HTTPException(404, f"writing job {job_id} not found")
    owner_key = job["user_id"] or "__anon__"
    with _WRITING_STOP_LOCK:
        ev = _WRITING_STOP_EVENTS.get((owner_key, job_id))
    if ev is not None:
        ev.set()
    job_store.stop_job(job_id)
    return {"job_id": job_id, "status": "stopped"}
