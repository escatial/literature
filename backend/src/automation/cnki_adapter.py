"""CNKI 爬虫适配器 —— 直接调用嵌入在 `automation/cnki/` 包内的 HTTP 爬虫。

设计:
- 爬虫已嵌入项目(`automation/cnki/crawler.py` + `cjy_client.py` + `config.yaml`),
  不再动态加载外部目录;config.yaml 维护在包根,运行时文件(cookies.json/滑块图等)
  落在包内 `data/` 目录,与工作目录解耦。
- 对外保持与旧 `cnki_auto.run_cnki_full_auto` 兼容的签名与 SSE 事件流:
  plan_generated / search_submitted / search_done / fetched / done / error,
  api/cnki.py 与 retrieval/sources/cnki.py 无需感知内部实现差异。
- 同步阻塞的爬虫函数在 executor 线程中执行,事件通过 call_soon_threadsafe 推给 asyncio.Queue。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import socket
import ssl
import threading
from urllib.parse import parse_qs, urlparse

from .cnki import crawler

log = logging.getLogger(__name__)


# ===== TRAE-debugger 临时埋点:cnki-cert-mismatch =====
# 只在出现 SSLCertVerificationError 时调用,捕捉证据,不改业务。
def _diag_ssl_once(idx: int, total: int, exc: BaseException) -> None:
    """抓 SSL 错误现场的关键证据,写入 .dbg/。
    触发条件:摘要抓取阶段出现 SSLCertVerificationError。
    """
    try:
        import json
        import time
        from pathlib import Path

        dbg_dir = Path(".dbg")
        dbg_dir.mkdir(parents=True, exist_ok=True)
        # 1) 解析失败 IP
        try:
            addrs = socket.getaddrinfo("kns.cnki.net", 443, type=socket.SOCK_STREAM)
            ips = sorted({a[4][0] for a in addrs})
        except Exception as e:  # noqa: BLE001
            ips = [f"<resolve-failed: {e!r}>"]
        # 2) 拿到证书,看 SNI/issuer 是否异常
        cert_info: dict = {}
        try:
            ctx = ssl.create_default_context()
            with ctx.wrap_socket(
                socket.socket(socket.AF_INET, socket.SOCK_STREAM),
                server_hostname="kns.cnki.net",
            ) as s:
                s.settimeout(8)
                s.connect((ips[0], 443))
                cert = s.getpeercert(binary_form=False) or {}
                cert_info = {
                    "subject": dict(x[0] for x in cert.get("subject", [])),
                    "issuer": dict(x[0] for x in cert.get("issuer", [])),
                    "san": [
                        v for (_typ, v) in cert.get("subjectAltName", [])
                    ],
                    "notBefore": cert.get("notBefore"),
                    "notAfter": cert.get("notAfter"),
                }
        except Exception as e:  # noqa: BLE001
            cert_info = {"error": repr(e)}
        # 3) 链路信息
        payload = {
            "ts": time.time(),
            "idx": idx,
            "total": total,
            "hostname": "kns.cnki.net",
            "resolved_ips": ips,
            "matched_ips": ips[:1],
            "cert": cert_info,
            "env": {
                "HTTP_PROXY": os.environ.get("HTTP_PROXY"),
                "HTTPS_PROXY": os.environ.get("HTTPS_PROXY"),
                "NO_PROXY": os.environ.get("NO_PROXY"),
                "http_proxy": os.environ.get("http_proxy"),
                "https_proxy": os.environ.get("https_proxy"),
                "REQUESTS_CA_BUNDLE": os.environ.get("REQUESTS_CA_BUNDLE"),
                "SSL_CERT_FILE": os.environ.get("SSL_CERT_FILE"),
            },
            "exc_type": type(exc).__name__,
            "exc_repr": repr(exc),
        }
        out = dbg_dir / "trae-debug-ssl.json"
        with out.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        log.warning("[cnki-diag] SSL证据已写入 %s", out)
    except Exception as inner:  # 埋点自身不能影响主流程
        log.warning("[cnki-diag] 埋点失败: %r", inner)


_SSL_DIAG_LOGGED = False
# ==============================================


def _build_lit_id(url: str) -> str:
    """lit_cnki_ + sha256(url)[:16],满足 provenance 前缀校验与 32 字符上限。"""
    return "lit_cnki_" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def _dedupe_key(url: str) -> str:
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    dbcode = (params.get("dbcode") or params.get("DbCode") or [""])[0].lower()
    filename = (params.get("filename") or params.get("FileName") or [""])[0].lower()
    if dbcode and filename:
        return f"{dbcode}:{filename}"
    return url.split("#", 1)[0]


def _clear_source_records(db_type: str) -> int:
    """入库前清空同源历史(需求3:同源覆盖写入,与英文 pool_writer.upsert_with_overwrite 对齐)。

    返回清空的条数。仅在确认本次抓到了列表后才调用,避免检索失败时误清旧数据。
    """
    from db.models import PaperModel
    from db.session import SessionLocal

    with SessionLocal() as db:
        rows = db.query(PaperModel).filter(PaperModel.source == db_type).all()
        n = len(rows)
        for r in rows:
            db.delete(r)
        db.commit()
    return n


def _persist_record(record: dict) -> bool:
    """基于 lit_id upsert;provenance 校验失败返回 False。"""
    from db.models import PaperModel
    from db.session import SessionLocal
    from retrieval.provenance import validate_paper_provenance

    try:
        validate_paper_provenance(
            record["source"], record["lit_id"], record.get("source_url") or ""
        )
    except ValueError:
        return False
    with SessionLocal() as db:
        existing = db.get(PaperModel, record["lit_id"])
        if existing:
            for k, v in record.items():
                if k in {"lit_id", "created_at"}:
                    continue
                setattr(existing, k, v)
        else:
            db.add(PaperModel(**record))
        db.commit()
    return True


def _detail_to_record(d: dict, db_type: str) -> dict:
    """把爬虫 fetch_abstract 的结果映射为 PaperModel 字段。"""
    url = d.get("url") or ""
    year = 0
    pub = (d.get("publish_time") or "").strip()
    if pub[:4].isdigit():
        year = int(pub[:4])
    return {
        "lit_id": _build_lit_id(url),
        "source": db_type,
        "title": d.get("title") or "",
        "authors": d.get("authors") or [],
        "journal": d.get("source") or "",
        "year": year,
        "abstract": d.get("abstract") or "",
        "abstract_text": d.get("abstract") or "",
        "doi": d.get("doi") or "",
        "source_url": url,
        "raw_citation": "",
        "quote_text": "",
        "selected": True,
    }


async def run_cnki_full_auto(
    *,
    topic: str,
    expert_query: str | None = None,
    expert_queries: list[str] | None = None,
    target_count: int = 300,
    queue: asyncio.Queue,
    config: dict | None = None,
    soft_id: str = "",
    user: str = "",
    password: str = "",
    max_pages: int = 10,
    db_type: str = "cnki",
    stop_event: "threading.Event | None" = None,
) -> dict:
    """顶层入口:驱动嵌入爬虫,事件推入 queue。

    签名与旧 cnki_auto.run_cnki_full_auto 保持一致(api/cnki.py 无需改动)。
    使用上游生成的知网专业检索式；旧调用未提供时保留主题词检索兼容。
    """
    del soft_id, user, password, config  # 爬虫凭据走 config.yaml / CJY_* 环境变量
    loop = asyncio.get_running_loop()

    def emit(**evt):
        # 从 executor 线程推事件到 asyncio.Queue(线程安全)
        loop.call_soon_threadsafe(queue.put_nowait, evt)

    # 用户手动停止:置位后各循环入口抛 _CnkiStopped,尽快退出
    class _CnkiStopped(Exception):
        pass

    def _check_stopped():
        if stop_event is not None and stop_event.is_set():
            raise _CnkiStopped("用户已手动停止")

    queries = [query.strip() for query in (expert_queries or []) if query.strip()]
    if not queries and expert_query and expert_query.strip():
        queries = [expert_query.strip()]
    if not queries:
        queries = [topic]

    # max_pages<=0 视为"翻到知网无结果为止";否则按页数计算 max_count
    page_size = int(crawler.CONFIG["search"]["page_size"] or 20)
    if max_pages is None or max_pages <= 0:
        max_count = None
        emit(stage="plan_generated", queries=queries, db=db_type, unbounded=True)
    else:
        max_count = max_pages * page_size
        emit(stage="plan_generated", queries=queries, db=db_type, max_count=max_count)
    emit(stage="search_submitted", db=db_type)
    # 把本次预计抓取上限打到日志面板,避免翻页数与用户预期对不上
    if max_count is None:
        emit(stage="log", msg=f"[计划] 共 {len(queries)} 条候选检索式,目标至少 {target_count} 篇,翻页上限=无", db=db_type)
    else:
        emit(stage="log", msg=f"[计划] 共 {len(queries)} 条候选检索式,目标至少 {target_count} 篇,每式最多 {max_pages} 页", db=db_type)

    def _sync_run() -> tuple[int, int]:
        """同步阻塞:列表 + 逐条摘要 + 入库,返回 (saved, skipped)。

        挂接爬虫的过程日志回调,把翻页/验证码/抓取进度实时推给前端 SSE。
        """
        crawler.set_log_callback(lambda m: emit(stage="log", msg=m, db=db_type))
        try:
            return _run_sync_inner()
        finally:
            crawler.set_log_callback(None)

    def _run_sync_inner() -> tuple[int, int]:
        unique_items: dict[str, dict] = {}
        for query_index, query in enumerate(queries, start=1):
            _check_stopped()
            per_query_count = target_count if max_count is None else min(max_count, target_count)
            emit(stage="log", msg=f"[预检] {query_index}/{len(queries)} 提交: {query}", db=db_type)
            try:
                query_json = (
                    crawler.build_expert_query(expert_str=query)
                    if query.startswith(("SU=", "TI=", "KY=", "AB=", "FT="))
                    else crawler.build_query(keyword=query)
                )
                found = crawler.fetch_all_list(query_json=query_json, max_count=per_query_count)
            except Exception as exc:
                emit(stage="log", msg=f"[预检] {query_index}/{len(queries)} 失败: {exc}", db=db_type)
                continue
            before = len(unique_items)
            for item in found:
                url = item.get("url") or ""
                if url:
                    unique_items.setdefault(_dedupe_key(url), item)
            added = len(unique_items) - before
            emit(stage="log", msg=f"[预检] {query_index}/{len(queries)} 命中 {len(found)} 条,新增 {added} 条,累计去重 {len(unique_items)} 条", db=db_type)
            if len(unique_items) >= target_count:
                emit(stage="log", msg=f"[预检] 已达到目标阈值 {target_count} 篇,停止尝试后续检索式", db=db_type)
                break
        items = list(unique_items.values())[:target_count]
        emit(stage="search_done", ok=bool(items), total=len(items), db=db_type)
        if not items:
            return 0, 0
        # 需求3:入库前清空同源历史,避免文献池累积多次检索的旧数据
        cleared = _clear_source_records(db_type)
        if cleared:
            emit(stage="log", msg=f"[入库] 已清空同源旧数据 {cleared} 篇,本次将覆盖写入", db=db_type)
        saved = skipped = 0
        total = len(items)
        for idx, it in enumerate(items, start=1):
            _check_stopped()
            url = it.get("url") or ""
            if not url:
                skipped += 1
                continue
            try:
                detail = crawler.fetch_abstract(url)
            except Exception as exc:
                # TRAE-debugger:首次 SSL 错误时落地证据
                global _SSL_DIAG_LOGGED
                if not _SSL_DIAG_LOGGED and (
                    "SSLCertVerificationError" in repr(exc)
                    or "Hostname mismatch" in repr(exc)
                    or "CERTIFICATE_VERIFY_FAILED" in repr(exc)
                ):
                    _diag_ssl_once(idx, total, exc)
                    _SSL_DIAG_LOGGED = True
                log.warning("[cnki] 摘要失败 %s: %s", url[:80], exc)
                emit(stage="log", msg=f"[摘要] {idx}/{total} 抓取失败: {exc}", db=db_type)
                skipped += 1
                continue
            record = _detail_to_record(detail, db_type)
            if _persist_record(record):
                saved += 1
            else:
                skipped += 1
            # 阶段事件:前端用 saved/total 计算进度条
            emit(
                stage="fetched",
                page_no=idx, saved=saved, total=saved,
                skipped_invalid_source=skipped, db=db_type,
                progress_total=total, progress_done=idx,
            )
            # 题录与抓取进度合并为单行日志(去「黑色控制台」感)
            authors = record.get("authors") or []
            author_str = ", ".join(authors[:3]) + ("等" if len(authors) > 3 else "")
            bib = f"《{record.get('title') or '(无题名)'}》"
            if author_str:
                bib += f" / {author_str}"
            if record.get("journal"):
                bib += f" / {record.get('journal')}"
            if record.get("year"):
                bib += f", {record.get('year')}"
            emit(stage="log",
                 msg=f"[摘要] {idx}/{len(items)} | 入库 {saved} | {bib}",
                 db=db_type)
        return saved, skipped

    try:
        saved, skipped = await asyncio.to_thread(_sync_run)
    except _CnkiStopped as exc:
        # 用户手动停止:置位标志后循环抛错退出
        emit(stage="error", msg=str(exc), db=db_type)
        return {"status": "failed", "saved": 0, "reason": str(exc)}
    except Exception as exc:
        log.exception("cnki 爬虫异常")
        emit(stage="error", msg=str(exc), db=db_type)
        return {"status": "failed", "saved": 0, "reason": str(exc)}

    emit(
        stage="done",
        saved=saved,
        total_saved=saved,
        skipped_invalid_source=skipped,
        db=db_type,
    )
    return {
        "status": "succeeded",
        "saved": saved,
        "skipped": skipped,
        "prechecked": saved + skipped,
        "target_reached": saved + skipped >= target_count,
    }


# === 兼容旧接口的同步包装(供单元测试) ===
def build_query(keyword: str) -> str:
    """构造知网高级检索 QueryJson(URL 编码),供测试直接断言。"""
    return crawler.build_query(keyword=keyword)


def check_cookies_health() -> dict:
    """调用爬虫 check_cookies() 探测 cookie 是否可用(不触发搜索/不扣题分)。"""
    try:
        ok = crawler.check_cookies()
        return {"ok": bool(ok), "detail": "cookie 校验通过" if ok else "cookie 失效或缺失"}
    except Exception as exc:
        log.warning("cookie 健康检查异常: %s", exc)
        return {"ok": False, "detail": f"cookie 检查异常: {exc}"}
