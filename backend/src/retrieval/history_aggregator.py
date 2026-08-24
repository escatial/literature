"""统一检索历史聚合器。

一次「启动自动检索」= 一个 run_id,中文 + 英文两边的数据由前端共享同一个
run_id 提交上来。本模块内存缓存每个 run_id 的两边数据,任一边到齐就先记
录;90 秒后另一边仍没到,就把已有的一边也写一条(避免丢历史)。

写入的 papers_snapshot 会把两边并集去重,total_count 是合并后的总数;
sources 字段是合并后的库名列表,如 ["cnki", "openalex", "pubmed"]。
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

from db.session import SessionLocal
from retrieval.history_service import record_history
from retrieval.types import Paper

log = logging.getLogger(__name__)

_FLUSH_AFTER_SECONDS = 90.0  # 90 秒超时,另一边还没到也先 flush


@dataclass
class _RunBuf:
    """单个 run_id 的内存缓存。"""

    run_id: str
    topic: str
    created_ts: float = field(default_factory=time.time)
    cnki: dict | None = None       # {"papers": [...], "task_id": ..., "failed_sources": {...}}
    english: dict | None = None    # {"papers": [...], "task_id": ..., "failed_sources": {...}}


_pending: dict[str, _RunBuf] = {}
_lock = threading.Lock()


def _persist(buf: _RunBuf) -> None:
    """把缓存写进 retrieval_history 一条记录,sources/total 是合并结果。"""
    cnki = buf.cnki or {}
    en = buf.english or {}
    cnki_papers: list[Paper] = cnki.get("papers") or []
    en_papers: list[Paper] = en.get("papers") or []
    failed: dict[str, int] = {}
    for src, part in ((k, v) for k, v in (("cnki", cnki), ("en", en)) if v):
        fs = part.get("failed_sources") or {}
        if not isinstance(fs, dict):
            continue
        for k, v2 in fs.items():
            try:
                failed[k] = failed.get(k, 0) + int(v2)
            except (TypeError, ValueError):
                continue
    # 合并去重(lit_id)
    merged: dict[str, Paper] = {}
    for p in cnki_papers + en_papers:
        merged[p.lit_id] = p
    papers = list(merged.values())
    sources: list[str] = []
    if buf.cnki is not None:
        sources.append("cnki")
    if buf.english is not None:
        sources.extend(buf.english.get("sources") or ["openalex", "pubmed"])
    # 单一 task_id(便于老逻辑溯源;取先到的那个)
    task_id = (cnki.get("task_id") if buf.cnki else None) or (en.get("task_id") if buf.english else None)
    try:
        record_history(
            topic=buf.topic,
            sources=sources,
            papers=papers,
            failed_sources=failed,
            task_id=task_id,
            run_id=buf.run_id,
        )
    except Exception:
        log.exception("aggregator flush run_id=%s 失败", buf.run_id)


def _flush(run_id: str) -> None:
    """从 pending 取出；只有中英文两边都完成才写入成功历史。"""
    with _lock:
        buf = _pending.pop(run_id, None)
    if buf is None:
        return
    if buf.cnki is None or buf.english is None:
        log.warning("aggregator: run_id=%s 三库未全部完成, 丢弃不完整历史", run_id)
        return
    _persist(buf)


def _watchdog() -> None:
    """后台线程:扫描超时未到齐的 run_id,强制 flush。"""
    while True:
        time.sleep(15.0)
        now = time.time()
        stale_ids: list[str] = []
        with _lock:
            for rid, buf in list(_pending.items()):
                # cnki 与 english 都到齐就不该留在 pending,这里只是兜底
                if (now - buf.created_ts) >= _FLUSH_AFTER_SECONDS:
                    stale_ids.append(rid)
        for rid in stale_ids:
            log.warning("aggregator: run_id=%s 超时 %ss 仍未到齐,强制 flush", rid, _FLUSH_AFTER_SECONDS)
            _flush(rid)


_thread: threading.Thread | None = None


def _ensure_thread() -> None:
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _thread = threading.Thread(target=_watchdog, name="history-aggregator", daemon=True)
    _thread.start()


def add_cnki(
    *,
    run_id: str,
    topic: str,
    papers: list[Paper],
    task_id: str,
    failed_sources: dict | None = None,
) -> None:
    """知网任务完成时调用。任一边到齐即 flush。"""
    _ensure_thread()
    cnki_part = {
        "papers": list(papers),
        "task_id": task_id,
        "failed_sources": dict(failed_sources or {}),
    }
    with _lock:
        buf = _pending.get(run_id)
        if buf is None:
            buf = _RunBuf(run_id=run_id, topic=topic)
            _pending[run_id] = buf
        buf.cnki = cnki_part
        ready = buf.english is not None
    if ready:
        _flush(run_id)


def add_english(
    *,
    run_id: str,
    topic: str,
    papers: list[Paper],
    task_id: str,
    sources: list[str],
    failed_sources: dict | None = None,
) -> None:
    """英文任务完成时调用。任一边到齐即 flush。"""
    _ensure_thread()
    en_part = {
        "papers": list(papers),
        "task_id": task_id,
        "sources": list(sources),
        "failed_sources": dict(failed_sources or {}),
    }
    with _lock:
        buf = _pending.get(run_id)
        if buf is None:
            buf = _RunBuf(run_id=run_id, topic=topic)
            _pending[run_id] = buf
        buf.english = en_part
        ready = buf.cnki is not None
    if ready:
        _flush(run_id)


__all__ = ["add_cnki", "add_english"]
