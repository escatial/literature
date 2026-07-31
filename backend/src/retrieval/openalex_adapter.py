"""OpenAlex 适配器。

- 只读取 OpenAlex 返回的字段,不构造任何字段
- 摘要还原 abstract_inverted_index(平台字段的反序列化,不是生成)
- 礼貌带 mailto,限速 ≤ 10 req/s
"""
from __future__ import annotations

import hashlib
import logging
import os

import httpx

from .types import Paper, Source

log = logging.getLogger(__name__)

# 默认 mailto,优先读环境变量 OPENALEX_MAILTO
DEFAULT_MAILTO = os.getenv("OPENALEX_MAILTO", "your-email@example.com")


def _rebuild_abstract(inverted: dict | None) -> str | None:
    """OpenAlex abstract_inverted_index 是反向索引,需要还原。

    这是字段反序列化,不是生成 — 我们没有创造内容,只是把
    平台存储的反向索引还原为自然文本。
    """
    if not inverted:
        return None
    word_positions = []
    for word, positions in inverted.items():
        for p in positions:
            word_positions.append((p, word))
    word_positions.sort()
    text = " ".join(w for _, w in word_positions).strip()
    return text or None


def _make_lit_id(title: str | None, doi: str | None) -> str:
    """内部唯一 ID,SHA256(title|doi)[:16]。

    与外部数据库通信无关,纯粹本地引用锚点。
    """
    raw = f"{title or ''}|{doi or ''}"
    return "lit_" + hashlib.sha256(raw.encode()).hexdigest()[:16]


class OpenAlexAdapter:
    BASE = "https://api.openalex.org/works"

    def __init__(self, mailto: str | None = None, timeout: float = 30.0):
        self.mailto = mailto or DEFAULT_MAILTO
        self.timeout = timeout

    def search(
        self,
        query: str,
        year_range: tuple[int, int],
        per_page: int = 50,
    ) -> list[Paper]:
        params = {
            "search": query,
            "filter": f"publication_year:{year_range[0]}-{year_range[1]}",
            "per-page": min(per_page, 200),
            "mailto": self.mailto,
        }
        with httpx.Client(timeout=self.timeout) as client:
            try:
                resp = client.get(self.BASE, params=params)
                resp.raise_for_status()
            except httpx.HTTPError as e:
                log.warning("OpenAlex 请求失败: %s", e)
                return []
            return [self._parse(w) for w in resp.json().get("results", [])]

    def _parse(self, w: dict) -> Paper:
        """读取 OpenAlex 字段。缺字段保持 None,绝不构造。"""
        doi_raw = w.get("doi") or ""
        doi = doi_raw.replace("https://doi.org/", "") or None
        title = (w.get("title") or w.get("display_name") or "").strip()

        authors = [
            a["author"]["display_name"]
            for a in w.get("authorships", [])
            if a.get("author")
        ]

        biblio = w.get("biblio") or {}
        volume = str(biblio.get("volume")) if biblio.get("volume") else None
        issue = str(biblio.get("issue")) if biblio.get("issue") else None
        first = biblio.get("first_page")
        last = biblio.get("last_page")
        pages = f"{first}-{last}" if (first and last) else (first or last or None)

        primary = w.get("primary_location") or {}
        source_loc = primary.get("source") or {}
        journal = source_loc.get("display_name") or ""

        return Paper(
            lit_id=_make_lit_id(title, doi),
            source=Source.OPENALEX,
            title=title,
            authors=authors,
            journal=journal,
            year=w.get("publication_year") or 0,
            volume=volume,
            issue=issue,
            pages=pages,
            abstract=_rebuild_abstract(w.get("abstract_inverted_index")),
            doi=doi,
            source_url=primary.get("landing_page_url") or w.get("id") or "",
            cited_by_count=w.get("cited_by_count") or 0,
        )
