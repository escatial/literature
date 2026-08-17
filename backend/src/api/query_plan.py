"""LLM 查询规划 API:把中文主题拆成英文检索关键词。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, model_validator

from retrieval.query_planner import plan_query, render_cnki_expert, render_en_candidates
from retrieval.reranker import rerank
from retrieval.sources.openalex import OpenAlexSource
from retrieval.sources.pubmed import PubMedSource
from retrieval.types import Paper, Source

router = APIRouter()


class QueryPlanRequest(BaseModel):
    topic: str = Field(..., min_length=1)
    year_start: int | None = None


class ConceptGroup(BaseModel):
    """一个核心概念 + 中英文同义词。"""
    id: str
    label: str
    label_en: str = ""
    synonyms_zh: list[str] = Field(default_factory=list)
    synonyms_en: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _fill_legacy_label(cls, data):
        """兼容 retrieval.intent.Concept 仅返回 label_en 的旧结构。"""
        if isinstance(data, dict):
            label = (data.get("label") or "").strip()
            label_zh = (data.get("label_zh") or "").strip()
            label_en = (data.get("label_en") or "").strip()
            if not label and (label_zh or label_en):
                data["label"] = label_zh or label_en
            if not label_en and label:
                data["label_en"] = label
        return data


class QueryPlanResponse(BaseModel):
    topic_summary: str
    keywords_en: list[str]
    keywords_zh: list[str] = Field(default_factory=list)
    query_str: str
    # 新增:结构化概念组 + 中英检索式
    concepts: list[ConceptGroup] = Field(default_factory=list)
    query_zh: str = ""
    queries_zh: list[str] = Field(default_factory=list)
    query_en: str = ""
    # 英文长检索式按语义单元拆分后的子检索式列表(OpenAlex 方言,依次执行后合并去重)
    queries_en: list[str] = Field(default_factory=list)
    field_zh: str = "SU"
    field_en: str = "default"
    # 三库方言对比预览:同一份检索意图分别翻译成的本库检索式
    query_cnki: str = ""
    query_openalex: str = ""
    query_pubmed: str = ""


@router.post("/query-plan", response_model=QueryPlanResponse)
def query_plan(req: QueryPlanRequest):
    """用 MiniMax 把中文主题拆成结构化检索式(概念 + 同义词 + 中英布尔式)。"""
    if not req.topic.strip():
        raise HTTPException(400, "topic 不能为空")
    planned = plan_query(req.topic, default_year_start=req.year_start)
    keywords = planned.get("keywords_en") or [req.topic]
    intent = planned.get("intent")
    # 同一份 SearchIntent 分别翻译成三个库的本地检索式,便于前端对比预览
    query_cnki = render_cnki_expert(intent) if intent else ""
    query_openalex = _render_openalex_query(intent) if intent else ""
    query_pubmed = _render_pubmed_query(intent) if intent else ""
    # 英文子检索式列表(按语义单元拆分,OpenAlex 方言渲染)
    queries_en = _render_en_sub_queries(intent) if intent else []
    return QueryPlanResponse(
        topic_summary=planned.get("topic_summary", req.topic),
        keywords_en=keywords,
        keywords_zh=planned.get("keywords_zh", [req.topic]),
        query_str=" ".join(keywords),
        concepts=[ConceptGroup(**c) for c in planned.get("concepts", [])],
        query_zh=planned.get("query_zh", req.topic),
        queries_zh=planned.get("queries_zh", []),
        query_en=planned.get("query_en", req.topic),
        queries_en=queries_en,
        field_zh=planned.get("field_zh", "SU"),
        field_en=planned.get("field_en", "default"),
        query_cnki=query_cnki,
        query_openalex=query_openalex,
        query_pubmed=query_pubmed,
    )


def _render_en_sub_queries(intent) -> list[str]:
    """把 render_en_candidates 的概念模板渲染成 OpenAlex 可读子检索式列表。"""
    src = OpenAlexSource()
    out: list[str] = []
    for template in render_en_candidates(intent):
        try:
            q = src.build_sub_query(intent, list(template))
            out.append("search=" + q.get("search", ""))
        except Exception:
            continue
    return out


def _render_openalex_query(intent) -> str:
    """OpenAlex build_query 返回 dict,拼成可读的查询串方便对比。"""
    q = OpenAlexSource().build_query(intent)
    return "search=" + q.get("search", "") + "\nfilter=" + q.get("filter", "")


def _render_pubmed_query(intent) -> str:
    """PubMed build_query 返回 {"term": ...},直接取 term。"""
    return PubMedSource().build_query(intent).get("term", "")


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
