"""文献池 CRUD API。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import PaperModel
from db.schemas import (
    PaperBulkCreate,
    PaperBulkCreateResponse,
    PaperOut,
    PaperUpdate,
)
from db.session import get_db

router = APIRouter(prefix="/papers", tags=["papers"])


@router.get("", response_model=list[PaperOut])
def list_papers(
    source: str | None = None,
    selected_only: bool = False,
    limit: int = 500,
    db: Session = Depends(get_db),
):
    """获取文献池列表。"""
    stmt = select(PaperModel).order_by(PaperModel.created_at.desc())
    if source:
        stmt = stmt.where(PaperModel.source == source)
    if selected_only:
        stmt = stmt.where(PaperModel.selected.is_(True))
    stmt = stmt.limit(limit)
    return db.execute(stmt).scalars().all()


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
