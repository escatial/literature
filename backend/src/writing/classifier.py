"""综述分类器:按国内外 / 按主题 对文献分组。

使用 prompts/literature-review.md 模板中的 `classify` 段作为 system prompt。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from prompts.service import parse_llm_json, render
from src.llm.client import messages_create
from src.retrieval.types import Paper, Source
from src.writing.settings import LOCALE_GROUP_DOMESTIC, LOCALE_GROUP_FOREIGN


@dataclass
class Group:
    name: str
    lit_ids: list[str] = field(default_factory=list)


def classify_by_locale(papers: list[Paper]) -> list[Group]:
    """国内外分类:中文导入(USER_IMPORTED)为国内,其余为国外。"""
    domestic = [p.lit_id for p in papers if p.source == Source.USER_IMPORTED]
    foreign = [p.lit_id for p in papers if p.source != Source.USER_IMPORTED]
    groups: list[Group] = []
    if domestic:
        groups.append(Group(name=LOCALE_GROUP_DOMESTIC, lit_ids=domestic))
    if foreign:
        groups.append(Group(name=LOCALE_GROUP_FOREIGN, lit_ids=foreign))
    return groups


def classify_by_theme(papers: list[Paper], topic: str) -> list[Group]:
    """主题分类:LLM 将文献归入若干主题。"""
    if not papers:
        return []

    catalog = "\n".join(
        f"- {p.lit_id} | {p.title} | {p.journal or 'N/A'} | {p.year or 'N/A'} | source={p.source.value}"
        for p in papers
    )

    system = render(
        "literature-review:classify",
        topic=topic,
        classify_mode="theme",
        papers_catalog=catalog,
    )
    user = f"研究主题:{topic}\n\n文献清单:\n{catalog}"

    try:
        raw = messages_create(
            system=system, user=user, max_tokens=2000,
            response_format={"type": "json_object"},
        )
        data = parse_llm_json(raw)
    except Exception:
        return [Group(name=topic, lit_ids=[p.lit_id for p in papers])]

    # 支持 {groups: [...]} 或 直接 [...]
    if isinstance(data, dict) and "groups" in data:
        data = data["groups"]

    valid_ids = {p.lit_id for p in papers}
    covered: set[str] = set()
    groups: list[Group] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        name = item.get("name") or item.get("theme")
        ids = [i for i in item.get("lit_ids", []) if i in valid_ids]
        if not name or not ids:
            continue
        covered.update(ids)
        groups.append(Group(name=name, lit_ids=ids))

    if not groups:
        return [Group(name=topic, lit_ids=[p.lit_id for p in papers])]

    rest = [p.lit_id for p in papers if p.lit_id not in covered]
    if rest:
        groups[-1].lit_ids.extend(rest)
    return groups


def classify(papers: list[Paper], topic: str, mode: str) -> list[Group]:
    if mode == "locale":
        return classify_by_locale(papers)
    if mode == "theme":
        return classify_by_theme(papers, topic)
    raise ValueError(f"unknown classify mode: {mode}")
