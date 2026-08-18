"""OpenAlex 适配器(带指数退避重试)。"""
from __future__ import annotations

import hashlib
import logging
import os
# 导包

import httpx

from .types import Paper, Source

log = logging.getLogger(__name__)

# 默认 mailto,优先读环境变量 OPENALEX_MAILTO
DEFAULT_MAILTO = os.getenv("OPENALEX_MAILTO", "your-email@example.com")


def _rebuild_abstract(inverted: dict | None) -> str | None:
    """OpenAlex abstract_inverted_index 反向索引还原(平台字段反序列化,不是生成)。"""
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
    """内部唯一 ID,SHA256(title|doi)[:16]。"""
    raw = f"{title or ''}|{doi or ''}"
    return "lit_" + hashlib.sha256(raw.encode()).hexdigest()[:16]


class OpenAlexAdapter:
    BASE = "https://api.openalex.org/works"

    def __init__(self, mailto: str | None = None, timeout: float = 90.0):
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
        return self._get_with_retry(params)

    def _get_with_retry(self, params: dict, max_retries: int = 3) -> list[Paper]:
        """指数退避重试,应对 OpenAlex 偶尔慢握手。"""
        import time as _time

        last_err: Exception | None = None
        for attempt in range(max_retries):
            try:
                log.info("OpenAlex 请求 attempt=%d params=%s", attempt + 1, {k: v for k, v in params.items() if k != "mailto"})
                with httpx.Client(timeout=self.timeout, trust_env=False, transport=httpx.HTTPTransport(local_address="0.0.0.0")) as client:
                    resp = client.get(self.BASE, params=params)
                    resp.raise_for_status()
                log.info("OpenAlex 成功 attempt=%d", attempt + 1)
                return [self._parse(w) for w in resp.json().get("results", [])]
            except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ConnectError) as e:
                last_err = e
                wait = 2 ** attempt  # 1, 2, 4 秒
                log.warning("OpenAlex attempt=%d 失败: %s; %d 秒后重试", attempt + 1, e, wait)
                _time.sleep(wait)
            except httpx.HTTPStatusError as e:
                log.warning("OpenAlex HTTP %s: %s", e.response.status_code, e)
                return []
        log.error("OpenAlex 全部重试失败: %s", last_err)
        return []

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
        # 注意:数字 0 会被 if 判定为 False,因此需用 is not None 严格判定
        if first and last:
            pages = f"{first}-{last}"
        elif first:
            pages = str(first)
        elif last:
            pages = str(last)
        else:
            pages = None

        primary = w.get("primary_location") or {}
        source_loc = primary.get("source") or {}
        journal = source_loc.get("display_name") or ""

        openalex_id = str(w.get("id") or "").rstrip("/").rsplit("/", 1)[-1]
        lit_id = f"lit_openalex_{openalex_id.lower()}" if openalex_id else _make_lit_id(title, doi)
        source_url = f"https://openalex.org/{openalex_id}" if openalex_id else ""
        return Paper(
            lit_id=lit_id,
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
            source_url=source_url,
            cited_by_count=w.get("cited_by_count") or 0,
        )
