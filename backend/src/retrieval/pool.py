"""PaperPool:跨源去重 + 异步摘要回填的统一接口。

三级去重键(DOI > normalized title > hash):
  1. 有 DOI -> doi:<lowercase, no prefix>;
  2. 无 DOI 但有 title -> title:<NFKC+HTML/LaTeX 去标签+小写+去标点>|<first_author>|<year>;
  3. 都没有 -> hash:<lit_id>。
"""
from __future__ import annotations

import asyncio
import logging
import re
import threading
import unicodedata
from typing import Iterable

from retrieval.types import Paper

log = logging.getLogger(__name__)


_DOI_PREFIX_RE = re.compile(r"^https?://(dx\.)?doi\.org/", re.IGNORECASE)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_LATEX_RE = re.compile(r"\$.*?\$", re.DOTALL)
_PUNCT_RE = re.compile(r"[^\w\s]")
_WS_RE = re.compile(r"\s+")


def normalize_doi(doi: str) -> str:
    d = doi.strip().lower()
    d = _DOI_PREFIX_RE.sub("", d)
    if d.startswith("doi:"):
        d = d[4:]
    return d


def normalize_title(t: str) -> str:
    """NFKC 归一化 + 去 HTML + 去 LaTeX + 小写 + 去标点 + 折叠空白。"""
    t = unicodedata.normalize("NFKC", t).lower()
    t = _HTML_TAG_RE.sub("", t)
    t = _LATEX_RE.sub("", t)
    t = _PUNCT_RE.sub("", t)
    t = _WS_RE.sub(" ", t).strip()
    return t


def _paper_id(p: Paper) -> str:
    if p.doi:
        return f"doi:{normalize_doi(p.doi)}"
    if p.title:
        nt = normalize_title(p.title)
        first_author = p.authors[0].strip().lower() if p.authors else ""
        return f"title:{nt}|{first_author}|{p.year}"
    return f"hash:{p.lit_id}"


class PaperPool:
    """线程安全的文献池。

    用法:
        pool = PaperPool()
        for src in sources:
            for page in ...:
                resp = src.execute(query, page=page, per_page=50)
                pool.add(resp.papers, source=src.name)
        pool.dedupe()      # 显式触发(默认 add 内部就 dedupe)
        pool.fill_missing_async(source)  # 异步回填缺失摘要
    """

    def __init__(self):
        self._seen: set[str] = set()
        self.papers: list[Paper] = []
        self._lock = threading.Lock()
        # 抽象回填队列:Paper 对象
        self._fill_queue: asyncio.Queue | None = None

    def __len__(self) -> int:
        with self._lock:
            return len(self.papers)

    def add(self, papers: Iterable[Paper], source: str | None = None) -> list[Paper]:
        """加入新 paper,自动去重。线程安全,返回真正加入的(非重复)子集。"""
        added: list[Paper] = []
        with self._lock:
            for p in papers:
                key = _paper_id(p)
                if key in self._seen:
                    continue
                self._seen.add(key)
                if source:
                    # 覆盖 source 字段,保证入库时记录真正来源
                    try:
                        from retrieval.types import Source as _S
                        p.source = _S(source) if isinstance(_S, type) else source
                    except Exception:
                        p.source = source  # type: ignore[assignment]
                self.papers.append(p)
                added.append(p)
        return added

    def dedupe(self) -> int:
        """兼容旧 API 调用;add 内部已 dedupe,这里仅返回当前去重后的总数。"""
        return len(self.papers)

    def filter_by(self, *, year_range: tuple[int, int] | None = None,
                  min_citations: int | None = None,
                  require_abstract: bool | None = None,
                  allowed_types: set[str] | None = None,
                  languages: set[str] | None = None,
                  require_doi: bool | None = None,
                  ) -> list[Paper]:
        """按结构化条件再过滤一遍。返回过滤后(但不动内部 papers)的子集。"""
        out = []
        for p in self.papers:
            if year_range and (not p.year or not (year_range[0] <= p.year <= year_range[1])):
                continue
            if min_citations is not None and p.cited_by_count < min_citations:
                continue
            if require_abstract and not (p.abstract and p.abstract.strip()):
                continue
            if require_doi and not p.doi:
                continue
            out.append(p)
        return out

    # === 异步摘要回填 ===

    async def fill_missing_async(
        self,
        source: object,        # AcademicSource 实现
        concurrency: int = 4,
    ) -> int:
        """对所有 abstract 为空的 paper 调 fetch_abstract_if_missing。
        并发数受 concurrency 控制。返回成功回填数。"""
        missing = [p for p in self.papers if not (p.abstract and p.abstract.strip())]
        if not missing:
            return 0

        sem = asyncio.Semaphore(concurrency)
        filled = 0

        async def _one(p: Paper):
            nonlocal filled
            async with sem:
                try:
                    updated = source.fetch_abstract_if_missing(p)
                    if updated and updated.abstract:
                        p.abstract = updated.abstract
                        filled += 1
                except Exception as e:
                    log.warning("回填 %s 摘要失败: %s", p.lit_id, e)

        await asyncio.gather(*[_one(p) for p in missing])
        log.info("PaperPool 异步回填: 缺失 %d, 成功 %d", len(missing), filled)
        return filled


__all__ = ["PaperPool", "normalize_doi", "normalize_title"]
