"""LLM 把用户主题拆成 OpenAlex 英文检索式。

产物:严格 JSON。失败时 fallback 到原始主题。
"""
from __future__ import annotations

import json
import logging
import re

from llm.client import messages_create

log = logging.getLogger(__name__)

QUERY_SYSTEM = """你是学术检索规划专家。
任务:把用户的研究主题拆成 OpenAlex 英文检索参数。
- 中文关键词仅作参考,实际检索用英文
- 输出严格 JSON,字段:
  {
    "keywords_en": ["...","..."],
    "exclude": ["...","..."],
    "year_start": 2020,
    "year_end": 2026,
    "topic_summary": "<一句英文检索意图>"
  }
- 不要列举具体文献,不要使用"据我所知"
- 不要输出 JSON 以外的任何文字,不要用 markdown 代码块包裹"""


def plan_query(topic: str, default_year_start: int = 2020) -> dict:
    """返回 keywords_en/exclude/year_start/year_end/topic_summary。

    LLM 失败或返回非 JSON 时,fallback 到 {keywords_en:[topic], ...}。
    """
    user_msg = f"研究主题:{topic}\n当前年份:2026。请输出严格 JSON:"
    try:
        raw = messages_create(QUERY_SYSTEM, user_msg, max_tokens=600)
        raw = re.sub(r"```(?:json)?|```", "", raw).strip()
        data = json.loads(raw)
    except Exception as e:
        log.warning("query_planner 失败,使用兜底: %s", e)
        data = {}

    keywords = data.get("keywords_en") if isinstance(data.get("keywords_en"), list) else None
    if not keywords:
        keywords = [topic]

    try:
        year_start = int(data.get("year_start") or default_year_start)
        year_end = int(data.get("year_end") or 2026)
    except (ValueError, TypeError):
        year_start, year_end = default_year_start, 2026

    return {
        "keywords_en": [str(k) for k in keywords],
        "exclude": [str(x) for x in (data.get("exclude") or [])],
        "year_start": year_start,
        "year_end": year_end,
        "topic_summary": str(data.get("topic_summary") or topic),
    }
