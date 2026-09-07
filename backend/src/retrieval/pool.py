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
from typing import Callable, Iterable

from retrieval.types import Paper

log = logging.getLogger(__name__)

# 单源摘要回填上限。综述最终只引用 70-90 篇,没必要为 4900 篇逐条 efetch:
# 每条 0.3-1s 的同步 HTTP,全量回填会让任务在「回填」阶段静默阻塞 40 分钟以上。
DEFAULT_FILL_LIMIT = 400


_DOI_PREFIX_RE = re.compile(r"^https?://(dx\.)?doi\.org/", re.IGNORECASE)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_LATEX_RE = re.compile(r"\$.*?\$", re.DOTALL)
_PUNCT_RE = re.compile(r"[^\w\s]")
_WS_RE = re.compile(r"\s+")


def _matches_source(paper: Paper, source_name: str) -> bool:
    """判断 paper 是否属于指定源。"""

    source_value = getattr(paper.source, "value", paper.source)
    if isinstance(source_value, str) and source_value:
        return source_value == source_name
    return paper.lit_id.startswith(f"lit_{source_name}_")


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
        limit: int | None = DEFAULT_FILL_LIMIT,
        on_progress: "Callable[[int, int], None] | None" = None,
    ) -> int:
        """对缺失摘要的 paper 调 fetch_abstract_if_missing,返回成功回填数。

        三个反卡死约束(修复「英文任务卡 99%」):
        1. 只回填「属于该源」的文献,不再把 OpenAlex 的条目丢给 PubMed 重试;
        2. fetch_abstract_if_missing 是同步阻塞 HTTP,必须 to_thread 下放线程池,
           否则事件循环被占满,Semaphore 形同虚设、全程串行;
        3. limit 限制单源回填上限,on_progress 逐条上报,UI 不再静默停在 99%。
        """
        source_name = str(getattr(source, "name", "") or "")
        candidates = [
            p for p in self.papers
            if not (p.abstract and p.abstract.strip())
            and (not source_name or _matches_source(p, source_name))
        ]
        if limit is not None and limit >= 0:
            candidates = candidates[:limit]
        total = len(candidates)
        if not total:
            return 0

        # 支持批量的源(如 PubMed efetch 单次可带 200 个 PMID)走批量通道:
        # 400 篇回填从「逐条 400 次 HTTP」压到「2 次 efetch」,是回填提速的主路径。
        batch_fn = getattr(source, "fetch_abstracts_batch", None)
        if callable(batch_fn):
            return await self._fill_via_batch(
                batch_fn, candidates, total, concurrency, on_progress
            )

        sem = asyncio.Semaphore(max(1, concurrency))
        filled = 0
        done = 0
        lock = asyncio.Lock()

        async def _one(p: Paper):
            nonlocal filled, done
            async with sem:
                try:
                    updated = await asyncio.to_thread(
                        source.fetch_abstract_if_missing, p
                    )
                    if updated and updated.abstract:
                        p.abstract = updated.abstract
                        async with lock:
                            filled += 1
                except Exception as e:
                    log.warning("回填 %s 摘要失败: %s", p.lit_id, e)
                async with lock:
                    done += 1
                    current = done
            if on_progress is not None:
                try:
                    on_progress(current, total)
                except Exception as e:
                    log.debug("回填进度回调失败: %s", e)

        await asyncio.gather(*[_one(p) for p in candidates])
        log.info(
            "PaperPool 异步回填 source=%s 候选 %d, 成功 %d",
            source_name or "?", total, filled,
        )
        return filled

    async def _fill_via_batch(
        self,
        batch_fn: "Callable[[list[Paper]], dict[str, str]]",
        candidates: list[Paper],
        total: int,
        concurrency: int,
        on_progress: "Callable[[int, int], None] | None",
    ) -> int:
        """批量回填:按 ~200 篇/块 to_thread 下放线程池并发执行,按 lit_id 回写摘要。

        批量请求本身就是大请求,信号量压到 2 即可——再高只会逼近 NCBI 限速
        (无 key 3 req/s),对耗时的边际收益几乎为零。
        """
        chunk_size = 200
        chunks = [
            candidates[i:i + chunk_size]
            for i in range(0, len(candidates), chunk_size)
        ]
        sem = asyncio.Semaphore(max(1, min(2, concurrency)))
        filled = 0
        done = 0
        lock = asyncio.Lock()

        async def _one_chunk(chunk: list[Paper]):
            nonlocal filled, done
            async with sem:
                current = 0
                try:
                    got = await asyncio.to_thread(batch_fn, chunk)
                    for p in chunk:
                        abstract = got.get(p.lit_id)
                        if abstract:
                            p.abstract = abstract
                    async with lock:
                        filled += sum(1 for p in chunk if got.get(p.lit_id))
                except Exception as e:
                    # 整块失败只丢这一块,不影响其余分块(块内已有 2 次重试)
                    log.warning("批量回填 %d 篇失败(跳过): %s", len(chunk), e)
                async with lock:
                    done += len(chunk)
                    current = done
            if on_progress is not None:
                try:
                    on_progress(current, total)
                except Exception as e:
                    log.debug("回填进度回调失败: %s", e)

        await asyncio.gather(*[_one_chunk(c) for c in chunks])
        log.info("PaperPool 批量回填候选 %d, 成功 %d", total, filled)
        return filled


__all__ = ["PaperPool", "normalize_doi", "normalize_title"]
