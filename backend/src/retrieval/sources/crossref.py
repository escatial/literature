"""Crossref 数据源实现(方案 §1 "英文文献以 Crossref/OpenAlex/出版社页面
和 Google Scholar 可检索性为交叉来源" 中的 Crossref 部分)。

- 走 https://api.crossref.org/works
- 失败 3 次指数退避重试
- 不依赖任何 API key(礼貌池 mailto 可选)
- 默认过滤:journal article / proceedings article;近 5 年
- 翻页用 offset + rows
- 优先级低于 OpenAlex,作为元数据交叉核验源

与 OpenAlex 的关系:
- OpenAlex 已覆盖 95% 英文文献,且元数据更现代;
- Crossref 作为元数据兜底(DOI 解析、卷期/页码等出版商权威数据),
  当 OpenAlex 元数据缺失或冲突时,以 Crossref 为准。

官方规范: https://github.com/CrossRef/rest-api-doc
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

import httpx

from retrieval.paper_identity import build_lit_id
from retrieval.sources.base import AcademicSource, SourcePage
from retrieval.types import Paper, Source

log = logging.getLogger(__name__)

CROSSREF_BASE = "https://api.crossref.org/works"
DEFAULT_MAILTO = os.getenv("CROSSREF_MAILTO", "lit-review-agent@example.com")
DEFAULT_YEAR_BACK = 5
# Crossref type 枚举里的期刊论文:journal-article + proceedings-article
DEFAULT_TYPES = ["journal-article", "proceedings-article"]


class CrossrefSource:
    """Crossref 期刊元数据源。"""

    name = "crossref"

    def __init__(self, timeout: float = 30.0, mailto: str | None = None):
        self.timeout = timeout
        self.mailto = mailto or DEFAULT_MAILTO

    # === AcademicSource 协议 ===

    def build_sub_query(self, query_string: str) -> dict:
        """把 LLM 输出的检索式字符串翻译成 Crossref query 参数。

        Crossref 不像 OpenAlex 有完整 boolean 语法,这里直接把 query_string
        当作关键词串(空格分隔)塞到 query.bibliographic;具体年份/类型由
        过滤器参数补齐。
        """
        import datetime as _dt

        year = _dt.datetime.now().year
        return {
            "query": {"bibliographic": query_string.strip()},
            "filter": {
                "from-pub-date": f"{year - DEFAULT_YEAR_BACK}-01-01",
                "until-pub-date": f"{year}-12-31",
                "type": DEFAULT_TYPES,
            },
            "rows": 50,
            "offset": 0,
        }

    def execute(self, query: dict, page: int, per_page: int) -> SourcePage:
        """执行一页查询。

        query: build_sub_query 输出的字典
        page: 1-based 页码
        per_page: 每页条数(<= 50)
        """
        params = dict(query)
        params["rows"] = min(per_page, 50)
        params["offset"] = (page - 1) * params["rows"]

        last_err: Exception | None = None
        for attempt in range(3):
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    resp = client.get(
                        CROSSREF_BASE,
                        params=params,
                        headers={"User-Agent": f"LitReviewAgent/1.0 (mailto:{self.mailto})"},
                    )
                if resp.status_code == 429:
                    backoff = 2 ** attempt
                    log.warning("Crossref 429, 退避 %ss (attempt=%d)", backoff, attempt + 1)
                    time.sleep(backoff)
                    last_err = RuntimeError(f"Crossref 429 attempt {attempt + 1}")
                    continue
                if resp.status_code >= 400:
                    last_err = RuntimeError(f"Crossref HTTP {resp.status_code}")
                    time.sleep(1)
                    continue
                data = resp.json()
                break
            except (httpx.HTTPError, ValueError) as e:
                last_err = e
                time.sleep(1)
        else:
            raise last_err or RuntimeError("Crossref 检索失败")

        items = (data.get("message") or {}).get("items") or []
        papers = [_item_to_paper(it) for it in items if _is_acceptable(it)]
        total_raw = (data.get("message") or {}).get("total-results") or 0
        has_next = (page * params["rows"]) < int(total_raw)

        return SourcePage(
            papers=papers,
            total=int(total_raw),
            has_next=has_next,
            page=page,
            raw_query={"query": query.get("query"), "filter": query.get("filter"), "offset": params["offset"], "rows": params["rows"]},
        )

    def fetch_abstract_if_missing(self, paper: Paper) -> Paper | None:
        """Crossref 不提供摘要字段,无法回填。"""
        return None

    def fetch_references(self, paper: Paper, depth: int = 1) -> list[Paper]:
        """Crossref 的 reference 字段是 DOI 列表(非完整元数据),雪球能力有限,
        这里保持空实现,真实引用回填交给 OpenAlex。
        """
        return []

    def health_check(self) -> bool:
        try:
            with httpx.Client(timeout=5.0) as client:
                resp = client.get(
                    CROSSREF_BASE,
                    params={"query": {"bibliographic": "test"}, "rows": 1},
                )
            return resp.status_code < 400
        except Exception:
            return False


def _is_acceptable(item: dict) -> bool:
    """过滤:必须是期刊论文(已在 filter.type 限过,这里二次校验以防 API 退化)。"""
    t = (item.get("type") or "").lower()
    return t in DEFAULT_TYPES


def _item_to_paper(item: dict) -> Paper:
    """把 Crossref item 转 Paper。"""
    title_list = item.get("title") or []
    title = (title_list[0] if title_list else "").strip()

    authors: list[str] = []
    for a in item.get("author") or []:
        family = (a.get("family") or "").strip()
        given = (a.get("given") or "").strip()
        if family and given:
            authors.append(f"{family}, {given}")
        elif family:
            authors.append(family)
        elif given:
            authors.append(given)
        # 机构作者:Crossref 用 "name" 字段
        if not authors and a.get("name"):
            authors.append(a["name"])

    # 期刊名:优先 container-title,其次 short-container-title
    container = item.get("container-title") or []
    journal = (container[0] if container else "").strip()
    if not journal:
        short = item.get("short-container-title") or []
        journal = (short[0] if short else "").strip()

    # 年份:issued.date-parts[0][0]
    year = 0
    issued = item.get("issued") or item.get("published-print") or item.get("published-online")
    if issued and isinstance(issued.get("date-parts"), list) and issued["date-parts"]:
        first = issued["date-parts"][0]
        if isinstance(first, list) and first:
            try:
                year = int(first[0])
            except (TypeError, ValueError):
                year = 0

    volume = (item.get("volume") or "").strip() or None
    issue = (item.get("issue") or "").strip() or None
    pages = (item.get("page") or "").strip() or None

    doi = (item.get("DOI") or "").strip() or None
    source_url = item.get("URL") or (f"https://doi.org/{doi}" if doi else "")

    cited_by = item.get("is-referenced-by-count") or 0
    try:
        cited_by = int(cited_by)
    except (TypeError, ValueError):
        cited_by = 0

    lit_id = build_lit_id(
        source=Source.CROSSREF.value,
        title=title,
        authors=authors,
        year=year,
        doi=doi,
    )

    return Paper(
        lit_id=lit_id,
        source=Source.CROSSREF,
        title=title,
        authors=authors,
        journal=journal,
        year=year,
        volume=volume,
        issue=issue,
        pages=pages,
        doi=doi,
        source_url=source_url,
        cited_by_count=cited_by,
        abstract=None,  # Crossref 不提供
    )


__all__ = ["CrossrefSource"]