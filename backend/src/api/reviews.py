"""综述历史 CRUD API。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import ReviewModel
from db.schemas import ReviewCreate, ReviewOut
from db.session import get_db

router = APIRouter(prefix="/reviews", tags=["reviews"])


@router.get("", response_model=list[ReviewOut])
def list_reviews(limit: int = 50, db: Session = Depends(get_db)):
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
