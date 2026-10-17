"""综述历史 CRUD API。"""
from __future__ import annotations

import json
import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import ReviewModel
from db.schemas import ReviewCreate, ReviewOut
from db.session import get_db

router = APIRouter(prefix="/reviews", tags=["reviews"])


@router.get("", response_model=list[ReviewOut])
def list_reviews(
    limit: int = Query(default=50, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """最近的综述列表。"""
    stmt = select(ReviewModel).order_by(ReviewModel.created_at.desc()).limit(limit)
    return db.execute(stmt).scalars().all()


@router.get("/{review_id}", response_model=ReviewOut)
def get_review(review_id: int, db: Session = Depends(get_db)):
    r = db.get(ReviewModel, review_id)
    if not r:
        raise HTTPException(404, f"review {review_id} not found")
    return r


@router.post("", response_model=ReviewOut, status_code=201)
def create_review(req: ReviewCreate, db: Session = Depends(get_db)):
    """保存一份综述(通常由 /writing/generate 触发后调用)。"""
    r = ReviewModel(
        topic=req.topic,
        classify_mode=req.classify_mode,
        sections=[s.model_dump() for s in req.sections],
        reference_list=req.reference_list,
        screened_out_ids=req.screened_out_ids,
        dropped_citations=req.dropped_citations,
    )
    db.add(r)
    db.commit()
    db.refresh(r)
    return r


@router.delete("/{review_id}", status_code=204)
def delete_review(review_id: int, db: Session = Depends(get_db)):
    r = db.get(ReviewModel, review_id)
    if not r:
        raise HTTPException(404, f"review {review_id} not found")
    db.delete(r)
    db.commit()


@router.get("/latest/export/docx")
def export_latest_review_docx(db: Session = Depends(get_db)):
    """导出最近一次综述为 Word(真实脚注 + 宋体小四/Times New Roman + 1.5 倍行距)。"""
    r = db.execute(
        select(ReviewModel).order_by(ReviewModel.created_at.desc())
    ).scalars().first()
    if not r:
        raise HTTPException(404, "还没有已生成的综述")
    return _export_review_docx(r)


@router.get("/{review_id}/export/docx")
def export_review_docx(review_id: int, db: Session = Depends(get_db)):
    """按 review id 导出 Word 文档。"""
    r = db.get(ReviewModel, review_id)
    if not r:
        raise HTTPException(404, f"review {review_id} not found")
    return _export_review_docx(r)


def _export_review_docx(r: ReviewModel) -> FileResponse:
    """把 reviews 表记录渲染为带原生脚注的 docx 并返回文件响应。

    - 正文里已编号的 [N] 锚点 → Word 原生脚注引用(脚注内容 = GB/T 7714 条目);
    - 排版:中文宋体、西文 Times New Roman、正文小四、1.5 倍行距。
    """
    # 延迟导入:渲染器依赖 python-docx,避免影响模块加载
    from delivery.docx_renderer import render_docx_with_footnotes

    sections = r.sections if isinstance(r.sections, list) else json.loads(r.sections or "[]")
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
    result = render_docx_with_footnotes(
        md_text=md_text,
        out_path=str(out_path),
        papers=[],
        title=str(r.topic or "文献综述"),
        refs_text=r.reference_list or "",
    )
    filename = f"文献综述-{safe_topic}-{r.id}.docx"
    return FileResponse(
        str(out_path),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=filename,
    )
