"""RetrievalController:不靠 LLM 的循环执行器。

输入是 LLM 已经规划好的多条检索式字符串 + 默认循环配置,这里只负责:
1. 每条检索式独立翻页(源内检索式之间可并发,按源限速配置并发数)
2. 跨条 / 跨源去重汇入 PaperPool
3. (可选) 雪球 + 摘要回填
"""
from __future__ import annotations

import asyncio
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FutureTimeoutError
from dataclasses import dataclass

from retrieval.pool import PaperPool
from retrieval.sources.base import AcademicSource
from retrieval.sources.openalex import OpenAlexRateLimitError

log = logging.getLogger(__name__)

# 单页请求最大执行时间(秒);超时则放弃该页,避免单源网络挂死拖垮整个任务
PAGE_FETCH_TIMEOUT = 45.0

# 默认循环配置(替代原 SearchIntent.loop, 不再由 LLM 配置)
DEFAULT_LOOP = {
    "per_page": 50,
    "max_pages_per_source": 20,
    "max_results_per_source": 5000,
    "stop_on_consecutive_empty": 3,
    "source_concurrency": 3,
    # 源内「检索式」并发数:int=全源统一;dict=按源名配置。
    # OpenAlex 限速宽松(~10 req/s)开 4;PubMed 每页 2 次请求且 NCBI
    # 无 key 限 3 req/s,只开 2,避免触发服务端限流。
    "query_concurrency": {"openalex": 4, "pubmed": 2},
}

# 默认雪球配置(替代原 SearchIntent.snowball)
DEFAULT_SNOWBALL = {
    "enabled": False,
    "forward_depth": 0,
    "backward_depth": 1,
    "max_seeds": 100,
    "max_results": 500,
}


class TaskCancelledError(Exception):
    """任务被用户手动停止(通过 stop_event 触发)。"""


# === 控制器 ===

@dataclass
class RetrievalProgress:
    """每个控制器 step 上报给任务层的进度事件。"""

    stage: str           # planning / fetching / fetching_source / snowballing / filling / done
    source: str = ""     # 当前在哪个源
    page: int = 0        # 当前页码
    added: int = 0       # 本轮新增
    total: int = 0       # 池总量
    message: str = ""     # 人类可读


ProgressCallback = "callable[[RetrievalProgress], None]"


class RetrievalController:
    """执行一次完整检索。

    输入:queries (3 条字符串列表) + sources (检索源列表)
    输出:PaperPool(去重合并后的论文池)
    """

    def __init__(self, queries: list[str] | None = None,
                 sources: list[AcademicSource] | None = None,
                 *,
                 loop_cfg: dict | None = None,
                 snow: dict | None = None,
                 on_progress: "callable | None" = None,
                 stop_event: "threading.Event | None" = None,
                 pool: PaperPool | None = None,
                 queries_per_source: dict[str, list[str]] | None = None,
                 year_start: int | None = None,
                 year_end: int | None = None):
        """构造检索控制器。

        两种 queries 传法(互斥):
        - queries (list[str]): 所有源共用一套检索式;
        - queries_per_source (dict[str, list[str]]): 按源 name 分发,
          例 {"openalex": [...], "pubmed": [...]}; 源名不在 dict 里则用 [].
        year_start/year_end: 任务级发表年份窗口,透传给支持该参数的源
          (openalex/pubmed); 缺省时各源用自己的默认窗口。
        """
        if queries is None and queries_per_source is None:
            raise ValueError("必须传 queries 或 queries_per_source")
        if queries is not None and queries_per_source is not None:
            raise ValueError("queries 与 queries_per_source 不能同时传")
        self.queries = list(queries) if queries is not None else None
        self.queries_per_source = dict(queries_per_source) if queries_per_source is not None else None
        self.sources = list(sources or [])
        self.loop_cfg = {**DEFAULT_LOOP, **(loop_cfg or {})}
        self.snow = {**DEFAULT_SNOWBALL, **(snow or {})}
        self.pool = pool or PaperPool()
        self.on_progress = on_progress or (lambda e: None)
        self.stop_event = stop_event
        # v9.6:任务级年份窗口(#9 此前 API 收了但查询构造被硬编码「近 5 年」)
        self.year_start = year_start
        self.year_end = year_end

    def _raise_if_stopped(self) -> None:
        if self.stop_event is not None and self.stop_event.is_set():
            raise TaskCancelledError("用户已手动停止")

    # === 主入口 ===

    def run_main(self) -> PaperPool:
        """主循环(同步,每源 × 每检索式 独立翻页)。返回填好 paper 的 pool。"""
        self._raise_if_stopped()
        total_queries = (
            sum(len(v or []) for v in self.queries_per_source.values())
            if self.queries_per_source is not None
            else len(self.queries or [])
        )
        self._emit("fetching", message=f"开始主循环,共 {len(self.sources)} 个源并发 × {total_queries} 条检索式")
        max_workers = max(1, min(len(self.sources), int(self.loop_cfg.get("source_concurrency", len(self.sources)) or 1)))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(self._fetch_one_source, src): src for src in self.sources}
            for future in as_completed(futures):
                src = futures[future]
                try:
                    future.result()
                except TaskCancelledError:
                    raise
                except Exception as exc:
                    self._emit("fetching_source", source=src.name,
                               message=f"该源执行异常,已跳过: {exc}")
                    log.exception("%s 源执行异常", src.name)
        self._emit("fetching_done", total=len(self.pool),
                   message=f"主循环完成,共 {len(self.pool)} 篇")
        return self.pool

    async def run_async(self) -> PaperPool:
        """异步主入口:主循环 + 雪球 + 摘要回填一气呵成。"""
        self.run_main()
        if self.snow["enabled"]:
            await self.run_snowball()
        await self.fill_abstracts()
        return self.pool

    # === 主循环 ===

    def _fetch_one_source(self, src: AcademicSource) -> None:
        """单源检索:检索式之间并发(按源配置并发数),每条检索式独立翻页。

        queries_per_source 优先;否则使用 queries(共享给所有源)。
        源不支持 build_sub_query(query_string) 时退化为单条(取 queries[0])。
        并发数取 loop_cfg["query_concurrency"]:int 全源统一;dict 按
        src.name 取(pubmed 每页 2 次请求且 NCBI 限速紧,并发应更小)。
        并发下 max_results_per_source 可能小幅超出(多个页同时入库),
        但 pool.add 去重保证不重复,超出量 ≤ (并发数-1)×per_page,可接受。
        """
        if self.queries_per_source is not None:
            queries_for_src = self.queries_per_source.get(src.name) or []
        elif self.queries:
            queries_for_src = self.queries
        else:
            queries_for_src = []
        if not queries_for_src:
            queries_for_src = [""]

        qc_cfg = self.loop_cfg.get("query_concurrency", 2)
        if isinstance(qc_cfg, dict):
            max_q_workers = int(qc_cfg.get(src.name, 2) or 1)
        else:
            max_q_workers = int(qc_cfg or 1)
        max_q_workers = max(1, min(max_q_workers, len(queries_for_src)))

        if max_q_workers <= 1:
            for qi, query_string in enumerate(queries_for_src):
                self._fetch_one_query(src, qi, queries_for_src, query_string)
            return

        self._emit("fetching_source", source=src.name,
                   message=f"{src.name} 开启 {max_q_workers} 路检索式并发")
        with ThreadPoolExecutor(max_workers=max_q_workers) as ex:
            futs = {
                ex.submit(self._fetch_one_query, src, qi, queries_for_src, q): qi
                for qi, q in enumerate(queries_for_src)
            }
            for future in as_completed(futs):
                qi = futs[future]
                try:
                    future.result()
                except TaskCancelledError:
                    raise
                except Exception as exc:
                    self._emit("fetching_source", source=src.name,
                               message=f"第 {qi + 1}/{len(queries_for_src)} 条检索式异常,已跳过: {exc}")
                    log.warning("%s 第 %s 条检索式异常: %s", src.name, qi + 1, exc)

    def _fetch_one_query(self, src: AcademicSource, qi: int,
                         queries_for_src: list[str], query_string: str) -> None:
        """单条检索式独立翻页(线程内串行翻页,结果汇入 PaperPool 自动去重)。"""
        self._raise_if_stopped()
        if len(self.pool) >= self.loop_cfg["max_results_per_source"]:
            return
        try:
            # v9.6:透传任务级年份窗口(#9);不支持该参数的源按鸭子类型忽略
            query = src.build_sub_query(query_string,
                                        year_start=self.year_start,
                                        year_end=self.year_end)
        except Exception as e:
            self._emit("fetching_source", source=src.name,
                       message=f"第 {qi + 1}/{len(queries_for_src)} 条检索式构建失败,已跳过: {e}")
            log.warning("%s 第 %s 条检索式构建失败: %s", src.name, qi + 1, e)
            return
        self._emit("fetching_source", source=src.name,
                   message=f"第 {qi + 1}/{len(queries_for_src)} 条检索式: {query_string[:60]}")

        empty_streak = 0
        # 该条检索式本轮累加入库数(paper_hit 用此展示,与前端 bar.saved 一致)
        src_added_total = 0
        for page in range(1, self.loop_cfg["max_pages_per_source"] + 1):
            if len(self.pool) >= self.loop_cfg["max_results_per_source"]:
                self._emit("fetching_source", source=src.name, page=page,
                           message="达到 max_results_per_source,提前停止")
                return
            self._raise_if_stopped()
            try:
                resp = self._execute_with_timeout(
                    src, query, page, self.loop_cfg["per_page"]
                )
            except FutureTimeoutError:
                # 单页超时属局部挫折:跳过该条检索式,其余继续;成败由池产出判定
                self._emit("fetching_source", source=src.name, page=page,
                           message=f"单页请求超时({PAGE_FETCH_TIMEOUT}s),已跳过该条检索式")
                log.warning("%s 单页请求超时,已跳过第 %s 条", src.name, qi + 1)
                break
            except OpenAlexRateLimitError as e:
                # 限流区别于「零结果」:明确提示用户是服务端限流。
                # 仅中止当前检索式;其余检索式首屏命中 429 后同样自停,额外请求量有限。
                self._emit("fetching_source", source=src.name, page=page,
                           message=f"{src.name} 限流(429): {e}")
                log.warning("%s 限流(429): %s", src.name, e)
                return
            except Exception as e:
                # 单源翻页异常 → 只跳过当前检索式,其余检索式/其他源继续
                self._emit("fetching_source", source=src.name, page=page,
                           message=f"{src.name} 翻页失败,已跳过(其他源继续): {e}")
                log.warning("%s 翻页失败: %s", src.name, e)
                return

            # 如果整页加完会超 max_results,只取前 N 个
            remaining = self.loop_cfg["max_results_per_source"] - len(self.pool)
            if remaining <= 0:
                self._emit("fetching_source", source=src.name, page=page,
                           message="达到 max_results_per_source,提前停止")
                return
            truncated = resp.papers[:remaining] if remaining < len(resp.papers) else resp.papers
            new_papers = self.pool.add(truncated, source=src.name)
            src_added_total += len(new_papers)
            self._emit("fetching_source", source=src.name, page=page,
                       added=len(new_papers), total=src_added_total,
                       message=f"命中 {resp.total} 篇")
            # 对称中文:逐条输出本次新增文献的题录
            for np in new_papers:
                authors = np.authors or []
                author_str = ", ".join(str(a) for a in authors[:3])
                if len(authors) > 3:
                    author_str += " 等"
                bib = f"《{np.title or '(无题名)'}》"
                if author_str:
                    bib += f" / {author_str}"
                if np.journal:
                    bib += f" / {np.journal}"
                if np.year:
                    bib += f", {np.year}"
                self._emit("paper_hit", source=src.name, page=page,
                           added=1, total=src_added_total,
                           message=f"[命中] {src_added_total}/{self.loop_cfg['max_results_per_source']} | {bib}")
            if len(new_papers) == 0:
                empty_streak += 1
                if empty_streak >= self.loop_cfg["stop_on_consecutive_empty"]:
                    self._emit("fetching_source", source=src.name, page=page,
                               message=f"连续 {empty_streak} 页 0 新结果,跳到下一条检索式")
                    break
            else:
                empty_streak = 0
            if not resp.has_next:
                break  # 本条检索式翻完

    # === 内部工具 ===

    def _execute_with_timeout(self, src: AcademicSource, query: dict,
                              page: int, per_page: int):
        """给单页请求加超时保护,防止网络挂死拖垮整个任务。"""
        pool = ThreadPoolExecutor(max_workers=1)
        fut = pool.submit(src.execute, query, page, per_page)
        try:
            return fut.result(timeout=PAGE_FETCH_TIMEOUT)
        except FutureTimeoutError:
            # 超时后不能再让 with 等待卡死的 worker;否则 45s 保护会退化成 90s+
            pool.shutdown(wait=False, cancel_futures=True)
            raise
        except Exception:
            pool.shutdown(wait=False, cancel_futures=True)
            raise

    # === 雪球 ===

    async def run_snowball(self) -> int:
        """对池内前 max_seeds 篇 paper 跑后向引用检索(确定性,无 LLM)。

        雪球失败不应让主流程失败——它只是个补充,主流程已入库的论文照常使用。
        """
        seeds = self.pool.papers[: self.snow["max_seeds"]]
        self._emit("snowballing", total=len(seeds),
                   message=f"开始雪球,种子 {len(seeds)} 篇,后向 {self.snow['backward_depth']} 层")
        added_total = 0
        for src in self.sources:
            if not hasattr(src, "fetch_references"):
                continue
            for seed in seeds:
                self._raise_if_stopped()
                try:
                    refs = src.fetch_references(seed, depth=self.snow["backward_depth"])
                    added = self.pool.add(refs, source=src.name)
                    added_total += len(added)
                except Exception as e:
                    self._emit("snowballing", source=src.name,
                               message=f"种子 {seed.lit_id} 引用拉取失败,已跳过: {e}")
                    log.warning("雪球 %s/%s 失败: %s", src.name, seed.lit_id, e)
        self._emit("snowballing_done", added=added_total,
                   message=f"雪球完成,新增 {added_total} 篇")
        return added_total

    # === 异步摘要回填 ===

    async def fill_abstracts(self) -> None:
        """并发对各源缺失摘要的 paper 回填。

        由 pool.fill_missing_async 统一负责:按源过滤候选、把同步 HTTP
        下放线程池真正并发、限制单源上限,并逐条回调进度。
        """
        self._emit("filling", message="开始异步摘要回填")
        for src in self.sources:
            if not hasattr(src, "fetch_abstract_if_missing"):
                continue
            self._raise_if_stopped()

            def _report(done: int, total: int, _name: str = src.name) -> None:
                # 每 25 条上报一次 + 最后一条必报,避免事件风暴又不让 UI 静默
                if done % 25 and done != total:
                    return
                self._emit("filling", source=_name, added=done, total=total,
                           message=f"{_name} 摘要回填 {done}/{total}")

            try:
                await self.pool.fill_missing_async(src, on_progress=_report)
            except Exception as e:
                self._emit("filling_warning", source=src.name,
                           message=f"回填 {src.name} 摘要失败(已忽略): {e}")
                log.warning("回填 %s 摘要失败: %s", src.name, e)
        self._emit("filling_done", message=f"回填完成,池内 {len(self.pool)} 篇")

    # === 工具 ===

    def _emit(self, stage: str, **kw) -> None:
        evt = RetrievalProgress(stage=stage, **kw)
        try:
            self.on_progress(evt)
        except Exception as e:
            log.warning("进度回调失败: %s", e)


__all__ = [
    "RetrievalController",
    "RetrievalProgress",
    "TaskCancelledError",
]