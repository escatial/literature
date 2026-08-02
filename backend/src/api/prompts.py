"""提示词模板管理 API。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from prompts.service import call_template, list_available, render

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
    max_tokens: int = 4000


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
    """便捷入口:直接调 humanizer-zh 模板。"""
    import json

    try:
        out = call_template(
            "humanizer-zh",
            {"text": req.text, "score_mode": req.score_mode},
            max_tokens=2000,
        )
    except FileNotFoundError:
        raise HTTPException(500, "humanizer-zh template missing")

    parsed: dict = {}
    # 尝试从 markdown code block 里抽 JSON
    if "```json" in out:
        start = out.find("```json") + len("```json")
        end = out.find("```", start)
        body = out[start:end].strip()
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            parsed = {"rewritten": body}
    elif "```" in out:
        start = out.find("```") + 3
        end = out.find("```", start)
        body = out[start:end].strip()
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            parsed = {"rewritten": body}
    else:
        try:
            parsed = json.loads(out)
        except json.JSONDecodeError:
            parsed = {"rewritten": out}

    return HumanizeResponse(
        rewritten=parsed.get("rewritten", ""),
        changes=parsed.get("changes", []) or [],
        score=parsed.get("score", {}) or {},
        raw=out,
    )