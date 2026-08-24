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
def screen(req: ScreenRequest):
    """主题不符筛选。

    保持同步:screen_batch 会调 LLM(数秒级),放线程里没有收益,
    但放进 async handler 会阻塞事件循环直到 LLM 返回,期间其他请求全部卡住,
    并且中间代理会因为长 keep-alive 静默而主动切断连接(产生 ECONNRESET)。
    同步端点由 starlette 自动丢到外层 threadpool,与 async 互不阻塞。
    """
    if not req.topic.strip():
        raise HTTPException(400, "topic 不能为空")
    papers = [Paper(**p) for p in req.papers]
    results = screen_batch(papers, req.topic)
    return ScreenResponse(results=results)
