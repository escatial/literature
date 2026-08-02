"""提示词模板服务:加载、渲染、调用 LLM。

两个外部 skill 已通过模板在本项目落地:
- humanizer-zh -> prompts/humanizer-zh.md
- literature-review -> prompts/literature-review-section.md
                       + prompts/literature-review-classify.md
"""
from __future__ import annotations

import logging
from typing import Any

from src.llm.client import messages_create
from . import PromptTemplate, list_templates

log = logging.getLogger(__name__)


def list_available() -> list[dict[str, str]]:
    return [
        {"id": tid, "description": PromptTemplate.load(tid).params.get("description", "")}
        for tid in list_templates()
    ]


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