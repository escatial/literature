"""文献池 CRUD API(需求5:服务端分页)。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from db.models import PaperModel
from db.schemas import (
    PaperBulkCreate,
    PaperBulkCreateResponse,
    PaperListResponse,
    PaperOut,
    PaperUpdate,
)
from db.session import get_db

router = APIRouter(prefix="/papers", tags=["papers"])


# 需求5:服务端分页白名单(防止 -1/99999 之类越界)
_ALLOWED_PAGE_SIZES = (10, 20, 50, 100)
_DEFAULT_PAGE_SIZE = 20
_MAX_PAGE = 10_000  # 防止过深的 page 拖垮 SQLite


@router.get("", response_model=PaperListResponse)
def list_papers(
    source: str | None = None,
    selected_only: bool = False,
    page: int = Query(1, ge=1, le=_MAX_PAGE),
    page_size: int = Query(_DEFAULT_PAGE_SIZE),
    db: Session = Depends(get_db),
):
    """服务端分页获取文献池。

    - page 从 1 起,page_size 必须在白名单 {10,20,50,100},默认 20。
    - 返回包含 items + total + page + page_size + total_pages,前端可直接渲染分页组件。
    """
    if page_size not in _ALLOWED_PAGE_SIZES:
        page_size = _DEFAULT_PAGE_SIZE
    base = select(PaperModel)
    count_base = select(func.count()).select_from(PaperModel)
    if source:
        # 前端传逗号分隔多值(如 user_imported,cnki / openalex,pubmed)
        sources = [s.strip() for s in source.split(",") if s.strip()]
        if sources:
            base = base.where(PaperModel.source.in_(sources))
            count_base = count_base.where(PaperModel.source.in_(sources))
    if selected_only:
        base = base.where(PaperModel.selected.is_(True))
        count_base = count_base.where(PaperModel.selected.is_(True))
    total = db.execute(count_base).scalar_one()
    items = db.execute(
        base.order_by(PaperModel.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).scalars().all()
    total_pages = (total + page_size - 1) // page_size if page_size else 1
    return PaperListResponse(
        items=list(items),
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages or 1,
    )


@router.get("/{lit_id}", response_model=PaperOut)
def get_paper(lit_id: str, db: Session = Depends(get_db)):
    p = db.get(PaperModel, lit_id)
    if not p:
        raise HTTPException(404, f"paper {lit_id} not found")
    return p


@router.post("/bulk", response_model=PaperBulkCreateResponse)
def bulk_upsert(req: PaperBulkCreate, db: Session = Depends(get_db)):
    """批量插入/更新(以 lit_id 为唯一键)。

    - 已有记录:更新可变字段,但保留 created_at(由首次插入时间决定)
    - 新记录:按 payload 插入,created_at 由数据库 default 填充
    """
    inserted = updated = skipped = 0
    # 排除 lit_id(主键)与 created_at(只读时间戳)
    _EXCLUDE = {"lit_id", "created_at"}
    for p in req.papers:
        existing = db.get(PaperModel, p.lit_id)
        payload = p.model_dump(exclude=_EXCLUDE)
        if existing:
            # 已存在:更新关键字段(不要覆盖 created_at)
            for k, v in payload.items():
                setattr(existing, k, v)
            updated += 1
        else:
            db.add(PaperModel(lit_id=p.lit_id, **payload))
            inserted += 1
    db.commit()
    return PaperBulkCreateResponse(inserted=inserted, updated=updated, skipped=skipped)


@router.patch("/{lit_id}", response_model=PaperOut)
def update_paper(lit_id: str, req: PaperUpdate, db: Session = Depends(get_db)):
    """更新单条(主要改 selected)。"""
    p = db.get(PaperModel, lit_id)
    if not p:
        raise HTTPException(404, f"paper {lit_id} not found")
    for k, v in req.model_dump(exclude_unset=True).items():
        setattr(p, k, v)
    db.commit()
    db.refresh(p)
    return p


@router.delete("/{lit_id}", status_code=204)
def delete_paper(lit_id: str, db: Session = Depends(get_db)):
    p = db.get(PaperModel, lit_id)
    if not p:
        raise HTTPException(404, f"paper {lit_id} not found")
    db.delete(p)
    db.commit()


@router.delete("", status_code=204)
def clear_papers(source: str | None = None, db: Session = Depends(get_db)):
    """清空文献池。传 source 时只清该来源(如 user_imported=中文, openalex=英文)。"""
    q = db.query(PaperModel)
    if source:
        q = q.filter(PaperModel.source == source)
    q.delete(synchronize_session=False)
    db.commit()
