"""激进简化版综述 API: topic in -> 综述 out。"""
from __future__ import annotations

import time
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from src.llm.client import (
    get_default_provider,
    get_provider_health,
    get_fallback_order,
    list_llm_providers,
    messages_create,
)
from src.review.simple_review import run_simple_review

router = APIRouter(prefix="/review", tags=["review"])


class SimpleReviewRequest(BaseModel):
    topic: str = Field(..., min_length=2, max_length=200)
    max_papers: int = Field(default=20, ge=5, le=50)
    provider: str | None = None
    model: str | None = None


class SimpleReviewResponse(BaseModel):
    topic: str
    review: str
    references: list[str]
    papers_found: int
    query: dict
    provider: str


class LLMTestResponse(BaseModel):
    ok: bool
    provider: str
    elapsed_ms: int
    response_preview: str


@router.get("/providers")
def review_providers() -> dict:
    """返回可选 LLM provider 列表 + 真正的轮换顺序(供前端 dashboard)。"""
    return {
        "default": get_default_provider(),
        "fallback_order": list(get_fallback_order()),
        "providers": list_llm_providers(),
    }


@router.get("/providers/health")
def review_providers_health() -> dict:
    """运行时健康态:每个 provider 最近一次调用结果 + 真实生效顺序。

    UI 可以用它显示「当前一级 provider 不可用,降级到 GPT」这类提示。
    """
    health = get_provider_health()
    return {
        "default": health["default"],
        "fallback_order": list(get_fallback_order()),
        "active_fallback_order": health["active_fallback_order"],
        "providers": health["providers"],
    }


@router.post("/test", response_model=LLMTestResponse)
def review_llm_test() -> LLMTestResponse:
    """发送最小 hello 请求，验证当前 LLM provider 的真实连通性。"""
    started = time.perf_counter()
    try:
        text = messages_create(
            system="你是连通性测试助手。请简短回答。",
            user="hello",
            max_tokens=32,
            max_retries=1,
            timeout=20.0,
        )
        health = get_provider_health()
        active = health.get("active_fallback_order") or [get_default_provider()]
        return LLMTestResponse(
            ok=bool(str(text).strip()),
            provider=str(active[0]),
            elapsed_ms=round((time.perf_counter() - started) * 1000),
            response_preview=str(text).strip()[:120],
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"LLM 测试失败: {exc}") from exc


@router.post("/simple", response_model=SimpleReviewResponse)
def review_simple(req: SimpleReviewRequest) -> SimpleReviewResponse:
    """激进简化版: 2 LLM + 1 检索, 一次返回全文。"""
    result = run_simple_review(req.topic, req.max_papers, provider=req.provider, model=req.model)
    return SimpleReviewResponse(topic=req.topic, **result)
