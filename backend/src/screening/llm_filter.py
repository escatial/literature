"""综述前的语义筛选:用 LLM 判断每篇文献是否与研究主题相符。

只输出布尔值与一句话原因,不构造任何论文字段。
LLM 失败或返回非 JSON 时,兜底为"全部视为相关"(不阻塞写作)。
"""
from __future__ import annotations

import json
import logging
import re

from llm.client import messages_create
from retrieval.types import Paper

log = logging.getLogger(__name__)

SYSTEM = """你是学术论文相关性筛选助手。
任务:对每篇论文,基于其标题与摘要,判断是否与"用户研究主题"实质相关。
- 实质相关:研究主题/对象/方法/理论/应用任一层面有交集
- 主题不符:完全无关、跨学科无连接、研究对象差异大

严格输出 JSON 数组(不要其他文字):
[
  {"lit_id": "<原文 lit_id>", "relevant": true|false, "reason": "<一句话中文原因>"}
]
不要输出 JSON 以外的内容,不要用 markdown 代码块包裹。"""


def screen_batch(papers: list[Paper], topic: str, max_chars: int = 400) -> list[dict]:
    """对一批论文做 LLM 主题相关筛选。"""
    if not papers:
        return []

    payload = [
        {"lit_id": p.lit_id, "title": p.title,
         "abstract": (p.abstract or "")[:max_chars]}
        for p in papers
    ]
    user = (
        f"研究主题:{topic}\n"
        f"候选论文:\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n"
        "请输出严格 JSON 数组。"
    )
    try:
        raw = messages_create(SYSTEM, user, max_tokens=4000, response_format={"type": "json_object"})
        raw = re.sub(r"```(?:json)?|```", "", raw).strip()
        decisions = json.loads(raw)
    except Exception as e:
        log.warning("screen_batch LLM 失败,全部视为相关: %s", e)
        return [
            {"lit_id": p.lit_id, "relevant": True, "reason": "LLM 解析失败,默认保留"}
            for p in papers
        ]

    if not isinstance(decisions, list):
        return [
            {"lit_id": p.lit_id, "relevant": True, "reason": "返回非数组,默认保留"}
            for p in papers
        ]

    # 字段清洗
    out = []
    valid_ids = {p.lit_id for p in papers}
    for d in decisions:
        if not isinstance(d, dict):
            continue
        lit_id = str(d.get("lit_id", ""))
        if lit_id not in valid_ids:
            continue
        out.append({
            "lit_id": lit_id,
            "relevant": bool(d.get("relevant", True)),
            "reason": str(d.get("reason", ""))[:200],
        })
    # 兜底:LLM 没覆盖到的 ID 默认保留
    seen_ids = {o["lit_id"] for o in out}
    for p in papers:
        if p.lit_id not in seen_ids:
            out.append({"lit_id": p.lit_id, "relevant": True, "reason": "LLM 未评分,默认保留"})
    return out
