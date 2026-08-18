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
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import select

from db.models import PaperModel, RetrievalTaskModel
import db.session as _db_session
from retrieval.loop import RetrievalController, TaskCancelledError
from retrieval.pool import PaperPool
from retrieval.provenance import validate_paper_provenance
from retrieval.query_planner import plan_query_strings
from retrieval.sources import OpenAlexSource, PubMedSource, CNKISource
from retrieval.types import Paper
import retrieval.history_service as history_service
import retrieval.pool_writer as pool_writer

log = logging.getLogger(__name__)


# === 取消注册表(进程级) ===
# 后台任务跑在线程里,线程不可强杀;用 threading.Event 置位,
# 各循环入口检查后尽快退出。stop_tasks 由停止接口调用。
_CANCEL_LOCK = threading.Lock()
_CANCEL_EVENTS: dict[str, threading.Event] = {}


def _cancel_event(task_id: str) -> threading.Event:
    with _CANCEL_LOCK:
        ev = _CANCEL_EVENTS.get(task_id)
        if ev is None:
            ev = threading.Event()
            _CANCEL_EVENTS[task_id] = ev
        return ev


def _clear_cancel(task_id: str) -> None:
    with _CANCEL_LOCK:
        _CANCEL_EVENTS.pop(task_id, None)


def stop_tasks(task_ids: list[str] | None = None) -> list[str]:
    """置位取消标志。task_ids 为空时停止所有已注册任务。返回实际置位了标志的任务 id。"""
    stopped: list[str] = []
    with _CANCEL_LOCK:
        targets = (
            list(_CANCEL_EVENTS.keys())
            if task_ids is None
            else [t for t in task_ids if t in _CANCEL_EVENTS]
        )
        for t in targets:
            _CANCEL_EVENTS[t].set()
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
            rows = (db.query(PaperModel)
                    .filter(PaperModel.lit_id.in_(lit_ids))
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
            existing = db.get(PaperModel, p.lit_id)
            meta = {k: v for k, v in p.to_dict().items()
                    if k not in ("lit_id", "created_at", "selected")}
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
) -> RetrievalTaskModel:
    """新版任务入口。

    流程:
      1. 调 LLM 把 topic 转 SearchIntent(零领域词表);
      2. 起后台线程 / 同步跑 run_task_v2;
      3. run_task_v2 用 RetrievalController 翻页 + 雪球 + 回填。

    year_start / year_end 若提供,会覆盖 SearchIntent.filters 里的年份。
    """
    task_id = str(uuid4())
    with _db_session.SessionLocal() as db:
        task = RetrievalTaskModel(
            task_id=task_id, topic=topic, status="pending", progress=0,
            year_start=year_start or 0, year_end=year_end or 0,
            use_rerank=use_snowball,  # 复用旧字段表达"扩展检索"
            updated_at=_utcnow(),
        )
        db.add(task); db.commit(); db.refresh(task)

    selected = sources or ["pubmed", "openalex"]
    if run_inline:
        run_task_v2(task_id, selected, use_snowball)
    else:
        threading.Thread(
            target=run_task_v2, args=(task_id, selected, use_snowball), daemon=True,
        ).start()
    with _db_session.SessionLocal() as db:
        return db.get(RetrievalTaskModel, task_id)


def run_task_v2(task_id: str, sources: list[str] | None = None,
                use_snowball: bool = False) -> None:
    """新版执行逻辑:走 plan_query_strings + Controller。"""
    task = get_task(task_id)
    if not task or task.status in TERMINAL_STATUSES:
        return

    # 注册取消标志;用户点「停止」后置位,Controller 循环里抛 TaskCancelledError
    stop = _cancel_event(task_id)
    try:
        _update(task_id, status="running", progress=5)

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

        def _on(evt):
            payload = {
                "stage": evt.stage, "source": evt.source,
                "page": evt.page, "added": evt.added,
                "total": evt.total, "message": evt.message,
            }
            progress_events.append(payload)
            # v4.1:过程日志持久化,供前端按 db 拆开展示
            try:
                _append_event(task_id, payload)
            except Exception as exc:
                log.warning("过程日志落库失败: %s", exc)
            mapping = {"fetching": 30, "fetching_done": 50,
                       "snowballing": 60, "snowballing_done": 80,
                       "filling": 85, "filling_done": 95,
                       "done": 100}
            pct = mapping.get(evt.stage)
            if pct is not None:
                _update(task_id, progress=pct)

        async def _run_all():
            ctrl = RetrievalController(
                queries_per_source=queries_by_source,
                sources=src_objs,
                snow={"enabled": use_snowball, "forward_depth": 0,
                      "backward_depth": 1, "max_seeds": 100, "max_results": 500},
                on_progress=_on,
                stop_event=stop,
            )
            return await ctrl.run_async()

        pool = asyncio.run(_run_all())

        # 4. 入库(按来源覆盖写,确保文献池与本次检索结果一致)
        if not pool.papers:
            _update(task_id, status="failed", progress=100,
                    total_after_filter=0, papers=[],
                    error="未检索到任何文献")
            _notify_task_result(task_id, False,
                                f"任务 {task_id[:8]} 未检索到任何文献")
            return
        # 失败源统计:成功入库数(按 source)少于预期时,记为异常源
        write_stats = pool_writer.upsert_with_overwrite(
            pool.papers, sources=selected,
        )
        # 同步 task.papers 字段,确保 delete_task 能级联清空文献池
        try:
            with _db_session.SessionLocal() as db:
                t = db.get(RetrievalTaskModel, task_id)
                if t:
                    t.papers = [p.to_dict() for p in pool.papers]
                    db.commit()
        except Exception as exc:
            log.warning("同步 task.papers 失败: %s", exc)
        failed_sources = {
            src: write_stats.get("failed", 0)
            for src in selected if write_stats.get("failed", 0)
        }
        # 记录历史(仅 succeeded 后)
        try:
            history_service.record_history(
                topic=task.topic,
                sources=selected,
                papers=pool.papers,
                failed_sources=failed_sources,
                task_id=task_id,
            )
        except Exception as exc:
            log.warning("写入检索历史失败: %s", exc)
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
        _update(task_id, status="failed", progress=100, error=str(exc))
        _notify_task_result(task_id, False,
                            f"任务 {task_id[:8]} 执行异常:\n{exc}")
    finally:
        _clear_cancel(task_id)
