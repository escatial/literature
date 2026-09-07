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


# ─── 写作导入筛选(去写作:筛选预览 + 批量勾选) ────────────────

class LangExportFilter(BaseModel):
    """单语种导出筛选(数量上限 + 年份区间)。0 = 不限。"""
    limit: int = Field(0, ge=0, le=10000, description="导入数量上限,0=不限")
    year_start: int = Field(0, ge=0, le=2100, description="起始年份,0=不限")
    year_end: int = Field(0, ge=0, le=2100, description="结束年份,0=不限")


class ExportPreviewRequest(BaseModel):
    """「去写作」导入预览请求:中英文各自独立的数量/年份筛选。"""
    cn: LangExportFilter = Field(default_factory=LangExportFilter)
    cn_core_only: bool = Field(False, description="中文仅取核心期刊文献")
    # 仅 cn_core_only=true 时生效,取代 cn.limit 作为中文的数量上限
    cn_core_limit: int = Field(0, ge=0, le=10000, description="中文核心导入数量,0=不限")
    en: LangExportFilter = Field(default_factory=LangExportFilter)


class LangPoolRange(BaseModel):
    """全池范围统计(不受筛选影响,供前端生成年份下拉选项)。"""
    total: int
    min_year: int
    max_year: int


class ExportPoolRange(BaseModel):
    cn: LangPoolRange
    en: LangPoolRange


class YearDistItem(BaseModel):
    year: int
    cn: int = 0
    en: int = 0


class ExportPreviewStats(BaseModel):
    """应用筛选(含数量截断)后的导入集合统计。"""
    total: int
    cn_total: int
    cn_core: int
    cn_non_core: int
    en_total: int
    truncated: bool = Field(False, description="候选数超过数量上限发生了截断")
    year_distribution: list[YearDistItem] = Field(default_factory=list)
    lit_ids: list[str] = Field(default_factory=list)


class ExportPreviewResponse(BaseModel):
    pool: ExportPoolRange
    filtered: ExportPreviewStats


class BatchSelectRequest(BaseModel):
    """批量勾选文献(「去写作」导入)。mode=replace 先清空当前任务勾选再标记。"""
    lit_ids: list[str] = Field(default_factory=list)
    mode: str = Field("replace", pattern="^(replace|add)$")


class BatchSelectResponse(BaseModel):
    updated: int
    cleared: int = 0


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