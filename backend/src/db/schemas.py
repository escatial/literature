"""Pydantic 请求/响应模型(API 层契约,与 ORM 模型解耦)。"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


# ─── Paper ────────────────────────────────────────────────

class PaperBase(BaseModel):
    source: str
    title: str
    authors: list[str] = Field(default_factory=list)
    journal: str = ""
    year: int = 0
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
    quote_text: str | None = None
    abstract_text: str | None = None
    selected: bool = True


class PaperCreate(PaperBase):
    lit_id: str


class PaperUpdate(BaseModel):
    selected: bool | None = None
    relevance_score: float | None = None


class PaperOut(PaperBase):
    lit_id: str
    created_at: datetime

    model_config = {"from_attributes": True}


class PaperBulkCreate(BaseModel):
    papers: list[PaperCreate]


class PaperBulkCreateResponse(BaseModel):
    inserted: int
    updated: int
    skipped: int


class PaperListResponse(BaseModel):
    """需求5:服务端分页响应。"""
    items: list[PaperOut]
    total: int
    page: int
    page_size: int
    total_pages: int


# ─── 检索历史(需求4) ──────────────────────────────────────

class RetrievalHistoryOut(BaseModel):
    id: int
    topic: str
    sources: list[str] = Field(default_factory=list)
    total_count: int
    failed_sources: dict[str, int] = Field(default_factory=dict)
    papers_snapshot: list[dict] = Field(default_factory=list)
    task_id: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


# ─── Review ───────────────────────────────────────────────

class SectionIn(BaseModel):
    key: str
    title: str
    content: str
    citations: list[str] = Field(default_factory=list)


class ReviewCreate(BaseModel):
    topic: str
    classify_mode: str
    sections: list[SectionIn]
    reference_list: str = ""
    screened_out_ids: list[str] = Field(default_factory=list)
    dropped_citations: list[str] = Field(default_factory=list)


class ReviewOut(ReviewCreate):
    id: int
    created_at: datetime

    model_config = {"from_attributes": True}


# ─── NotifyContact ────────────────────────────────────────

class NotifyContactCreate(BaseModel):
    """新增通知联系人(按 email 幂等 upsert)。"""
    email: str = Field(..., description="联系人邮箱")
    usage: str = Field("api", description="api/report/alert/all")
    enabled: bool = True
    name: str | None = None


class NotifyContactUpdate(BaseModel):
    usage: str | None = None
    enabled: bool | None = None
    name: str | None = None


class NotifyContactOut(NotifyContactCreate):
    id: int
    created_at: datetime

    model_config = {"from_attributes": True}