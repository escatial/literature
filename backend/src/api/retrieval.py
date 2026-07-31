"""英文文献检索 API。/api/retrieval/search"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from retrieval.filters import by_min_citations, by_year, deduplicate
from retrieval.openalex_adapter import OpenAlexAdapter
from retrieval.query_planner import plan_query
from retrieval.reranker import rerank

router = APIRouter()


class SearchRequest(BaseModel):
    topic: str = Field(..., min_length=1)
    year_start: int = 2020
    year_end: int = 2026
    min_citations: int = 0
    limit: int = 50
    use_rerank: bool = True


class SearchResponse(BaseModel):
    topic_summary: str
    query_used: str
    total_before_filter: int
    total_after_filter: int
    papers: list[dict]


@router.post("/retrieval/search", response_model=SearchResponse)
async def search(req: SearchRequest):
    """英文文献检索主入口。"""
    if not req.topic.strip():
        raise HTTPException(400, "topic 不能为空")

    # 1. LLM 拆词(失败兜底使用原主题)
    planned = plan_query(req.topic, default_year_start=req.year_start)
    query_str = " ".join(planned["keywords_en"]) if planned["keywords_en"] else req.topic

    # 2. OpenAlex 检索
    adapter = OpenAlexAdapter()
    raw = adapter.search(
        query=query_str,
        year_range=(req.year_start, req.year_end),
        per_page=req.limit,
    )

    # 3. 过滤 + 去重
    papers = by_year(raw, (req.year_start, req.year_end))
    papers = by_min_citations(papers, req.min_citations)
    papers = deduplicate(papers)

    # 4. LLM 重排(可选,失败不阻塞)
    if req.use_rerank and papers:
        papers = rerank(papers, planned["topic_summary"], top_n=min(req.limit, 50))

    return SearchResponse(
        topic_summary=planned["topic_summary"],
        query_used=query_str,
        total_before_filter=len(raw),
        total_after_filter=len(papers),
        papers=[p.to_dict() for p in papers],
    )
