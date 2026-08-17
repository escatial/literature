"""通知联系人 CRUD API。

用途:
- 登记联系人邮箱(按 email 幂等 upsert);
- 配置通知接收权限(usage: api/report/alert/all;enabled 开关).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import NotifyContactModel
from db.schemas import NotifyContactCreate, NotifyContactOut, NotifyContactUpdate
from db.session import get_db

router = APIRouter(prefix="/contacts", tags=["contacts"])


@router.get("", response_model=list[NotifyContactOut])
def list_contacts(db: Session = Depends(get_db)):
    """全部通知联系人列表。"""
    stmt = select(NotifyContactModel).order_by(NotifyContactModel.created_at.desc())
    return db.execute(stmt).scalars().all()


@router.post("", response_model=NotifyContactOut, status_code=201)
def upsert_contact(req: NotifyContactCreate, db: Session = Depends(get_db)):
    """新增/更新联系人(按 email 幂等)。"""
    email = req.email.strip().lower()
    if not email or "@" not in email:
        raise HTTPException(400, "无效邮箱地址")
    usage = req.usage or "api"
    if usage not in ("api", "report", "alert", "all"):
        raise HTTPException(400, "usage 仅支持 api/report/alert/all")
    stmt = select(NotifyContactModel).where(NotifyContactModel.email == email)
    contact = db.execute(stmt).scalars().first()
    if contact is None:
        contact = NotifyContactModel(
            email=email, usage=usage, enabled=req.enabled, name=req.name
        )
        db.add(contact)
    else:
        contact.usage = usage
        contact.enabled = req.enabled
        contact.name = req.name if req.name is not None else contact.name
    db.commit()
    db.refresh(contact)
    return contact


@router.put("/{contact_id}", response_model=NotifyContactOut)
def update_contact(contact_id: int, req: NotifyContactUpdate, db: Session = Depends(get_db)):
    """更新联系人权限(enabled/usage/name)。"""
    contact = db.get(NotifyContactModel, contact_id)
    if not contact:
        raise HTTPException(404, f"contact {contact_id} not found")
    if req.usage is not None:
        if req.usage not in ("api", "report", "alert", "all"):
            raise HTTPException(400, "usage 仅支持 api/report/alert/all")
        contact.usage = req.usage
    if req.enabled is not None:
        contact.enabled = req.enabled
    if req.name is not None:
        contact.name = req.name
    db.commit()
    db.refresh(contact)
    return contact


@router.delete("/{contact_id}", status_code=204)
def delete_contact(contact_id: int, db: Session = Depends(get_db)):
    """删除联系人。"""
    contact = db.get(NotifyContactModel, contact_id)
    if not contact:
        raise HTTPException(404, f"contact {contact_id} not found")
    db.delete(contact)
    db.commit()
