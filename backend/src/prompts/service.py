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


def call_template(template_id: str, vars: dict[str, Any], max_tokens: int = 4000) -> str:
    """加载模板 -> 渲染 -> 调 MiniMax -> 返回助手消息文本。"""
    tpl = PromptTemplate.load(template_id)
    system = tpl.render(**vars)
    user_vars = {k: v for k, v in vars.items() if k != "score_mode"}
    return messages_create(
        system=system,
        user=str(user_vars.get("text") or user_vars.get("topic") or ""),
        max_tokens=max_tokens,
    )
