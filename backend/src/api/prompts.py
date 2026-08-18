"""提示词模板管理 API。"""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from prompts.service import call_template, list_available, parse_llm_json, render

router = APIRouter(prefix="/prompts", tags=["prompts"])


@router.get("")
def list_prompts():
    return {"templates": list_available()}


@router.get("/{template_id}")
def get_prompt(template_id: str):
    from prompts import PromptTemplate

    try:
        tpl = PromptTemplate.load(template_id)
    except FileNotFoundError:
        raise HTTPException(404, f"template '{template_id}' not found")
    return {
        "id": tpl.id,
        "params": tpl.params,
        "body_preview": tpl.body[:400],
    }


class RenderRequest(BaseModel):
    template_id: str
    vars: dict


@router.post("/render")
def render_prompt(req: RenderRequest):
    """预览模板渲染结果(不调用 LLM)。"""
    try:
        rendered = render(req.template_id, **req.vars)
    except FileNotFoundError:
        raise HTTPException(404, f"template '{req.template_id}' not found")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"rendered": rendered}


class CallRequest(BaseModel):
    template_id: str
    vars: dict
    max_tokens: int = Field(default=4000, ge=1, le=32000)


@router.post("/call")
def call_prompt(req: CallRequest):
    """渲染模板并调用 LLM,返回助手文本。"""
    try:
        out = call_template(req.template_id, req.vars, max_tokens=req.max_tokens)
    except FileNotFoundError:
        raise HTTPException(404, f"template '{req.template_id}' not found")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"output": out}


class HumanizeRequest(BaseModel):
    text: str
    score_mode: bool = True


class HumanizeResponse(BaseModel):
    rewritten: str
    changes: list[str] = []
    score: dict = {}
    raw: str


@router.post("/humanize", response_model=HumanizeResponse)
def humanize_text(req: HumanizeRequest):
    """便捷入口:直接调 humanize 模板, 强制 JSON 输出 (response_format=json_object)。"""
    try:
        out = call_template(
            "literature-review:humanize",
            {"text": req.text, "score_mode": req.score_mode},
            max_tokens=2000,
            response_format={"type": "json_object"},
        )
    except FileNotFoundError:
        raise HTTPException(500, "humanize template missing")

    parsed = parse_llm_json(out)

    return HumanizeResponse(
        rewritten=parsed.get("rewritten", ""),
        changes=parsed.get("changes", []) or [],
        score=parsed.get("score", {}) or {},
        raw=out,
    )


def _parse_llm_json(out: str) -> dict:
    """从 LLM 输出里抽 JSON, 兼容 dict / list[dict] / list[str] / 纯文本。

    LLM 偶发返回:
      - 标准 ````json { ... }```` (期望)
      - ````[{...}, ...]```` (LLM 把 dict 套了 list)
      - 裸 JSON `{...}` 或 `[...]` 无代码围栏
      - 完全不是 JSON(那就把整段当 rewritten)
    """
    candidates = []
    if "```json" in out:
        start = out.find("```json") + len("```json")
        end = out.find("```", start)
        if end > start:
            candidates.append(out[start:end].strip())
    if "```" in out:
        start = out.find("```") + 3
        end = out.find("```", start)
        if end > start:
            candidates.append(out[start:end].strip())
    candidates.append(out.strip())

    for body in candidates:
        try:
            obj = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            continue
        # 套娃 list: 取第一个 dict 元素
        if isinstance(obj, list):
            for item in obj:
                if isinstance(item, dict):
                    return item
            # list 全是 str: 把第一个当 rewritten
            if obj and isinstance(obj[0], str):
                return {"rewritten": obj[0]}
            return {"rewritten": ""}
        if isinstance(obj, dict):
            return obj
        if isinstance(obj, str):
            return {"rewritten": obj}
    # 都失败了
    return {"rewritten": out}
