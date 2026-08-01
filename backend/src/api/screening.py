"""主题不符筛选 API。/api/screening/filter"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from retrieval.types import Paper
from screening.llm_filter import screen_batch

router = APIRouter()


class ScreenRequest(BaseModel):
    topic: str
    papers: list[dict]


class ScreenResponse(BaseModel):
    results: list[dict]


@router.post("/screening/filter", response_model=ScreenResponse)
async def screen(req: ScreenRequest):
    if not req.topic.strip():
        raise HTTPException(400, "topic 不能为空")
    papers = [Paper(**p) for p in req.papers]
    results = screen_batch(papers, req.topic)
    return ScreenResponse(results=results)
