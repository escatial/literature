"""提示词模板服务:加载、渲染、调用 LLM。"""
from __future__ import annotations

import logging
from typing import Any

from src.llm.client import messages_create
from . import PromptTemplate, list_templates

log = logging.getLogger(__name__)


def list_available() -> list[dict[str, str]]:
    """列出所有可用模板(只返回 id,name/description 等 skill 字段已移除)。"""
    return [{"id": tid} for tid in list_templates()]


def render(template_id: str, **vars) -> str:
    tpl = PromptTemplate.load(template_id)
    return tpl.render(**vars)


def call_template(
    template_id: str,
    vars: dict[str, Any],
    max_tokens: int = 4000,
    response_format: dict | None = None,
) -> str:
    """加载模板 -> 渲染 -> 调 MiniMax -> 返回助手消息文本。

    response_format: 传给 OpenAI 兼容 API 的结构化输出参数。
    - None: 自由输出(默认)
    - {"type": "json_object"}: 强制 JSON 模式(MiniMax 支持)
    适用于需要 LLM 严格按 JSON 输出的场景(人审、分
    类、评分等),非 JSON 场景不要传。
    """
    tpl = PromptTemplate.load(template_id)
    system = tpl.render(**vars)
    user_vars = {k: v for k, v in vars.items() if k != "score_mode"}
    return messages_create(
        system=system,
        user=str(user_vars.get("text") or user_vars.get("topic") or ""),
        max_tokens=max_tokens,
        response_format=response_format,
    )

def parse_llm_json(out: str) -> dict:
    """从 LLM 输出里抽 JSON, 兼容 dict / list[dict] / list[str] / 纯文本。

    LLM 偶发返回:
      - 标准 ````json { ... }```` (期望,response_format=json_object 启用后常态)
      - ````[{...}, ...]```` (LLM 把 dict 套了 list)
      - 裸 JSON `{...}` 或 `[...]` 无代码围栏
      - 完全不是 JSON(那就把整段当 rewritten)

    与 `_parse_llm_json` 同一份逻辑, 提为公共工具供 api/writing 等模块复用。
    """
    import json
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
        if isinstance(obj, list):
            for item in obj:
                if isinstance(item, dict):
                    return item
            if obj and isinstance(obj[0], str):
                return {"rewritten": obj[0]}
            return {"rewritten": ""}
        if isinstance(obj, dict):
            return obj
        if isinstance(obj, str):
            return {"rewritten": obj}
    return {"rewritten": out}
