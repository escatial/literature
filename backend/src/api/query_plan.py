"""LLM 查询规划 API:把中文主题直接拆成 3 库 × 3 条检索式。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from retrieval.query_planner import plan_query_strings
from retrieval.reranker import rerank
from retrieval.types import Paper, Source

router = APIRouter()


class QueryPlanRequest(BaseModel):
    topic: str = Field(..., min_length=1)
    year_start: int | None = None


class QueryPlanResponse(BaseModel):
    topic_summary: str
    # 3 库各自的 3 条检索式,后端按源透传给对应数据源
    queries_cnki: list[str]
    queries_openalex: list[str]
    queries_pubmed: list[str]


@router.post("/query-plan", response_model=QueryPlanResponse)
def query_plan(req: QueryPlanRequest):
    """用 LLM 把中文主题直接拆成 3 库各自的 3 条检索式字符串。

    失败(LLM 抽风 / 不合法 JSON / 某库不是 3 条) → 抛 500 让用户重提,
    不静默兜底。
    """
    if not req.topic.strip():
        raise HTTPException(400, "topic 不能为空")
    try:
        planned = plan_query_strings(req.topic, year=req.year_start)
    except RuntimeError as exc:
        # LLM 失败重试都失败:返回 502 让前端明确告知用户
        raise HTTPException(502, f"LLM 规划失败: {exc}") from exc
    return QueryPlanResponse(
        topic_summary=planned["topic_summary"],
        queries_cnki=planned["queries_cnki"],
        queries_openalex=planned["queries_openalex"],
        queries_pubmed=planned["queries_pubmed"],
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