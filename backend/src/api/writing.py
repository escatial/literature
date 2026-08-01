"""综述写作 API。"""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from src.retrieval.types import Paper, Source
from src.writing.orchestrator import generate_review, render_reference_list

router = APIRouter(tags=["writing"])


class PaperIn(BaseModel):
    """前端 IndexedDB 里 Paper 的镜像。后端不持久化,只做一次性消费。"""

    lit_id: str
    source: str  # "openalex" | "crossref" | "user_imported"
    title: str
    authors: list[str] = Field(default_factory=list)
    journal: str | None = None
    year: int | None = None
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

    def to_paper(self) -> Paper:
        return Paper(
            lit_id=self.lit_id,
            source=Source(self.source),
            title=self.title,
            authors=self.authors,
            journal=self.journal or "",
            year=self.year or 0,
            volume=self.volume,
            issue=self.issue,
            pages=self.pages,
            abstract=self.abstract,
            doi=self.doi,
            source_url=self.source_url,
            cited_by_count=self.cited_by_count,
            journal_level=self.journal_level,
            relevance_score=self.relevance_score,
            raw_citation=self.raw_citation,
        )


class WritingRequest(BaseModel):
    topic: str
    papers: list[PaperIn]
    classify_mode: str  # "locale" | "theme"
    do_screening: bool = True


class SectionOut(BaseModel):
    key: str
    title: str
    content: str
    citations: list[str]


class GroupOut(BaseModel):
    name: str
    lit_ids: list[str]


class WritingResponse(BaseModel):
    topic: str
    classify_mode: str
    groups: list[GroupOut]
    sections: list[SectionOut]
    reference_list: str
    screened_out_ids: list[str]
    dropped_citations: list[str]


@router.post("/writing/generate", response_model=WritingResponse)
def writing_generate(req: WritingRequest) -> WritingResponse:
    papers = [p.to_paper() for p in req.papers]
    result = generate_review(
        topic=req.topic,
        papers=papers,
        classify_mode=req.classify_mode,
        do_screening=req.do_screening,
    )
    # 参考文献列表:只纳入正文实际引用的文献
    cited_ids = {cid for s in result.sections for cid in s.citations}
    cited_papers = [p for p in papers if p.lit_id in cited_ids]
    ref = render_reference_list(cited_papers)

    return WritingResponse(
        topic=result.topic,
        classify_mode=result.classify_mode,
        groups=[GroupOut(name=g.name, lit_ids=g.lit_ids) for g in result.groups],
        sections=[
            SectionOut(
                key=s.key, title=s.title, content=s.content, citations=s.citations
            )
            for s in result.sections
        ],
        reference_list=ref,
        screened_out_ids=result.screened_out_ids,
        dropped_citations=result.dropped_citations,
    )
