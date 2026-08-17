"""LoopConfig + RetrievalController:不靠 LLM 的循环执行器。

核心约束:
- 每数据源独立翻页,各自停止(LoopConfig);
- 预检(preflight)用第 1 页结果数判断 query 是否过宽/过窄,反馈给上游重规划;
- 雪球(可选)在主流程完成后确定性执行,不调 LLM;
- 异步回填缺失摘要。
"""
from __future__ import annotations

import asyncio
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from typing import Iterable

from retrieval.intent import LoopConfig, SearchIntent, SnowballConfig, relax_intent
from retrieval.pool import PaperPool
from retrieval.sources.base import AcademicSource
from retrieval.sources.openalex import OpenAlexRateLimitError

log = logging.getLogger(__name__)

# 单页请求最大执行时间(秒);超时则放弃该页,避免单源网络挂死拖垮整个任务
PAGE_FETCH_TIMEOUT = 45.0


class TaskCancelledError(Exception):
    """任务被用户手动停止(通过 stop_event 触发)。"""


# === 预检机制 ===

@dataclass
class PreflightVerdict:
    ok: bool
    avg_total: int
    per_source_totals: dict[str, int]
    reason: str = ""


def preflight(intent: SearchIntent, sources: list[AcademicSource],
              per_page: int = 10) -> PreflightVerdict:
    """跑每个源第 1 页(per_page=10),看命中总数:
    - 任一源 < 10: query 太窄,建议放宽(返回 ok=False)
    - 平均 > 10000: query 太宽,建议加严
    - 否则 ok=True
    """
    totals: dict[str, int] = {}
    for src in sources:
        if not src.health_check():
            totals[src.name] = -1
            continue
        try:
            q = src.build_query(intent)
            resp = src.execute(q, page=1, per_page=per_page)
            totals[src.name] = resp.total
        except Exception as e:
            log.warning("preflight %s 失败: %s", src.name, e)
            totals[src.name] = -1

    valid = [t for t in totals.values() if t >= 0]
    if not valid:
        return PreflightVerdict(False, 0, totals, "所有数据源都不可用")
    avg = sum(valid) / len(valid)
    if min(valid) < 10:
        return PreflightVerdict(False, avg, totals, f"最窄源仅 {min(valid)} 篇,query 可能太严")
    if avg > 10000:
        return PreflightVerdict(False, avg, totals, f"平均 {avg:.0f} 篇,query 可能太宽")
    return PreflightVerdict(True, avg, totals)


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
    """执行一次完整检索:
        1. 预检(可选)
        2. 主循环:每个源按 LoopConfig 翻页
        3. 硬筛选(在 SearchIntent.filters 里)
        4. 雪球(可选)
        5. 异步摘要回填(可选)
    """

    def __init__(self, intent: SearchIntent, sources: list[AcademicSource],
                 loop_cfg: LoopConfig | None = None,
                 snow: SnowballConfig | None = None,
                 on_progress: "callable | None" = None,
                 stop_event: "threading.Event | None" = None):
        self.intent = intent
        # 不再用 health_check 静默过滤源:瞬时 SSL/网络异常会让整个源消失,
        # 前端也看不到原因。保留所有源,由 execute 的 try/except 逐源兜底并上报事件。
        self.sources = list(sources)
        self.loop_cfg = loop_cfg or intent.loop
        self.snow = snow or intent.snowball
        self.pool = PaperPool()
        self.on_progress = on_progress or (lambda e: None)
        # 用户手动停止:置位后各循环入口抛 TaskCancelledError,尽快退出
        self.stop_event = stop_event

    def _raise_if_stopped(self) -> None:
        if self.stop_event is not None and self.stop_event.is_set():
            raise TaskCancelledError("用户已手动停止")

    # === 主入口 ===

    def run_main(self) -> PaperPool:
        """主循环(同步,每源独立翻页)。返回填好 paper 的 pool。"""
        self._raise_if_stopped()
        self._emit("fetching", message=f"开始主循环,共 {len(self.sources)} 个源")
        for src in self.sources:
            self._raise_if_stopped()
            self._fetch_one_source(src)
        self._emit("fetching_done", total=len(self.pool),
                   message=f"主循环完成,共 {len(self.pool)} 篇")
        return self.pool

    async def run_async(self) -> PaperPool:
        """异步主入口:主循环 + 雪球 + 摘要回填一气呵成。"""
        self.run_main()
        if self.snow.enabled:
            await self.run_snowball()
        await self.fill_abstracts()
        return self.pool

    # === 主循环 ===

    def _fetch_one_source(self, src: AcademicSource) -> None:
        """单源检索:长英文检索式按语义单元拆成多个子检索式依次执行,
        结果统一汇入 PaperPool(按 DOI/标题 自动去重合并)。

        对称中文流程(render_cnki_candidates 生成多候选 -> cnki_adapter 依次执行):
        - 子式模板来自 query_planner.render_en_candidates(概念 id 组合);
        - 每子式独立翻页,命中 max_results_per_source 提前停止;
        - 源不支持拆分(无 build_sub_query)时退化为单条检索式(原行为)。
        """
        # 放宽梯队:丢概念(最多到核心 2 个) -> 清空排除词 -> 放宽年份
        max_relax = 3

        def _templates(intent):
            """当前意图的子检索式模板;不支持拆分的源退化为单条 [None]。"""
            if not hasattr(src, "build_sub_query"):
                return [None]
            from retrieval.query_planner import render_en_candidates
            return render_en_candidates(intent) or [None]

        def _build(intent, template):
            if template is not None and hasattr(src, "build_sub_query"):
                return src.build_sub_query(intent, list(template))
            return src.build_query(intent)

        templates = _templates(self.intent)
        for template in templates:
            active_intent = self.intent
            relax_count = 0
            # 翻页前先查上限,避免浪费请求
            if len(self.pool) >= self.loop_cfg.max_results_per_source:
                self._emit("fetching_source", source=src.name,
                           message="达到 max_results_per_source,提前停止")
                return
            self._raise_if_stopped()
            query = _build(active_intent, template)
            empty_streak = 0
            skip_candidate = False
            for page in range(1, self.loop_cfg.max_pages_per_source + 1):
                if len(self.pool) >= self.loop_cfg.max_results_per_source:
                    self._emit("fetching_source", source=src.name, page=page,
                               message="达到 max_results_per_source,提前停止")
                    return
                self._raise_if_stopped()
                try:
                    resp = self._execute_with_timeout(src, query, page, self.loop_cfg.per_page)
                except FutureTimeoutError:
                    self._emit("fetching_source", source=src.name, page=page,
                               message=f"单页请求超时({PAGE_FETCH_TIMEOUT}s),已跳过该候选式")
                    log.warning("%s 单页请求超时,已跳过该候选式", src.name)
                    skip_candidate = True
                    break
                except OpenAlexRateLimitError as e:
                    # 限流(额度耗尽/操作符过多)区别于「零结果」:不触发放宽重检,
                    # 明确提示用户是服务端限流,而非检索式无命中。
                    self._emit("fetching_source", source=src.name, page=page,
                               message=f"{src.name} 限流(429): {e}")
                    log.warning("%s 限流(429): %s", src.name, e)
                    return
                except Exception as e:
                    # 单源翻页异常 → 警告 + 跳过该源(其他源不受影响)
                    self._emit("fetching_source", source=src.name, page=page,
                               message=f"{src.name} 翻页失败,已跳过(其他源继续): {e}")
                    log.warning("%s 翻页失败: %s", src.name, e)
                    return

                # 第一页零结果 → 自动放宽检索式重检(最多 max_relax 次)
                if page == 1 and resp.total == 0 and relax_count < max_relax:
                    while relax_count < max_relax:
                        relaxed = relax_intent(active_intent)
                        if relaxed is None:
                            break
                        relax_count += 1
                        active_intent = relaxed
                        query = _build(active_intent, template)
                        self._emit(
                            "fetching_source", source=src.name, page=page,
                            message=f"该源零结果,已放宽概念重检(第 {relax_count}/{max_relax} 次)",
                        )
                        try:
                            resp = self._execute_with_timeout(
                                src, query, page, self.loop_cfg.per_page
                            )
                        except FutureTimeoutError:
                            self._emit("fetching_source", source=src.name, page=page,
                                       message=f"放宽重检超时,放弃 {src.name}")
                            log.warning("%s 放宽重检超时", src.name)
                            return
                        except Exception as e:
                            self._emit("fetching_source", source=src.name, page=page,
                                       message=f"放宽后重检仍失败,放弃 {src.name}: {e}")
                            log.warning("%s 放宽重检失败: %s", src.name, e)
                            return
                        if resp.total > 0:
                            break

                # 如果整页加完会超 max_results,只取前 N 个
                remaining = self.loop_cfg.max_results_per_source - len(self.pool)
                if remaining <= 0:
                    self._emit("fetching_source", source=src.name, page=page,
                               message="达到 max_results_per_source,提前停止")
                    return
                truncated = resp.papers[:remaining] if remaining < len(resp.papers) else resp.papers
                new_papers = self.pool.add(truncated, source=src.name)
                self._emit("fetching_source", source=src.name, page=page,
                           added=len(new_papers), total=len(self.pool),
                           message=f"命中 {resp.total} 篇")
                # 对称中文:逐条输出本次新增文献的题录(标题 / 作者 / 期刊,年份),
                # 不再显示子式概念(如 ['A', 'B'])
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
                               added=1, total=len(self.pool),
                               message=f"[命中] {len(self.pool)}/{self.loop_cfg.max_results_per_source} | {bib}")
                if len(new_papers) == 0:
                    empty_streak += 1
                    if empty_streak >= self.loop_cfg.stop_on_consecutive_empty:
                        self._emit("fetching_source", source=src.name, page=page,
                                   message=f"连续 {empty_streak} 页 0 新结果,停止")
                        return
                else:
                    empty_streak = 0
                if not resp.has_next:
                    break  # 本子式翻完,进入下一个子式
            if skip_candidate:
                continue

    # === 内部工具 ===

    def _execute_with_timeout(self, src: AcademicSource, query: dict,
                              page: int, per_page: int):
        """给单页请求加超时保护,防止网络挂死拖垮整个任务。

        在独立线程里跑 src.execute,超时后放弃并抛 FutureTimeoutError。
        """
        with ThreadPoolExecutor(max_workers=1) as pool:
            fut = pool.submit(src.execute, query, page, per_page)
            return fut.result(timeout=PAGE_FETCH_TIMEOUT)

    # === 雪球 ===

    async def run_snowball(self) -> int:
        """对池内前 max_seeds 篇 paper 跑后向引用检索(确定性,无 LLM)。

        雪球失败不应让主流程失败——它只是个补充,主流程已入库的论文照常使用。
        """
        seeds = self.pool.papers[: self.snow.max_seeds]
        self._emit("snowballing", total=len(seeds),
                   message=f"开始雪球,种子 {len(seeds)} 篇,后向 {self.snow.backward_depth} 层")
        added_total = 0
        for src in self.sources:
            if not hasattr(src, "fetch_references"):
                continue
            for seed in seeds:
                self._raise_if_stopped()
                try:
                    refs = src.fetch_references(seed, depth=self.snow.backward_depth)
                    added = self.pool.add(refs, source=src.name)
                    added_total += len(added)
                except Exception as e:
                    # 单种子失败 → 警告 + 跳过,不中断后续种子/源
                    self._emit("snowballing", source=src.name,
                               message=f"种子 {seed.lit_id} 引用拉取失败,已跳过: {e}")
                    log.warning("雪球 %s/%s 失败: %s", src.name, seed.lit_id, e)
        self._emit("snowballing_done", added=added_total,
                   message=f"雪球完成,新增 {added_total} 篇")
        return added_total

    # === 异步摘要回填 ===

    async def fill_abstracts(self) -> None:
        """并发对所有源缺失摘要的 paper 回填。

        网络异常(SSL 握手超时 / 远程主机强制关闭)不应让整个任务失败:
        已被 Controller 收纳的 paper 仍可入库,只是少数摘要缺失——
        上层 try/except 会兜住,run_task_v2 进 succeeded 分支并把错误降级为 warning。
        """
        self._emit("filling", message="开始异步摘要回填")
        for src in self.sources:
            if not hasattr(src, "fetch_abstract_if_missing"):
                continue
            self._raise_if_stopped()
            try:
                await self.pool.fill_missing_async(src)
            except Exception as e:
                # 不再吞静默警告——上报为事件,前端可看到
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
    "LoopConfig",
    "RetrievalController",
    "RetrievalProgress",
    "PreflightVerdict",
    "preflight",
    "TaskCancelledError",
]
