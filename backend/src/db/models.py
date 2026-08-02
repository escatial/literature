"""ORM 模型。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .session import Base


class PaperModel(Base):
    """文献池条目(中英文统一存储)。"""
    __tablename__ = "papers"

    lit_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    source: Mapped[str] = mapped_column(String(32), index=True)  # openalex/crossref/user_imported
    title: Mapped[str] = mapped_column(String(500), index=True)
    authors: Mapped[list] = mapped_column(JSON, default=list)   # ["A", "B"]
    journal: Mapped[str] = mapped_column(String(200), default="")
    year: Mapped[int] = mapped_column(Integer, default=0, index=True)
    volume: Mapped[str | None] = mapped_column(String(50), nullable=True)
    issue: Mapped[str | None] = mapped_column(String(50), nullable=True)
    pages: Mapped[str | None] = mapped_column(String(50), nullable=True)
    abstract: Mapped[str | None] = mapped_column(Text, nullable=True)
    doi: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    source_url: Mapped[str] = mapped_column(String(500), default="")
    cited_by_count: Mapped[int] = mapped_column(Integer, default=0)
    journal_level: Mapped[str | None] = mapped_column(String(50), nullable=True)
    relevance_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    raw_citation: Mapped[str | None] = mapped_column(Text, nullable=True)  # 中文 GB/T 7714 原文
    selected: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ReviewModel(Base):
    """综述生成记录(每次生成一条)。"""
    __tablename__ = "reviews"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    topic: Mapped[str] = mapped_column(String(500), index=True)
    classify_mode: Mapped[str] = mapped_column(String(20))  # locale/theme
    sections: Mapped[list] = mapped_column(JSON, default=list)  # [{key, title, content, citations}]
    reference_list: Mapped[str] = mapped_column(Text, default="")
    screened_out_ids: Mapped[list] = mapped_column(JSON, default=list)
    dropped_citations: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)