"""综述分类器:按国内外 / 按主题 对文献分组。

用户在工作流中明确:开始写作前会告知是"按照国内外分类"还是"按照不同主题划分"。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from src.llm.client import messages_create
from src.retrieval.types import Paper, Source


@dataclass
class Group:
    """一组文献。"""

    name: str
    lit_ids: list[str] = field(default_factory=list)


def classify_by_locale(papers: list[Paper]) -> list[Group]:
    """国内外分类:中文导入(USER_IMPORTED)为国内,其余为国外。"""
    domestic = [p.lit_id for p in papers if p.source == Source.USER_IMPORTED]
    foreign = [p.lit_id for p in papers if p.source != Source.USER_IMPORTED]
    groups: list[Group] = []
    if domestic:
        groups.append(Group(name="国内研究", lit_ids=domestic))
    if foreign:
        groups.append(Group(name="国外研究", lit_ids=foreign))
    return groups


def classify_by_theme(papers: list[Paper], topic: str) -> list[Group]:
    """主题分类:LLM 将文献归入若干主题。

    兜底策略:
    - LLM 失败/非法 JSON → 全部归入"综合研究"
    - 未知 lit_id → 跳过
    - 未被覆盖的 lit_id → 归入"其他"
    """
    if not papers:
        return []

    catalog = "\n".join(
        f"- {p.lit_id} | {p.title} | {p.journal or 'N/A'} | {p.year or 'N/A'}"
        for p in papers
    )
    system = (
        "你是学术文献分类助手。给定研究主题与文献清单，"
        "将文献划分为 3~6 个主题组。只输出严格 JSON，"
        '格式: [{"theme": "主题名", "lit_ids": ["lit_..."]}]。'
        "lit_id 必须从输入中精确复制，禁止编造。"
    )
    user = f"研究主题:{topic}\n\n文献清单:\n{catalog}"

    try:
        raw = messages_create(system=system, user=user, max_tokens=2000)
        m = re.search(r"\[.*\]", raw, re.DOTALL)
        data = json.loads(m.group(0)) if m else json.loads(raw)
    except Exception:
        return [Group(name="综合研究", lit_ids=[p.lit_id for p in papers])]

    valid_ids = {p.lit_id for p in papers}
    covered: set[str] = set()
    groups: list[Group] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        theme = item.get("theme")
        ids = [i for i in item.get("lit_ids", []) if i in valid_ids]
        if not theme or not ids:
            continue
        covered.update(ids)
        groups.append(Group(name=theme, lit_ids=ids))

    if not groups:
        return [Group(name="综合研究", lit_ids=[p.lit_id for p in papers])]

    rest = [p.lit_id for p in papers if p.lit_id not in covered]
    if rest:
        groups.append(Group(name="其他", lit_ids=rest))
    return groups


def classify(papers: list[Paper], topic: str, mode: str) -> list[Group]:
    """mode ∈ {"locale", "theme"}"""
    if mode == "locale":
        return classify_by_locale(papers)
    if mode == "theme":
        return classify_by_theme(papers, topic)
    raise ValueError(f"unknown classify mode: {mode}")
