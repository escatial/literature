"""综述历史 CRUD API(B6 数据归属)。"""
from __future__ import annotations

import json
import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import ReviewModel
from db.schemas import ReviewCreate, ReviewOut
from db.session import get_db

router = APIRouter(prefix="/reviews", tags=["reviews"])


def _request_user(request: Request | None):
    """取鉴权中间件注入的当前用户(未登录上下文返回 None,如测试直挂)。"""
    return getattr(request.state, "user", None) if request is not None else None


def _apply_owner_scope(q, user):
    """B6 数据归属过滤,与 api/papers.apply_owner_scope 同语义(本地实现,
    避免 api router 模块间横向 import)。admin 直通;兼容 Query/Select。"""
    if user is None or getattr(user, "role", "") == "admin":
        return q
    cond = ReviewModel.owner_user_id == getattr(user, "user_id", None)
    if hasattr(q, "where"):
        return q.where(cond)
    return q.filter(cond)


@router.get("", response_model=list[ReviewOut])
def list_reviews(
    limit: int = Query(default=50, ge=1, le=100),
    request: Request = None,
    db: Session = Depends(get_db),
):
    """最近的综述列表。B6:非 admin 仅见本人名下。"""
    stmt = _apply_owner_scope(
        select(ReviewModel).order_by(ReviewModel.created_at.desc()).limit(limit),
        _request_user(request),
    )
    return db.execute(stmt).scalars().all()


def _get_scoped(db: Session, review_id: int, user) -> ReviewModel | None:
    """按归属过滤取单条(404 语义由调用方处理,防存在性泄漏)。"""
    return _apply_owner_scope(
        db.query(ReviewModel).filter(ReviewModel.id == review_id), user,
    ).first()


@router.get("/{review_id}", response_model=ReviewOut)
def get_review(review_id: int, request: Request = None, db: Session = Depends(get_db)):
    r = _get_scoped(db, review_id, _request_user(request))
    if not r:
        raise HTTPException(404, f"review {review_id} not found")
    return r


_INTERNAL_ANCHOR_RE = re.compile(r"\[(?:lit_[a-zA-Z0-9_]+|hash:[a-zA-Z0-9_]+)\]")


def _copy_quality(sections: list[dict]) -> tuple[int, int]:
    """副本质量 = (章节数, 内部锚点残留数)。章节数多且零残留者胜出。"""
    n = sum(1 for s in sections if str(s.get("content") or "").strip())
    leak = sum(len(_INTERNAL_ANCHOR_RE.findall(str(s.get("content") or ""))) for s in sections)
    return n, leak


@router.post("", response_model=ReviewOut, status_code=201)
def create_review(req: ReviewCreate, request: Request = None, db: Session = Depends(get_db)):
    """保存一份综述(B6:归属当前用户)。

    D1(2026-11 交付链加固)双门禁:
    - 服务端调和:同 (task_id, owner) 已有权威落库(流完成时后端直接写入
      的编号+QA+摘要定稿)时,残缺的前端副本(流中断中间态)不得覆盖它 ——
      章节数更多且零锚点泄漏的副本才胜出;
    - 入库校验:章节为空或正文残留内部锚点([lit_xxx])直接 422,残品绝不
      静默入库。
    """
    sections = [s.model_dump() for s in req.sections]
    uid = getattr(_request_user(request), "user_id", None)
    # X-Task-Id 请求头兜底(http 客户端对所有请求自动注入;前端无需镜像 task_id)
    header_task = (
        (request.headers.get("X-Task-Id") or "").strip() if request is not None else ""
    )
    effective_task = (req.task_id or header_task) or None

    server_row = None
    if effective_task:
        q = db.query(ReviewModel).filter_by(task_id=effective_task)
        if uid:
            q = q.filter(ReviewModel.owner_user_id == uid)
        server_row = q.order_by(ReviewModel.id.desc()).first()

    if server_row is not None:
        client_n, client_leak = _copy_quality(sections)
        server_n, server_leak = _copy_quality(
            server_row.sections if isinstance(server_row.sections, list) else []
        )
        if client_leak == 0 and client_leak <= server_leak and client_n >= server_n:
            # 前端副本完整且干净(如同步路径直存),以它更新服务端行
            server_row.sections = sections
            server_row.reference_list = req.reference_list
            server_row.screened_out_ids = req.screened_out_ids
            server_row.dropped_citations = req.dropped_citations
            server_row.qa_report = req.qa_report
            server_row.abstract = req.abstract
            server_row.keywords = req.keywords
            db.commit()
            db.refresh(server_row)
            return server_row
        # 服务端定稿胜出:丢弃前端残品,返回服务端行
        return server_row

    # 无服务端副本(旧流程/同步路径):入库校验
    if not sections or all(not str(s.get("content") or "").strip() for s in sections):
        raise HTTPException(422, "综述正文为空,拒绝保存(生成流可能未完成)")
    leak_total = sum(len(_INTERNAL_ANCHOR_RE.findall(str(s.get("content") or ""))) for s in sections)
    if leak_total > 0:
        raise HTTPException(
            422,
            f"正文残留 {leak_total} 处未编号内部引用([lit_xxx]),生成流未完成定稿,"
            "已拒绝保存。请重新生成或重试。",
        )

    r = ReviewModel(
        topic=req.topic,
        classify_mode=req.classify_mode,
        sections=sections,
        reference_list=req.reference_list,
        screened_out_ids=req.screened_out_ids,
        dropped_citations=req.dropped_citations,
        owner_user_id=uid,
        task_id=effective_task,
        qa_report=req.qa_report,
        abstract=req.abstract,
        keywords=req.keywords,
    )
    db.add(r)
    db.commit()
    db.refresh(r)
    return r


@router.delete("/{review_id}", status_code=204)
def delete_review(review_id: int, request: Request = None, db: Session = Depends(get_db)):
    r = _get_scoped(db, review_id, _request_user(request))
    if not r:
        raise HTTPException(404, f"review {review_id} not found")
    db.delete(r)
    db.commit()


@router.get("/latest/export/docx")
def export_latest_review_docx(request: Request = None, db: Session = Depends(get_db)):
    """导出最近一次综述为 Word(真实脚注 + 宋体小四/Times New Roman + 1.5 倍行距)。"""
    r = db.execute(
        _apply_owner_scope(
            select(ReviewModel).order_by(ReviewModel.created_at.desc()),
            _request_user(request),
        )
    ).scalars().first()
    if not r:
        raise HTTPException(404, "还没有已生成的综述")
    return _export_review_docx(r)


@router.get("/{review_id}/export/docx")
def export_review_docx(review_id: int, request: Request = None, db: Session = Depends(get_db)):
    """按 review id 导出 Word 文档。"""
    r = _get_scoped(db, review_id, _request_user(request))
    if not r:
        raise HTTPException(404, f"review {review_id} not found")
    return _export_review_docx(r)


def _export_review_docx(r: ReviewModel) -> FileResponse:
    """把 reviews 表记录渲染为带原生脚注的 docx 并返回文件响应。

    - 正文里已编号的 [N] 锚点 → Word 原生脚注引用(脚注内容 = GB/T 7714 条目);
    - 排版:中文宋体、西文 Times New Roman、正文小四、1.5 倍行距;
    - D1 导出门禁:交付门禁校验 + 残留锚点剥离(历史行兜底)+ 摘要/关键词
      + QA 附录,渲染后跑 check_render 结构校验并落日志。
    """
    import logging

    from delivery.render_checker import (
        check_render,
        check_sections_delivery,
        strip_internal_anchors,
    )
    # 延迟导入:渲染器依赖 python-docx,避免影响模块加载
    from delivery.docx_renderer import render_docx_with_footnotes

    log = logging.getLogger(__name__)
    sections = r.sections if isinstance(r.sections, list) else json.loads(r.sections or "[]")

    # D1 导出门禁:先校验(问题落日志),再剥离历史行可能残留的内部锚点
    delivery_issues = check_sections_delivery(sections, r.reference_list or "")
    if delivery_issues:
        log.warning(
            "review %s 导出门禁发现 %d 项问题(自动修复/继续导出): %s",
            r.id, len(delivery_issues),
            [i.get("code") for i in delivery_issues],
        )
    leak_total = 0
    for s in sections:
        cleaned, n = strip_internal_anchors(str(s.get("content") or ""))
        leak_total += n
        s["content"] = cleaned
    if leak_total:
        log.warning("review %s 导出前剥离未编号内部锚点 %d 处(历史数据兜底)", r.id, leak_total)

    parts: list[str] = []
    for section in sections:
        title = str(section.get("title") or "").strip()
        content = str(section.get("content") or "").strip()
        if not content:
            continue
        parts.append(f"## {title}\n{content}" if title else content)
    md_text = "\n\n".join(parts)
    if not md_text.strip():
        raise HTTPException(404, "该综述没有可导出的正文")

    out_dir = Path(__file__).resolve().parents[2] / "outputs"
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_topic = re.sub(r"[^一-鿿A-Za-z0-9]+", "-", r.topic)[:24].strip("-") or "review"
    out_path = out_dir / f"review_{r.id}_{safe_topic}.docx"
    qa = r.qa_report if isinstance(r.qa_report, dict) else None
    result = render_docx_with_footnotes(
        md_text=md_text,
        out_path=str(out_path),
        papers=[],
        title=str(r.topic or "文献综述"),
        refs_text=r.reference_list or "",
        abstract=(r.abstract or "").strip() or None,
        keywords=list(r.keywords or []) or None,
        qa_summary=qa,
    )
    # D1:渲染结构校验激活(此前 render_checker 是死代码)
    render_result = check_render(str(out_path))
    if not render_result.passed:
        log.warning(
            "review %s 渲染结构校验未通过: %s",
            r.id, [i.get("code") for i in render_result.issues],
        )
    filename = f"文献综述-{safe_topic}-{r.id}.docx"
    return FileResponse(
        str(out_path),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=filename,
    )
