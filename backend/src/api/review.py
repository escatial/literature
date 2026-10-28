"""激进简化版综述 API: topic in -> 综述 out。"""
from __future__ import annotations

import time
from fastapi import APIRouter, HTTPException, Request
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
    model: str
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
def review_llm_test(request: Request) -> LLMTestResponse:
    """发送最小 hello 请求，验证当前 LLM provider 的真实连通性。

    按发起用户的 LLM 配置测试(自定义 base_url/key 优先,未配置回落系统 env)。
    C1:请求作用域绑定 —— try/finally 恢复上下文,线程池复用防泄漏。
    """
    from security.auth import apply_user_llm

    llm_reset = apply_user_llm(getattr(request.state, "user", None))
    started = time.perf_counter()
    try:
        text = messages_create(
            system="你是连通性测试助手。请简短回答。",
            user="hello",
            max_tokens=32,
            max_retries=1,
            timeout=20.0,
        )
        from src.llm.client import resolve_provider, get_model_override
        rp = resolve_provider("minimax")
        model_name = get_model_override() or rp.model
        health = get_provider_health()
        active = health.get("active_fallback_order") or [get_default_provider()]
        return LLMTestResponse(
            ok=bool(str(text).strip()),
            provider=str(active[0]),
            model=model_name,
            elapsed_ms=round((time.perf_counter() - started) * 1000),
            response_preview=str(text).strip()[:120],
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"LLM 测试失败: {exc}") from exc
    finally:
        llm_reset()


class ModelInfo(BaseModel):
    id: str
    owned_by: str = ""
    active: bool = False


class ModelSelectRequest(BaseModel):
    model_id: str = Field(..., min_length=1)


def _user_llm_ready(user) -> bool:
    return bool(
        user is not None
        and (user.llm_base_url or "").strip()
        and (user.llm_api_key or "").strip()
    )


@router.get("/models/upstream")
def fetch_upstream_models(request: Request) -> dict:
    """获取可用模型列表——用户感知:已配置自定义 LLM 的用户拉自己的上游,
    未配置的回落系统 env MiniMax。响应带 user_configured 供前端提醒。"""
    from src.llm.client import (
        fetch_available_models,
        fetch_models_for,
        get_model_override,
    )

    user = getattr(request.state, "user", None)
    if _user_llm_ready(user):
        try:
            models = fetch_models_for(
                (user.llm_kind or "openai").strip().lower(),
                user.llm_base_url.strip(), user.llm_api_key.strip(),
            )
        except Exception as exc:  # noqa: BLE001
            return {
                "models": [], "active": user.llm_model or "",
                "user_configured": True, "source": "user",
                "error": f"获取模型列表失败:{exc}",
            }
        active = (user.llm_model or "").strip()
        for m in models:
            m["active"] = (m["id"] == active)
        return {
            "models": models, "active": active,
            "user_configured": True, "source": "user",
        }

    models = fetch_available_models("minimax")
    active = get_model_override()
    for m in models:
        m["active"] = (m["id"] == active)
    return {
        "models": models, "active": active,
        "user_configured": False, "source": "system",
    }


@router.post("/models/select")
def select_model(req: ModelSelectRequest, request: Request) -> dict:
    """切换模型——用户感知:已配置用户改自己的 llm_model(只影响自己的任务);
    未配置用户保持旧语义(系统级覆盖,影响所有人)。"""
    from src.llm.client import fetch_models_for, set_model_override, fetch_available_models

    user = getattr(request.state, "user", None)
    if _user_llm_ready(user):
        available = {
            m["id"] for m in fetch_models_for(
                (user.llm_kind or "openai").strip().lower(),
                user.llm_base_url.strip(), user.llm_api_key.strip(),
            )
        }
        if req.model_id not in available:
            raise HTTPException(
                400, f"模型 {req.model_id} 不在你的上游可用列表中。可用: {', '.join(sorted(available))}"
            )
        from db import session as db_session
        from db.models import UserModel

        with db_session.SessionLocal() as db:
            row = db.query(UserModel).filter_by(user_id=user.user_id).one_or_none()
            if row is None:
                raise HTTPException(404, "用户不存在")
            row.llm_model = req.model_id
            db.commit()
        return {"selected": req.model_id, "ok": True, "scope": "user"}

    available = {m["id"] for m in fetch_available_models("minimax")}
    if req.model_id not in available:
        raise HTTPException(400, f"模型 {req.model_id} 不在可用列表中。可用: {', '.join(sorted(available))}")
    set_model_override(req.model_id)
    return {"selected": req.model_id, "ok": True, "scope": "system"}


@router.get("/models/current")
def get_current_model() -> dict:
    """返回当前生效的模型名。"""
    from src.llm.client import get_model_override, resolve_provider
    override = get_model_override()
    try:
        rp = resolve_provider("minimax")
        return {"override": override, "effective": rp.model}
    except Exception:
        return {"override": override, "effective": None}


@router.post("/simple", response_model=SimpleReviewResponse)
def review_simple(req: SimpleReviewRequest) -> SimpleReviewResponse:
    """激进简化版: 2 LLM + 1 检索, 一次返回全文。"""
    result = run_simple_review(req.topic, req.max_papers, provider=req.provider, model=req.model)
    return SimpleReviewResponse(topic=req.topic, **result)
