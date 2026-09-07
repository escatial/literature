"""检索任务服务。

两条 API:
- 旧:create_task / run_task —— 保留,内部仍用旧 OpenAlexAdapter/PubMedAdapter,
  供向后兼容(老 API 调用);
- 新:create_task_v2 / run_task_v2 —— 走 SearchIntent + AcademicSource 协议 +
  RetrievalController,带雪球 + 异步回填。

新代码请一律用 v2。
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import select

from db.models import PaperModel, RetrievalTaskModel
import db.session as _db_session
from retrieval.loop import RetrievalController, TaskCancelledError
from retrieval.pool import PaperPool
from retrieval.provenance import validate_paper_provenance
from retrieval.paper_identity import build_identity_key, repair_paper_fields
from retrieval.query_planner import plan_query_strings
from retrieval.sources import OpenAlexSource, PubMedSource, CNKISource
from retrieval.types import Paper
import retrieval.history_service as history_service
from retrieval.history_aggregator import add_english as aggregator_add_english
import retrieval.pool_writer as pool_writer

log = logging.getLogger(__name__)


# === 取消注册表(进程级) ===
# 后台任务跑在线程里,线程不可强杀;用 threading.Event 置位,
# 各循环入口检查后尽快退出。stop_tasks 由停止接口调用。
_CANCEL_LOCK = threading.Lock()
_CANCEL_EVENTS: dict[str, threading.Event] = {}
# v9.6:停止请求账本——create_task_v2 建 DB 行返回 task_id 后、后台线程跑到
# _cancel_event() 注册之前存在窗口,此间到达的 stop_tasks 找不到 Event 直接
# 丢失,任务照跑。这里把已请求停止的 task_id 记下来,注册时补置位。
_CANCEL_REQUESTED: set[str] = set()


def _cancel_event(task_id: str) -> threading.Event:
    with _CANCEL_LOCK:
        ev = _CANCEL_EVENTS.get(task_id)
        if ev is None:
            ev = threading.Event()
            _CANCEL_EVENTS[task_id] = ev
        if task_id in _CANCEL_REQUESTED:
            ev.set()
        return ev


def _clear_cancel(task_id: str) -> None:
    with _CANCEL_LOCK:
        _CANCEL_EVENTS.pop(task_id, None)
        _CANCEL_REQUESTED.discard(task_id)


def stop_tasks(task_ids: list[str] | None = None) -> list[str]:
    """置位取消标志。task_ids 为空时停止所有已注册任务。返回实际置位了标志的任务 id。

    v9.6:task_id 尚未注册(任务线程还没跑到注册点)时也记账并计入返回值,
    堵住「停止请求先于任务注册到达」的竞态窗口——否则停止指令静默丢失,
    前端以为已停止,任务继续跑完并双写文献池。
    """
    stopped: list[str] = []
    with _CANCEL_LOCK:
        if task_ids is None:
            stopped = list(_CANCEL_EVENTS.keys())
            for t in stopped:
                _CANCEL_EVENTS[t].set()
            _CANCEL_REQUESTED.update(stopped)
        else:
            for t in task_ids:
                ev = _CANCEL_EVENTS.get(t)
                if ev is not None:
                    ev.set()
                _CANCEL_REQUESTED.add(t)
                stopped.append(t)
    return stopped


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


TERMINAL_STATUSES = {"succeeded", "failed"}


def _notify_task_result(task_id: str, ok: bool, body: str) -> None:
    """任务结束通知触发桩。

    仅建框架:SMTP 授权码未配置或无可投递收件人时静默跳过;
    任何异常只记 warning,绝不中断任务主流程。
    """
    try:
        from notify import resolve_recipients, send_alert_email, send_report_email
        with _db_session.SessionLocal() as db:
            recipients = resolve_recipients(db, "report" if ok else "alert")
        if not recipients:
            return
        subject = f"[文献综述] 检索任务{'成功' if ok else '失败'} - {task_id[:8]}"
        if ok:
            send_report_email(subject, body, recipients)
        else:
            send_alert_email(subject, body, recipients)
    except Exception as exc:
        log.warning("[notify] 任务结果通知失败: %s", exc)


# === 注册表 ===

SOURCE_REGISTRY: dict[str, type] = {
    "openalex": OpenAlexSource,
    "pubmed": PubMedSource,
    "cnki": CNKISource,
}


# === 旧 API (向后兼容) ============================================
# 只保留 create_task / list_tasks / get_task / delete_task / run_task,
# 内部仍走旧适配器。新代码不要在这里加逻辑。

def _update(task_id: str, **values) -> None:
    values["updated_at"] = _utcnow()
    with _db_session.SessionLocal() as db:
        task = db.get(RetrievalTaskModel, task_id)
        if not task:
            return
        for key, value in values.items():
            setattr(task, key, value)
        db.commit()


def _append_event(task_id: str, evt: dict) -> None:
    """追加一条 v4.1 英文任务过程日志。

    每条形如 {"stage": ..., "source": ..., "page": ..., "added": ..., "total": ...,
              "message": ..., "ts": "<ISO8601>"}
    限制条数防止无限膨胀(取最近 200 条)。
    """
    from datetime import datetime, timezone
    evt = {**evt, "ts": datetime.now(timezone.utc).isoformat()}
    with _db_session.SessionLocal() as db:
        task = db.get(RetrievalTaskModel, task_id)
        if not task:
            return
        events = list(task.events or [])
        events.append(evt)
        task.events = events[-200:]
        task.updated_at = _utcnow()
        db.commit()


def _append_events_bulk(task_id: str, evts: list[dict]) -> None:
    """一次 session + 一次 commit 追加多条过程事件(同样截断 last 200)。

    并发检索下事件产生速度远超 SQLite 单写吞吐,逐条 commit 会在写锁上
    排队;攒批后写放大从 N 次 fsync 降到 1 次。
    """
    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).isoformat()
    stamped = [{**evt, "ts": now_iso} for evt in evts]
    with _db_session.SessionLocal() as db:
        task = db.get(RetrievalTaskModel, task_id)
        if not task:
            return
        events = list(task.events or [])
        events.extend(stamped)
        task.events = events[-200:]
        task.updated_at = _utcnow()
        db.commit()


class _EventBuffer:
    """进度事件缓冲器(线程安全)。

    源级×检索式级并发后,单次翻页可瞬间产生几十条 paper_hit 事件;
    若每条都开 SessionLocal+commit,SQLite 写锁会拖慢甚至打挂检索线程。
    这里先攒在内存,按「条数阈值 或 距上次落库超过时间阈值」批量落库;
    任务终态前调用 flush() 强制清空,保证过程日志完整。
    """

    def __init__(self, task_id: str, max_size: int = 20, flush_interval: float = 2.0):
        self._task_id = task_id
        self._max_size = max_size
        self._interval = flush_interval
        self._lock = threading.Lock()
        self._buf: list[dict] = []
        self._last_flush = time.monotonic()

    def append(self, evt: dict) -> None:
        """追加事件;达到阈值时立即落库(在调用线程内 flush,无后台定时器)。"""
        with self._lock:
            self._buf.append(evt)
            need = (
                len(self._buf) >= self._max_size
                or time.monotonic() - self._last_flush >= self._interval
            )
        if need:
            self.flush()

    def flush(self) -> None:
        """强制把缓冲中的事件批量落库。异常只告警,绝不中断任务主流程。"""
        with self._lock:
            pending = self._buf
            self._buf = []
            self._last_flush = time.monotonic()
        if not pending:
            return
        try:
            _append_events_bulk(self._task_id, pending)
        except Exception as exc:
            log.warning("批量落库 %d 条过程事件失败: %s", len(pending), exc)


def create_task(
    topic: str,
    year_start: int = 2020,
    year_end: int = 2026,
    min_citations: int = 0,
    limit: int = 50,
    use_rerank: bool = True,
    sources: list[str] | None = None,
    run_inline: bool = False,
) -> RetrievalTaskModel:
    """旧 API:同步入口。"""
    task_id = str(uuid4())
    with _db_session.SessionLocal() as db:
        task = RetrievalTaskModel(
            task_id=task_id, topic=topic, status="pending", progress=0,
            year_start=year_start, year_end=year_end,
            min_citations=min_citations, limit=limit, use_rerank=use_rerank,
            updated_at=_utcnow(),
        )
        db.add(task); db.commit(); db.refresh(task)
    selected = sources or ["pubmed", "openalex"]
    if run_inline:
        run_task(task_id, selected)
    else:
        threading.Thread(target=run_task, args=(task_id, selected), daemon=True).start()
    with _db_session.SessionLocal() as db:
        return db.get(RetrievalTaskModel, task_id)


def list_tasks(limit: int = 20) -> list[RetrievalTaskModel]:
    with _db_session.SessionLocal() as db:
        stmt = select(RetrievalTaskModel).order_by(RetrievalTaskModel.created_at.desc()).limit(limit)
        return list(db.execute(stmt).scalars().all())


def get_task(task_id: str) -> RetrievalTaskModel | None:
    with _db_session.SessionLocal() as db:
        return db.get(RetrievalTaskModel, task_id)


def delete_task(task_id: str) -> dict:
    with _db_session.SessionLocal() as db:
        task = db.get(RetrievalTaskModel, task_id)
        if not task:
            return {"task_deleted": False, "papers_deleted": 0,
                    "task_status": None, "papers_existed": 0}
        papers_deleted = 0
        papers_existed = 0
        if task.papers:
            lit_ids = [p.get("lit_id") for p in task.papers if p.get("lit_id")]
            # v8.1:只删无主行(task_id 为空,本任务早期直接写入的);
            # 挂在前端池 task 名下的行属于检索历史管理,不随任务删除,
            # 避免按全局 lit_id 误删其他任务的同款文献。
            rows = (db.query(PaperModel)
                    .filter(PaperModel.lit_id.in_(lit_ids),
                            PaperModel.task_id.is_(None))
                    .all())
            papers_existed = len(rows)
            for r in rows:
                db.delete(r)
            papers_deleted = papers_existed
        task_status = task.status
        db.delete(task); db.commit()
        return {
            "task_deleted": True, "papers_deleted": papers_deleted,
            "task_status": task_status, "papers_existed": papers_existed,
        }


def run_task(task_id: str, sources: list[str] | None = None) -> None:
    """兼容旧接口:转调 run_task_v2。"""
    run_task_v2(task_id, sources=sources, use_snowball=False)


def _upsert(papers: list[Paper]) -> None:
    with _db_session.SessionLocal() as db:
        for p in papers:
            validate_paper_provenance(
                str(p.source.value if hasattr(p.source, "value") else p.source),
                p.lit_id, p.source_url,
            )
            # v8.1:lit_id 不再是主键,按 (task_id 为空, lit_id) 查重
            existing = (db.query(PaperModel)
                        .filter(PaperModel.lit_id == p.lit_id,
                                PaperModel.task_id.is_(None))
                        .first())
            meta = repair_paper_fields({k: v for k, v in p.to_dict().items()
                                        if k not in ("lit_id", "created_at", "selected")})
            meta["identity_key"] = build_identity_key(
                source=str(meta.get("source") or ""), title=str(meta.get("title") or ""),
                authors=list(meta.get("authors") or []), year=int(meta.get("year") or 0),
                doi=str(meta.get("doi") or ""),
            )
            if existing:
                for k, v in meta.items():
                    setattr(existing, k, v)
            else:
                db.add(PaperModel(lit_id=p.lit_id, selected=True, **meta))
        db.commit()


# === 新 API:SearchIntent + AcademicSource + 控制器 =================

def create_task_v2(
    topic: str,
    *,
    sources: list[str] | None = None,
    use_snowball: bool = False,
    run_inline: bool = False,
    year_start: int | None = None,
    year_end: int | None = None,
    run_id: str | None = None,
    limit: int | None = None,
    pool_task_id: str | None = None,
) -> RetrievalTaskModel:
    """新版任务入口。

    流程:
      1. 调 LLM 把 topic 转 SearchIntent(零领域词表);
      2. 起后台线程 / 同步跑 run_task_v2;
      3. run_task_v2 用 RetrievalController 翻页 + 雪球 + 回填。

    year_start / year_end 若提供,会覆盖 SearchIntent.filters 里的年份。
    limit 为本次英文检索的目标文献总量(两库共享同一个池),不传则用
    DEFAULT_LOOP 里的默认上限。
    pool_task_id 为前端 X-Task-Id 隔离 ID,写入文献池时打标签(v7.1)。
    """

    selected = list(sources or ["openalex", "pubmed"])
    if set(selected) != {"openalex", "pubmed"} or len(selected) != 2:
        raise ValueError("英文检索必须同时包含且仅包含 OpenAlex、PubMed")

    task_id = str(uuid4())
    with _db_session.SessionLocal() as db:
        task = RetrievalTaskModel(
            task_id=task_id, topic=topic, status="pending", progress=0,
            year_start=year_start or 0, year_end=year_end or 0,
            use_rerank=use_snowball,  # 复用旧字段表达"扩展检索"
            updated_at=_utcnow(),
        )
        db.add(task); db.commit(); db.refresh(task)

    if run_inline:
        # v9.6:年份窗口透传(#9)——此前 API 收了入库但从未进入查询构造
        run_task_v2(task_id, selected, use_snowball, run_id=run_id, limit=limit,
                    pool_task_id=pool_task_id, year_start=year_start, year_end=year_end)
    else:
        threading.Thread(
            target=run_task_v2,
            args=(task_id, selected, use_snowball),
            kwargs={"run_id": run_id, "limit": limit, "pool_task_id": pool_task_id,
                    "year_start": year_start, "year_end": year_end},
            daemon=True,
        ).start()
    with _db_session.SessionLocal() as db:
        return db.get(RetrievalTaskModel, task_id)


def _warmup_dns(hosts: list[str]) -> None:
    """并发预热 DNS 解析,规避检索线程首请求卡在 getaddrinfo。

    socket.getaddrinfo 不受 httpx timeout 约束,Windows 下偶发挂起 30s+
    会直接吃满单页 45s 超时预算(实测复现);提前预热让检索期命中
    OS DNS 缓存。与 LLM 规划并行执行,常规耗时毫秒级。
    """
    import socket

    def _resolve(h: str) -> None:
        try:
            socket.getaddrinfo(h, 443, type=socket.SOCK_STREAM)
        except OSError as e:
            log.warning("DNS 预热失败 %s: %s", h, e)

    threads = [threading.Thread(target=_resolve, args=(h,), daemon=True) for h in hosts]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)


# v9.6:同 task_id 并发重入保护——两个线程同时跑同一任务会双双通过
# 开头的 status 检查,双写文献池。进程级 _RUNNING 集合挡住重入。
_RUNNING_LOCK = threading.Lock()
_RUNNING: set[str] = set()


def run_task_v2(task_id: str, sources: list[str] | None = None,
                use_snowball: bool = False,
                run_id: str | None = None,
                limit: int | None = None,
                pool_task_id: str | None = None,
                year_start: int | None = None,
                year_end: int | None = None) -> None:
    """Run the v2 retrieval controller(并发重入保护入口,实际逻辑见 _run_task_v2_impl)。

    year_start/year_end: 任务级发表年份窗口,透传给 RetrievalController
    (v9.6:此前 create_task_v2 收了入库但查询硬编码「近 5 年」)。
    """
    with _RUNNING_LOCK:
        if task_id in _RUNNING:
            log.warning("任务 %s 已在运行,拒绝并发重入", task_id)
            return
        _RUNNING.add(task_id)
    try:
        _run_task_v2_impl(
            task_id, sources, use_snowball, run_id=run_id, limit=limit,
            pool_task_id=pool_task_id, year_start=year_start, year_end=year_end,
        )
    finally:
        with _RUNNING_LOCK:
            _RUNNING.discard(task_id)


def _run_task_v2_impl(task_id: str, sources: list[str] | None = None,
                      use_snowball: bool = False,
                      run_id: str | None = None,
                      limit: int | None = None,
                      pool_task_id: str | None = None,
                      year_start: int | None = None,
                      year_end: int | None = None) -> None:
    """Run the v2 retrieval controller.

    year_start/year_end: 任务级发表年份窗口,透传给 RetrievalController
    (v9.6:此前 create_task_v2 收了入库但查询硬编码「近 5 年」)。
    """
    task = get_task(task_id)
    if not task or task.status in TERMINAL_STATUSES:
        return

    # 注册取消标志;用户点「停止」后置位,Controller 循环里抛 TaskCancelledError
    stop = _cancel_event(task_id)
    # v9.6:事件缓冲必须在 try 外创建——下方兜底 except 无条件调用
    # event_buffer.flush(),若异常发生在 try 内定义之前(如 _update 撞
    # SQLite busy),except 自身 NameError,failed 终态永远写不进去
    event_buffer = _EventBuffer(task_id)
    try:
        _update(task_id, status="running", progress=5)

        # 0. DNS 预热:与 LLM 规划并行,避免检索首请求卡 getaddrinfo(不受 httpx timeout 约束)
        _warmup_dns(["api.openalex.org", "eutils.ncbi.nlm.nih.gov"])

        # 1. 规划:LLM 直接输出 3 库各自的 3 条检索式字符串
        try:
            planned = plan_query_strings(task.topic, year=task.year_end or None)
        except Exception as exc:
            _update(task_id, status="failed", progress=100,
                    error=f"LLM 规划失败: {exc}")
            _notify_task_result(task_id, False,
                                f"任务 {task_id[:8]} LLM 规划失败:\n{exc}")
            return

        topic_summary = planned["topic_summary"]
        queries_by_source: dict[str, list[str]] = {
            "cnki": planned["queries_cnki"],
            "openalex": planned["queries_openalex"],
            "pubmed": planned["queries_pubmed"],
        }

        _update(task_id, progress=10, topic_summary=topic_summary,
                query_used=" | ".join(planned["queries_openalex"]))

        # 2. 装配源
        selected = sources or ["pubmed", "openalex"]
        src_objs = []
        for name in selected:
            cls = SOURCE_REGISTRY.get(name)
            if not cls:
                log.warning("未知数据源 %s,跳过", name)
                continue
            src_objs.append(cls())

        # v4.1:补"启动" + "各源就绪"两条过程日志,
        # 让前端能按 db 拆出 OpenAlex / PubMed 两条独立面板(对称中文知网)
        try:
            _append_event(task_id, {
                "stage": "starting", "source": "",
                "page": 0, "added": 0, "total": 0,
                "message": f"英文检索启动,主题={task.topic!r}",
            })
            for s in src_objs:
                _append_event(task_id, {
                    "stage": "source_ready", "source": s.name,
                    "page": 0, "added": 0, "total": 0,
                    "message": f"{s.name} 已就绪,准备翻页",
                })
        except Exception as exc:
            log.warning("补启动事件失败: %s", exc)

        if not src_objs:
            _update(task_id, status="failed", progress=100,
                    error="无可用数据源")
            _notify_task_result(task_id, False,
                                f"任务 {task_id[:8]} 无可用数据源")
            return

        # 3. 主流程 + 雪球 + 回填(异步跑完)
        progress_events: list[dict] = []
        # (事件缓冲已在 try 外创建,见函数开头——异常路径也要能 flush)

        def _on(evt):
            payload = {
                "stage": evt.stage, "source": evt.source,
                "page": evt.page, "added": evt.added,
                "total": evt.total, "message": evt.message,
            }
            progress_events.append(payload)
            # v4.1:过程日志经缓冲批量持久化,避免逐条 commit 撞 SQLite 写锁
            event_buffer.append(payload)
            mapping = {"fetching": 30, "fetching_done": 50,
                       "snowballing": 60, "snowballing_done": 80,
                       "filling": 85, "filling_done": 95,
                       "done": 100}
            pct = mapping.get(evt.stage)
            if pct is not None:
                _update(task_id, progress=pct)

        # 目标文献总量:两库共享同一个 PaperPool,达到上限即提前收尾
        loop_cfg = {"max_results_per_source": limit} if limit and limit > 0 else None

        async def _run_all():
            ctrl = RetrievalController(
                queries_per_source=queries_by_source,
                sources=src_objs,
                loop_cfg=loop_cfg,
                snow={"enabled": use_snowball, "forward_depth": 0,
                      "backward_depth": 1, "max_seeds": 100, "max_results": 500},
                on_progress=_on,
                stop_event=stop,
                # v9.6:任务级年份窗口贯通(#9),此前在源内被硬编码为「近 5 年」
                year_start=year_start,
                year_end=year_end,
            )
            return await ctrl.run_async()

        pool = asyncio.run(_run_all())
        # 检索结束:强制清空事件缓冲,后续入库事件顺序才正确
        event_buffer.flush()

        required_sources = {"openalex", "pubmed"}
        ready_sources = {source.name for source in src_objs}
        missing_sources = required_sources - ready_sources
        if missing_sources:
            error = ("三库任务失败；未启动: "
                     + ", ".join(sorted(missing_sources)) + "。请重新开始")
            _update(task_id, status="failed", progress=100,
                    total_after_filter=0, papers=[], error=error)
            _notify_task_result(task_id, False, f"任务 {task_id[:8]} {error}")
            return
        # 单条检索式超时/翻页失败/429 属局部挫折(事件流中 fetching_source warning 可见),
        # 不再判死整任务:成败只看池产出——池空由下方「未检索到任何文献」兜底。

        # 4. 入库(按来源覆盖写,确保文献池与本次检索结果一致)
        if not pool.papers:
            _update(task_id, status="failed", progress=100,
                    total_after_filter=0, papers=[],
                    error="未检索到任何文献")
            _notify_task_result(task_id, False,
                                f"任务 {task_id[:8]} 未检索到任何文献")
            return
        # 失败源统计:成功入库数(按 source)少于预期时,记为异常源
        def _emit_persisting(message: str, added: int = 0) -> None:
            """入库阶段的进度事件。按源各发一条,前端按 source 分栏才能显示。"""
            for src_name in selected:
                _append_event(task_id, {
                    "stage": "persisting", "source": src_name, "page": 0,
                    "added": added, "total": len(pool.papers),
                    "message": message,
                })

        _emit_persisting(f"开始写入文献池,共 {len(pool.papers)} 篇")
        _update(task_id, progress=96)
        write_stats = pool_writer.upsert_with_overwrite(
            pool.papers, sources=selected, pool_task_id=pool_task_id,
        )
        _emit_persisting(
            f"文献池写入完成: 新增 {write_stats.get('inserted', 0)}, "
            f"更新 {write_stats.get('updated', 0)}"
        )
        _update(task_id, progress=98)
        # 同步 task.papers 字段,确保 delete_task 能级联清空文献池
        try:
            with _db_session.SessionLocal() as db:
                t = db.get(RetrievalTaskModel, task_id)
                if t:
                    t.papers = [p.to_dict() for p in pool.papers]
                    db.commit()
        except Exception as exc:
            log.warning("同步 task.papers 失败: %s", exc)
        # v7.2:failed 按源分摊。此前把合计失败数复制给每个源,
        # 实际只失败 10 条却显示「openalex: 10, pubmed: 10」的假象。
        failed_by_source: dict[str, int] = write_stats.get("failed_by_source") or {}
        failed_sources = {
            src: int(failed_by_source.get(src, 0))
            for src in selected if int(failed_by_source.get(src, 0)) > 0
        }
        # 记录历史(仅 succeeded 后)。有 run_id 走 aggregator(合并中文那一边);
        # 没 run_id 兼容旧调用方,直接写一条历史。
        try:
            if run_id:
                aggregator_add_english(
                    run_id=run_id,
                    topic=task.topic,
                    papers=pool.papers,
                    # v8:历史记录记「池 task_id」(与 papers.task_id 同源),
                    # 不能记 v2 内部任务 id,否则检索记录与池脱节
                    task_id=pool_task_id or task_id,
                    sources=selected,
                    failed_sources=failed_sources,
                )
            else:
                history_service.record_history(
                    topic=task.topic,
                    sources=selected,
                    papers=pool.papers,
                    failed_sources=failed_sources,
                    task_id=pool_task_id or task_id,
                )
        except Exception as exc:
            log.warning("写入检索历史失败: %s", exc)
        event_buffer.flush()  # 终态落库前清空缓冲,保证过程日志完整
        _update(task_id, status="succeeded", progress=100,
                total_before_filter=len(pool.papers),
                total_after_filter=len(pool.papers),
                papers=[p.to_dict() for p in pool.papers],
                error=None)
        _notify_task_result(
            task_id, True,
            f"任务 {task_id[:8]} 已完成,共获取 {len(pool.papers)} 篇文献。\n"
            f"主题: {task.topic}",
        )
    except TaskCancelledError:
        # 用户手动停止:进程级标志置位,Controller 主动抛错退出
        event_buffer.flush()  # 停止前已产生的事件先落库
        try:
            _append_event(task_id, {
                "stage": "cancelled", "source": "",
                "page": 0, "added": 0, "total": 0,
                "message": "用户已手动停止,任务终止",
            })
        except Exception as exc:
            log.warning("停止事件落库失败: %s", exc)
        _update(task_id, status="failed", progress=100, error="用户已手动停止")
    except Exception as exc:
        log.exception("run_task_v2 失败")
        event_buffer.flush()  # 异常路径也保证已产生的事件可见,便于排查
        _update(task_id, status="failed", progress=100, error=str(exc))
        _notify_task_result(task_id, False,
                            f"任务 {task_id[:8]} 执行异常:\n{exc}")
    finally:
        _clear_cancel(task_id)
