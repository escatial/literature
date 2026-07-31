"""LLM 相关度重排。

基于论文标题与摘要,输出 0-100 整数相关度分。
LLM 只输出分数与原因,不输出引文字段。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Iterable

from llm.client import messages_create
from .types import Paper

log = logging.getLogger(__name__)

RERANK_SYSTEM = """你是学术相关性评分专家。
任务:对每篇论文,根据其标题与摘要,输出与用户研究主题的相关度(0-100 整数)。
只输出严格 JSON 数组,每个元素结构:
{"lit_id": "<原文 lit_id>", "score": <0~100 整数>, "reason": "<一句话中文原因>"}
不要输出任何其他文字、不要输出 markdown 代码块包裹。"""


def _strip_md_fences(text: str) -> str:
    m = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", text)
    return m.group(1).strip() if m else text.strip()


def rerank(
    papers: list[Paper],
    topic: str,
    top_n: int = 50,
) -> list[Paper]:
    """对前 N 篇打分,按分降序返回。

    LLM 调用失败时,返回原始顺序(不阻塞主流程)。
    """
    if not papers:
        return []

    sample = papers[:top_n]
    payload = [
        {
            "lit_id": p.lit_id,
            "title": p.title,
            "abstract": (p.abstract or "")[:600],
        }
        for p in sample
    ]
    user_msg = (
        f"研究主题:{topic}\n"
        f"候选论文:\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n"
        "请输出严格 JSON 数组。"
    )

    try:
        raw = messages_create(RERANK_SYSTEM, user_msg, max_tokens=2000)
        raw = _strip_md_fences(raw)
        scores = json.loads(raw)
    except (json.JSONDecodeError, Exception) as e:
        log.warning("rerank LLM 调用或解析失败,使用原始顺序: %s", e)
        return sample

    if not isinstance(scores, list):
        log.warning("rerank 返回非列表,使用原始顺序")
        return sample

    score_map: dict[str, float] = {}
    for s in scores:
        if isinstance(s, dict) and "lit_id" in s and "score" in s:
            try:
                score_map[str(s["lit_id"])] = float(s["score"])
            except (ValueError, TypeError):
                continue

    def sort_key(p: Paper) -> float:
        return score_map.get(p.lit_id, 0.0)

    sorted_papers = sorted(sample, key=sort_key, reverse=True)
    # 把分数回写到 paper
    for p in sorted_papers:
        p.relevance_score = score_map.get(p.lit_id)
    # 把没打分的也补一份原顺序(可选,这里不修改原 papers 列表)
    return sorted_papers
