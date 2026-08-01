"""LLM 查询规划 API:把中文主题拆成英文检索关键词。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from retrieval.query_planner import plan_query
from retrieval.reranker import rerank
from retrieval.types import Paper, Source

router = APIRouter()


class QueryPlanRequest(BaseModel):
    topic: str = Field(..., min_length=1)
    year_start: int = 2020


class QueryPlanResponse(BaseModel):
    topic_summary: str
    keywords_en: list[str]
    query_str: str


@router.post("/query-plan", response_model=QueryPlanResponse)
def query_plan(req: QueryPlanRequest):
    """用 MiniMax 把中文主题拆成英文检索关键词。"""
    if not req.topic.strip():
        raise HTTPException(400, "topic 不能为空")
    planned = plan_query(req.topic, default_year_start=req.year_start)
    keywords = planned.get("keywords_en") or [req.topic]
    return QueryPlanResponse(
        topic_summary=planned.get("topic_summary", req.topic),
        keywords_en=keywords,
        query_str=" ".join(keywords),
    )


class RerankPaperIn(BaseModel):
    """前端 Paper 镜像(无 source_url 等可选字段差异,用 dict 更稳)。"""

    lit_id: str
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

    def to_paper(self) -> Paper:
        return Paper(
            lit_id=self.lit_id,
            source=Source(self.source),
            title=self.title,
            authors=self.authors,
            journal=self.journal,
            year=self.year,
            volume=self.volume,
            issue=self.issue,
            pages=self.pages,
            abstract=self.abstract,
            doi=self.doi,
            source_url=self.source_url,
            cited_by_count=self.cited_by_count,
            journal_level=self.journal_level,
            relevance_score=self.relevance_score,
        )


class RerankRequest(BaseModel):
    topic: str
    papers: list[RerankPaperIn]
    top_n: int = 50


class RerankResponse(BaseModel):
    papers: list[dict]


@router.post("/rerank", response_model=RerankResponse)
def rerank_papers(req: RerankRequest):
    """LLM 相关度重排。失败兜底返回原顺序。"""
    papers = [p.to_paper() for p in req.papers]
    ranked = rerank(papers, req.topic, top_n=req.top_n)
    return RerankResponse(papers=[p.to_dict() for p in ranked])
