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