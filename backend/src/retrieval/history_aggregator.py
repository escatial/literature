"""统一检索历史聚合器。

统计口径(v8.2):一次「启动自动检索」= 一个 run_id = **一条**历史记录。
中文 + 英文两边共用同一个 run_id,由前端提交上来:

- 两边到齐 → 立即合并写一条(并集去重,total 是合并后的总数);
- 知网逐条抓摘要远慢于英文,正常也要几分钟,因此先到的一边只缓存不落库;
- 兜底:若某一边彻底失败(回调永远不会来),等 15 分钟把已有的一边
  单独落库,避免历史永久丢失;
- 迟到的另一边若在落库后才到达,合并更新**同一条**记录(幂等 upsert),
  绝不拆成两条各记一半。

papers_snapshot 存两边并集去重结果;sources 是合并后的库名列表,
如 ["cnki", "openalex", "pubmed"]。
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

from db.session import SessionLocal
from retrieval.history_service import upsert_history_by_run_id
from retrieval.types import Paper

log = logging.getLogger(__name__)

# 兜底窗口:仅用于「一边彻底失败没回调」的场景。知网正常耗时约 4 分钟,
# 15 分钟足够覆盖失败判定,不会误拆。
_FLUSH_AFTER_SECONDS = 900.0
# 已落库缓存的保留时长:迟到半边在此窗口内到达仍可合并回同一条。
_FLUSHED_KEEP_SECONDS = 1800.0


@dataclass
class _RunBuf:
    """单个 run_id 的内存缓存。"""

    run_id: str
    topic: str
    created_ts: float = field(default_factory=time.time)
    flushed_ts: float | None = None  # 非 None = 已落库过(等迟到半边合并)
    cnki: dict | None = None       # {"papers": [...], "task_id": ..., "failed_sources": {...}}
    english: dict | None = None    # {"papers": [...], "task_id": ..., "sources": [...], "failed_sources": {...}}


_pending: dict[str, _RunBuf] = {}    # 未落库
_flushed: dict[str, _RunBuf] = {}    # 已落库,等待迟到半边合并
_lock = threading.Lock()


def _persist(buf: _RunBuf) -> None:
    """把缓存按 run_id 幂等写入 retrieval_history(有则合并更新,无则插入)。"""
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
        upsert_history_by_run_id(
            run_id=buf.run_id,
            topic=buf.topic,
            sources=sources,
            papers=papers,
            failed_sources=failed,
            task_id=task_id,
        )
    except Exception:
        log.exception("aggregator upsert run_id=%s 失败", buf.run_id)


def _flush(run_id: str) -> None:
    """把该 run 的当前数据落库(两边齐=完整合并;超时=单边兜底)。

    落库后缓存保留在 _flushed,迟到半边到达时合并更新同一条。
    """
    with _lock:
        buf = _pending.pop(run_id, None)
        if buf is None:
            buf = _flushed.pop(run_id, None)
    if buf is None:
        return
    buf.flushed_ts = time.time()
    with _lock:
        _flushed[run_id] = buf
    _persist(buf)


def _watchdog() -> None:
    """后台线程:超时未到齐的 run 强制落库;并清理过期缓存防内存泄漏。"""
    while True:
        time.sleep(15.0)
        now = time.time()
        stale_ids: list[str] = []
        expired_flushed: list[str] = []
        with _lock:
            for rid, buf in list(_pending.items()):
                if (now - buf.created_ts) >= _FLUSH_AFTER_SECONDS:
                    stale_ids.append(rid)
            for rid, buf in list(_flushed.items()):
                if buf.flushed_ts is not None and (now - buf.flushed_ts) >= _FLUSHED_KEEP_SECONDS:
                    expired_flushed.append(rid)
        for rid in stale_ids:
            log.warning(
                "aggregator: run_id=%s 超时 %ss 仍未到齐,先按已有半边落库(迟到半边到后仍合并回这一条)",
                rid, _FLUSH_AFTER_SECONDS,
            )
            _flush(rid)
        for rid in expired_flushed:
            with _lock:
                _flushed.pop(rid, None)


_thread: threading.Thread | None = None


def _ensure_thread() -> None:
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _thread = threading.Thread(target=_watchdog, name="history-aggregator", daemon=True)
    _thread.start()


def _submit(run_id: str, topic: str, side: str, part: dict) -> None:
    """公共提交入口:side 为 "cnki" / "english"。"""
    _ensure_thread()
    with _lock:
        buf = _pending.get(run_id) or _flushed.get(run_id)
        if buf is None:
            buf = _RunBuf(run_id=run_id, topic=topic)
            _pending[run_id] = buf
        setattr(buf, side, part)
        other_ready = buf.cnki is not None and buf.english is not None
        was_flushed = buf.flushed_ts is not None
    if other_ready:
        # 两边到齐:合并落一条(未落库)/合并更新已落库的那条(迟到半边场景)
        _flush(run_id)
    elif was_flushed:
        # 兜底已按单边落库,本边是迟到半边:合并更新同一条
        _persist(buf)


def add_cnki(
    *,
    run_id: str,
    topic: str,
    papers: list[Paper],
    task_id: str,
    failed_sources: dict | None = None,
) -> None:
    """知网任务完成时调用。"""
    _submit(run_id, topic, "cnki", {
        "papers": list(papers),
        "task_id": task_id,
        "failed_sources": dict(failed_sources or {}),
    })


def add_english(
    *,
    run_id: str,
    topic: str,
    papers: list[Paper],
    task_id: str,
    sources: list[str],
    failed_sources: dict | None = None,
) -> None:
    """英文任务完成时调用。"""
    _submit(run_id, topic, "english", {
        "papers": list(papers),
        "task_id": task_id,
        "sources": list(sources),
        "failed_sources": dict(failed_sources or {}),
    })


__all__ = ["add_cnki", "add_english"]
