"""检索结果过滤。纯函数,无副作用,无 LLM。

不做任何字段构造,只对 Paper 对象做过滤和去重。
"""
from __future__ import annotations

from .types import Paper


def by_year(papers: list[Paper], year_range: tuple[int, int]) -> list[Paper]:
    """按出版年份范围过滤。"""
    lo, hi = year_range
    return [p for p in papers if p.year and lo <= p.year <= hi]


def by_min_citations(papers: list[Paper], min_cited: int) -> list[Paper]:
    """按最低被引次数过滤。"""
    if min_cited <= 0:
        return papers
    return [p for p in papers if p.cited_by_count >= min_cited]


def by_journal_level(papers: list[Paper], levels: set[str]) -> list[Paper]:
    """按期刊层级过滤(SCI/SSCI/AHCI/ESCI)。

    levels 是集合,任一命中即保留。
    空集合表示不过滤。
    """
    if not levels:
        return papers
    return [p for p in papers if p.journal_level in levels]


def deduplicate(papers: list[Paper]) -> list[Paper]:
    """基于 DOI + (title|first_author|year) 复合键去重,保留第一篇。"""
    seen: set[tuple[str, str]] = set()
    out: list[Paper] = []
    for p in papers:
        doi_key = (p.doi or "").lower()
        title_key = (
            (p.title or "").strip().lower()
            + "|"
            + (p.authors[0] if p.authors else "").strip().lower()
            + "|"
            + str(p.year)
        )
        if (doi_key, title_key) in seen:
            continue
        seen.add((doi_key, title_key))
        out.append(p)
    return out
