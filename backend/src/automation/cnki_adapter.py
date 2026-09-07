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
import random
import re
import socket
import ssl
import threading
import time
from urllib.parse import parse_qs, urlparse

from .cnki import crawler
from .cnki import monitor as _monitor
from .cnki import quality as _quality
from .cnki import scheduler as _scheduler
from .cnki.crawler import CnkiServerBusyError
from core_journals import clean_journal_name
from retrieval.query_planner import normalize_cnki_query

log = logging.getLogger(__name__)

# 摘要详情页抓取的温和并发数。crawler 为模块级单例(全局 CONFIG + requests.Session),
# 知网风控敏感:3 线程 + 每篇 0.3~0.8s 抖动延迟是「能感知提速又不触发验证码风暴」
# 的平衡点;列表页/检索式循环仍保持串行,由严到宽的梯度短路逻辑不受影响。
DETAIL_CONCURRENCY = 3


def _on_param_change(task_id: str, patch: dict) -> None:
    """监控面板参数热调整回调。

    v9.6:此前只处理 delay_seconds,docstring 声称其余参数「由动态池与检索
    逻辑在各自循环点自然读取」但实际无任何读取点——调参返回 200 实为空操作。
    现在:
    - delay_seconds → 限速器热重载(原有)
    - max_workers   → 动态池实例属性热调(desired_workers/effective_max 均实时读)
    - page_size     → crawler.CONFIG["search"]["page_size"] 热写(下一页即生效,
                      L1326 每页都从 CONFIG 现读)
    - max_per_keyword → 仅落账(启动参数透传,运行中不可达),API 响应中标注
    """
    delay = patch.get("delay_seconds")
    if delay is not None:
        try:
            crawler.throttle_init(float(delay))
            log.info("[cnki] 任务 %s 热调整 delay_seconds=%s 已生效", task_id, delay)
        except Exception:
            log.exception("[cnki] delay_seconds 热调整失败")

    workers = patch.get("max_workers")
    if workers is not None:
        try:
            pool = _scheduler.get_pool()
            if pool is not None:
                pool.max_workers = max(int(workers), pool.min_workers)
                log.info("[cnki] 任务 %s 热调整 max_workers=%s 已生效", task_id, workers)
            else:
                log.warning("[cnki] 动态池未初始化,max_workers 热调整跳过")
        except Exception:
            log.exception("[cnki] max_workers 热调整失败")

    page_size = patch.get("page_size")
    if page_size is not None:
        try:
            crawler.CONFIG["search"]["page_size"] = int(page_size)
            log.info("[cnki] 任务 %s 热调整 page_size=%s 已生效(下一页起)", task_id, page_size)
        except Exception:
            log.exception("[cnki] page_size 热调整失败")


# 注册到全局任务注册表（单例，幂等去重）：面板改参数 → 运行中任务即时生效
_monitor.get_registry().register_param_hook(_on_param_change)

# 报纸类条目过滤(v8.3):
# - 知网 url 参数 dbcode=CCND 表示「中国重要报纸全文数据库」,收录的是报纸文章
#   而非学术论文,混进综述会拉低引用质量(如《夜访农家听民声》这类报道)。
# - GB/T 7714 引文中的 [N] 类型标记 = Newspaper,双保险。
_NEWS_DBCODES = {"ccnd"}
_NEWS_TYPE_MARK_RE = re.compile(r"\[N\]")


def _is_news_item(record: dict, url: str) -> bool:
    """判断一条知网记录是否为报纸类条目。"""
    try:
        qs = parse_qs(urlparse(url).query)
        dbcode = (qs.get("dbcode") or [""])[0].strip().lower()
    except Exception:
        dbcode = ""
    if dbcode in _NEWS_DBCODES:
        return True
    citation = (record.get("raw_citation") or "").strip()
    return bool(citation and _NEWS_TYPE_MARK_RE.search(citation))


# ---- v8.7 学位论文过滤:文献综述只允许期刊文献 ----
# 根因:config 里 CAPJ(期刊)与 CROSSDB(总库)共用同一串 KuaKuCode,且
# searchFrom 表单写死「资源范围:总库」→ 期刊检索实际跑在总库上,
# 硕士(CMFD)/博士(CDFD)学位论文混入结果。修复:入库链路确定性过滤。
# 判据(确定性,不依赖知网库参数是否生效):
#   1) url 参数 dbname/dbcode 以 CMFD(硕士库)/CDFD(博士库)开头
#   2) GB/T 7714 引文类型标识 [D](Dissertation)
_THESIS_DB_PREFIXES = ("cmfd", "cdfd")
_THESIS_TYPE_MARK_RE = re.compile(r"\[D\]")


def _is_thesis_url(url: str) -> bool:
    """列表条目/详情 url 是否指向学位论文库(CMFD 硕士 / CDFD 博士)。"""
    try:
        qs = parse_qs(urlparse(url).query)
    except Exception:
        return False
    for key in ("dbname", "dbName", "dbcode", "DbCode"):
        for val in qs.get(key, []):
            if val.strip().lower().startswith(_THESIS_DB_PREFIXES):
                return True
    return False


def _is_thesis_item(record: dict, url: str) -> bool:
    """判断一条知网记录是否为学位论文(硕士/博士)。综述仅收期刊,一律剔除。"""
    if _is_thesis_url(url):
        return True
    citation = (record.get("raw_citation") or "").strip()
    return bool(citation and _THESIS_TYPE_MARK_RE.search(citation))


def _strip_thesis_items(items: list[dict], emit, db_type: str) -> list[dict]:
    """列表级过滤:预检阶段剔除学位论文条目(凭 url dbname 与引文 [D] 标识)。

    放在守门 _gate_list_items 之前:先除杂再判命中率,避免学位论文
    拉低命中率的统计;剔除数打日志,用户可见。
    """
    kept: list[dict] = []
    removed = 0
    for it in items:
        url = it.get("url") or ""
        quote = (it.get("quote_text") or "").strip()
        if _is_thesis_url(url) or (quote and _THESIS_TYPE_MARK_RE.search(quote)):
            removed += 1
            continue
        kept.append(it)
    if removed:
        emit(stage="log",
             msg=f"[过滤] 已剔除 {removed} 篇学位论文(文献综述仅收期刊文献)",
             db=db_type)
    return kept



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


def _clear_source_records(db_type: str, pool_task_id: str | None = None) -> int:
    """入库前清空同源历史(需求3:同源覆盖写入,与英文 pool_writer.upsert_with_overwrite 对齐)。

    v7.1:pool_task_id 非空时只清「该任务」的同源数据,不跨任务误删。

    返回清空的条数。仅在确认本次抓到了列表后才调用,避免检索失败时误清旧数据。
    """
    from db.models import PaperModel
    from db.session import SessionLocal

    with SessionLocal() as db:
        q = db.query(PaperModel).filter(PaperModel.source == db_type)
        if pool_task_id:
            q = q.filter(PaperModel.task_id == pool_task_id)
        rows = q.all()
        n = len(rows)
        for r in rows:
            db.delete(r)
        db.commit()
    return n


def _persist_record(record: dict, pool_task_id: str | None = None) -> bool:
    """基于 lit_id upsert;provenance 校验失败返回 False。

    v7.1:写入/更新时打 task_id 标签,否则文献池按 X-Task-Id 过滤会显示 0 条。
    """
    from db.models import PaperModel
    from db.session import SessionLocal
    from retrieval.provenance import validate_paper_provenance

    try:
        validate_paper_provenance(
            record["source"], record["lit_id"], record.get("source_url") or ""
        )
    except ValueError:
        return False
    if pool_task_id:
        record = {**record, "task_id": pool_task_id}
    with SessionLocal() as db:
        # v8.1:按 (task_id, lit_id) 查重 —— 同一文献可在不同任务各存一行
        q = db.query(PaperModel).filter(PaperModel.lit_id == record["lit_id"])
        if pool_task_id:
            q = q.filter(PaperModel.task_id == pool_task_id)
        else:
            q = q.filter(PaperModel.task_id.is_(None))
        existing = q.first()
        if existing:
            for k, v in record.items():
                if k in {"lit_id", "created_at"}:
                    continue
                setattr(existing, k, v)
        else:
            db.add(PaperModel(**record))
        db.commit()
    return True


def _persist_records(records: list[dict], pool_task_id: str | None = None) -> list[bool]:
    """批量 upsert:单 session + 单 commit;返回与 records 顺序对应的成败列表。

    原逐篇 _persist_record 每篇一次 session+commit,300 篇就是 300 次写事务;
    攒一批后写放大从 N 次 fsync 降到 1 次,入库阶段耗时随 commit 次数线性下降。
    provenance 校验失败的条目直接标 False,不进本次事务。
    """
    from db.models import PaperModel
    from db.session import SessionLocal
    from retrieval.provenance import validate_paper_provenance

    results: list[bool] = []
    valid: list[dict] = []
    for record in records:
        try:
            validate_paper_provenance(
                record["source"], record["lit_id"], record.get("source_url") or ""
            )
        except ValueError:
            results.append(False)
            continue
        if pool_task_id:
            record = {**record, "task_id": pool_task_id}
        valid.append(record)
        results.append(True)
    if not valid:
        return results
    try:
        with SessionLocal() as db:
            for record in valid:
                # v8.1:按 (task_id, lit_id) 查重 —— 同一文献可在不同任务各存一行
                q = db.query(PaperModel).filter(PaperModel.lit_id == record["lit_id"])
                rid = record.get("task_id")
                if rid:
                    q = q.filter(PaperModel.task_id == rid)
                else:
                    q = q.filter(PaperModel.task_id.is_(None))
                existing = q.first()
                if existing:
                    for k, v in record.items():
                        if k in {"lit_id", "created_at"}:
                            continue
                        setattr(existing, k, v)
                else:
                    db.add(PaperModel(**record))
            db.commit()
    except Exception as exc:
        # 批量 commit 失败(如瞬时锁冲突/个别坏行连坐整批):
        # 降级为逐条写入,只让真正有问题的行失败,不连坐整块。
        log.warning("[cnki] 批量入库失败,降级逐条写入: %s", exc)
        vi = 0
        for i, ok in enumerate(results):
            if not ok:
                continue
            try:
                results[i] = _persist_record(valid[vi], pool_task_id)
            except Exception as row_exc:
                # 单行写入也失败(约束冲突等):标记 False,继续处理下一行
                log.warning("[cnki] 降级单条入库失败 lit_id=%s: %s", valid[vi].get("lit_id"), row_exc)
                results[i] = False
            vi += 1
    return results


_AUTHORS_HEAD_RE = re.compile(r"^\s*\[\d+\]\s*")


def _authors_from_citation(citation: str) -> list[str]:
    """从 GB/T 7714 引文解析作者(详情页 authorpart XPath 未命中时的回填源)。

    根因背景:知网详情页有多种模板变体,部分页面没有 h3.author#authorpart,
    导致 _parse_detail 的 authors=[];而 GB/T 引文(服务端导出,必含作者段)
    此刻已成功拿到,不用白不用。引文形如:
      `[1]张三, 李四. 某标题[J]. 某刊, 2024.`
    取第一个句点前的作者段,按逗号拆分,过滤「等」与异常段。
    """
    text = (citation or "").strip()
    if not text:
        return []
    text = _AUTHORS_HEAD_RE.sub("", text)  # 去列表页序号 [1] [2]...
    head = re.split(r"[.。]", text, maxsplit=1)[0]
    authors: list[str] = []
    for part in re.split(r"[,，;；]", head):
        name = part.strip()
        # 「等」/空段跳过;过长或带文献类型标记([J][D]等)的段不是作者名
        if not name or name in ("等", "et al", "ET AL"):
            continue
        if len(name) > 40 or "[" in name:
            continue
        authors.append(name)
    return authors[:25]


def _detail_to_record(d: dict, db_type: str) -> dict:
    """把爬虫 fetch_abstract 的结果映射为 PaperModel 字段。

    v6.1:把列表页抓到的 GB/T 7714-2025 引文(quote_text)直接写到 raw_citation。
    渲染时 render_reference_list 走「raw_citation 已有」分支,不再 fallback 拼装。
    quote_text 同时保留(给将来「GB/T vs MLA vs APA 多格式」用)。
    """
    import re as _re_gbt
    url = d.get("url") or ""
    year = 0
    pub = (d.get("publish_time") or "").strip()
    if pub[:4].isdigit():
        year = int(pub[:4])
    # 优先用列表页抓到的 GB/T 7714 引文;缺失时再考虑详情页摘要区里的备用
    # v6.1:优先用 server-side GB/T 7714 导出 API 拿到的引文(d.get("gbt_citation"));
    # 兜底:列表页 quote_text 字段(老逻辑,保留向后兼容)。
    gbt = (d.get("gbt_citation") or "").strip()
    quote_text = (d.get("quote_text") or "").strip()
    citation = gbt or quote_text
    # 清洗:知网的 GB/T 7714 引文末尾会带「查看该刊数据库收录来源」之类的脏尾巴。
    # 历史 bug:同一篇正文里出现过 `贵州畜牧兽医, 2026, 50(04) 查看该刊数据库收录来源, 2026`
    # 这种尾巴(年号重复/换行混排),必须有兜底规则。
    if citation:
        # 1) 任何"查看该刊数据库收录来源"开始、后面接任意内容,一律截断到句号
        citation = _re_gbt.sub(r"查看该刊数据库收录来源[\s\S]*?(?=[。\.](?:\s|$)|\n|$)", "", citation)
        # 2) 同段落里"年份, 年份"重复(kjb 有些详情页会粘两遍出版年和在线公开年):
        #    A,B,C,2026, 50(04):... ,2026 -> 留下" ,2026" 之前的内容。
        #    匹配最后 ", YYYY 或 ,YYYY" 段后跟空格或 :, 并在原文中出现两次以上
        citation = _re_gbt.sub(
            r"(?<=[\.\u3002\)\]])[\s,，]*(1[89]\d{2}|20\d{2})\s*$",
            "",
            citation,
        )
        # 3) 收尾空白 / 多余空格
        citation = _re_gbt.sub(r"\s{2,}", " ", citation).strip()
        citation = _re_gbt.sub(r"[,，\s]+$", "", citation).strip()
    from retrieval.paper_identity import (
        build_identity_key,
        build_lit_id,
        repair_paper_fields,
        validate_paper_identity,
    )
    # 作者回填:必须在 build_lit_id 之前,保证身份指纹与回填后的作者一致
    authors = d.get("authors") or []
    if not authors:
        authors = _authors_from_citation(citation)
        if authors:
            log.warning("[cnki] 详情页无作者节点,已从 GB/T 引文回填 %d 位作者: %s",
                        len(authors), url[:80])
    record = {
        "lit_id": build_lit_id(
            source=db_type, title=d.get("title") or "", authors=authors,
            year=year, doi=d.get("doi") or "",
        ),
        "source": db_type,
        "title": d.get("title") or "",
        "authors": authors,
        # v7.3:详情页 source 原文是「刊名 . 年卷期 查看该刊数据库收录来源」,
        # 直接落库会污染 journal 字段导致核心期刊匹配全败,落库前先清洗。
        "journal": clean_journal_name(d.get("source") or ""),
        "year": year,
        "abstract": d.get("abstract") or "",
        "abstract_text": d.get("abstract") or "",
        "doi": d.get("doi") or "",
        "source_url": url,
        # raw_citation 与 quote_text 都存,渲染时优先 raw_citation
        "raw_citation": citation,
        "quote_text": citation,
        "selected": True,
    }
    record = repair_paper_fields(record)
    record["identity_key"] = build_identity_key(
        source=db_type, title=record["title"], authors=record["authors"],
        year=record["year"], doi=record["doi"],
    )
    validate_paper_identity(record)
    return record


# ---- v8.4 结果侧守门:防知网静默降级 ----
# 事故实证(2026-08-29):知网对无法执行的 Expert 检索式不报错,静默降级为
# 「最新收录」默认列表(165 篇全 2026 大杂烩,含「院士寄语」,主题交叉命中 0 篇)。
# 爬虫解析器照单全收,垃圾直接入库。守门:列表抓回后先验证与检索词的匹配率。

_QUERY_TERM_RE = re.compile(r"'([^']+)'")
_GATE_MIN_ITEMS = 10        # 少于该条数不做批级判定(样本太小)
# 阈值 0.05 而非 0.5:知网静默降级返回"最新收录大杂烩",标题命中率趋近 0;
# 而 SU= 主题检索是标引匹配,正常结果的标题命中率本就常在 5%~40%
# (标题用词≠主题标引词)。0.5 会把正常结果整批误杀 → 0 篇入库(2026-08-31 22:50 事故)。
_GATE_HIT_RATIO = 0.05      # 批命中率低于该值 → 判定检索式未生效,整批弃用


def _extract_query_terms(query: str) -> list[str]:
    """从专业检索式提取全部引号内词项('主题词A'+'主题词B')*'主题词C' → [主题词A,主题词B,主题词C]。"""
    return [t.strip().lower() for t in _QUERY_TERM_RE.findall(query or "") if t.strip()]


def _gate_list_items(query: str, items: list[dict], db_type: str, emit) -> list[dict]:
    """批级守门:返回结果里标题命中任一检索词的占比过低 → 知网没执行检索式。

    命中按标题做(列表页只有标题可靠);摘要还没抓,不浪费请求。
    返回原列表(不裁剪单条——检索结果本应宽松,只做批级真伪判定)。
    判定失败时抛 ValueError,由调用方决定弃用该式并告警。
    """
    terms = _extract_query_terms(query)
    if not terms or len(items) < _GATE_MIN_ITEMS:
        return items
    hit = sum(
        1 for it in items
        if any(t in (it.get("title") or "").lower() for t in terms)
    )
    ratio = hit / len(items)
    if ratio < _GATE_HIT_RATIO:
        # 知网静默降级的典型形态:最新收录大杂烩,标题命中率趋近 0
        emit(stage="log",
             msg=f"[守门] 检索式未生效:返回 {len(items)} 条中仅 {hit} 条标题含检索词"
                 f"(命中率 {ratio:.0%} < {_GATE_HIT_RATIO:.0%}),疑似知网降级返回默认列表,"
                 f"该式整批弃用: {query[:80]}",
             db=db_type)
        raise ValueError(f"知网检索式未生效(批命中率 {ratio:.0%}): {query[:60]}")
    return items


def _dump_plan_queries(queries: list[str], topic: str) -> None:
    """检索式落盘 .dbg:plan_generated 只推前端 SSE 不落库,降级事故无法事后取证。

    2026-08-29 事故时本次提交的式子原文已丢失,只能靠结果反推。此后每次落盘。
    """
    try:
        import json
        import time
        from pathlib import Path
        dbg_dir = Path(__file__).resolve().parent.parent / ".dbg"
        dbg_dir.mkdir(exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        payload = {"topic": topic, "created": stamp, "queries": queries}
        (dbg_dir / f"cnki_queries_{stamp}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass  # 取证落盘失败不影响主流程


class _CnkiStopped(Exception):
    """用户手动停止:各循环入口(check_stopped/冷却分段)抛出,尽快退出。

    v9.8 从 run_cnki_full_auto 闭包提升到模块级 —— 自愈重启调度器
    (_run_with_auto_restart)是模块级函数,闭包类会 NameError;单类也修正了
    每次调用创建一个新异常类、跨调用 isinstance 不可比的隐患。
    """


def _sleep_in_chunks(sleep_fn, check_stopped, total: float, chunk: float = 5.0) -> None:
    """冷却期分段睡眠:每段之间检查停止请求,保证面板「停止」秒级响应。

    补漏轮冷却长达数十至数百秒,若一次性 sleep,用户点停止后要等满整段才能退出。
    """
    slept = 0.0
    while slept < total:
        check_stopped()
        step = min(chunk, total - slept)
        sleep_fn(step)
        slept += step


# 补漏轮数与轮间冷却(秒):知网限流窗口为分钟级,轮间冷却逐级拉长
# (60/120/300,累计最多 8 分钟),比单页退避(15/45/120)更彻底地跨过风控窗口
_RESCUE_ROUNDS = 3
_RESCUE_COOLDOWNS = (60.0, 120.0, 300.0)

# v9.8 agent 自愈重启:检索整体失败(会话风控/网络故障等环境性原因)后,
# 冷却期间重置会话状态,自动重启整个检索 —— 与补漏(式子级,跑在同一会话里)
# 的本质区别:整体重启会更换会话凭证,针对的是会话/网络级的风控封锁。
# 非环境性故障(知网改版/超级鹰题分耗尽/用户停止)重启无意义,立即上抛。
_AUTO_RESTART_ROUNDS = 2
_AUTO_RESTART_COOLDOWNS = (120.0, 300.0)


def _run_with_auto_restart(
    inner,
    *,
    rounds: int = _AUTO_RESTART_ROUNDS,
    cooldowns: tuple[float, ...] = _AUTO_RESTART_COOLDOWNS,
    emit_fn,
    sleep_fn,
    check_stopped,
    before_restart,
):
    """agent 自愈重启循环:检索整体失败 → 冷却 → 重置会话 → 自动重启。

    - inner(): 无参回调,跑一次完整检索(列表+详情+入库),幂等可重入;
    - rounds: 额外重启次数(总尝试 = 1 + rounds);
    - 冷却分段睡眠,面板「停止」秒级响应(与补漏轮同一机制);
    - before_restart(attempt): 重启前的会话重置钩子(清零空壳连击/换 cookie);
    - 环境性故障(会话风控/网络/限流)重启,闸口故障(改版/题分耗尽/用户停止)
      立即上抛;重启耗尽后抛最后一次异常,由外层以 error 终态如实上报。
    纯调度逻辑:inner/emit/sleep/stop 钩子均可注入,离线确定性单测。
    """
    attempt = 0
    while True:
        attempt += 1
        try:
            return inner()
        except _CnkiStopped:
            raise  # 用户停止:任何重启都无意义
        except (crawler.CnkiRevisionError, crawler.CnkiCaptchaBalanceError,
                crawler.CnkiCookieError):
            raise  # 闸口故障:需要人工介入(反馈开发者/充值/检查网络),重启只会重复失败
        except Exception as exc:
            if attempt > rounds:
                raise  # 重启预算耗尽:如实上抛最后一次异常
            cooldown = cooldowns[min(attempt - 1, len(cooldowns) - 1)]
            emit_fn(
                f"[自愈] 第 {attempt} 次尝试失败({exc}),"
                f"冷却 {cooldown:.0f}s 后自动重启检索"
                f"(第 {attempt + 1}/{rounds + 1} 次,可随时停止)"
            )
            _sleep_in_chunks(sleep_fn, check_stopped, cooldown)
            before_restart(attempt)


def _run_with_rescue(
    queries: list[str],
    fetcher,
    *,
    emit_fn,
    sleep_fn,
    check_stopped,
    delay_seconds: float,
    target_count: int,
    rescue_rounds: int = _RESCUE_ROUNDS,
    rescue_cooldowns: tuple[float, ...] = _RESCUE_COOLDOWNS,
) -> tuple[dict[str, dict], list[str]]:
    """首轮顺序抓取 + 失败式子多轮补漏调度(产品级「不丢单」语义)。

    背景(2026-09 用户反馈):旧预检循环里式子命中限流空壳就 continue 换下一条、
    连续 3 条失败直接中止——限流期跑完用户拿到的是「静默缺失」甚至 0 篇。
    现语义:失败式子一律记入补漏队列,首轮跑完后按轮冷却(60/120/300s)重试,
    直到全部补回/达标/轮数耗尽;耗尽时返回缺口清单由调用方如实报告。

    fetcher(query) -> list[dict]:单式抓取+清洗(剔学位论文/降级守门由闭包负责);
      - CnkiServerBusyError/一般 Exception → 本式失败,进补漏队列(不丢单)
      - 闸口故障(CnkiCookieError/CnkiCaptchaBalanceError/CnkiRevisionError/
        CnkiSessionBlockedError)→ 自动处置已穷尽,原样上抛交外层 error 终态处置
    返回 (merged: 去重条目表(键=_dedupe_key(url)), missing: 补漏后仍失败的式子)。
    纯调度逻辑:sleep_fn/check_stopped/fetcher 均可注入,可离线确定性单测。
    """
    merged: dict[str, dict] = {}
    missing: list[str] = []
    delay = float(delay_seconds)

    def absorb(found: list[dict]) -> int:
        before = len(merged)
        for item in found:
            url = item.get("url") or ""
            if url:
                merged.setdefault(_dedupe_key(url), item)
        return len(merged) - before

    # ---- 首轮:顺序尝试全部式子 ----
    busy_streak = 0  # 连续命中空壳的式子数(判据:限流窗口已成型,暂停首轮硬撞)
    aborted_by_busy = False
    last_index = 0
    for index, query in enumerate(queries, start=1):
        last_index = index
        check_stopped()
        # 式子之间加间隔,背靠背提交是触发知网限流/风控的典型诱因
        if index > 1:
            sleep_fn(delay)
        emit_fn(f"[检索] {index}/{len(queries)} 提交: {query}")
        try:
            found = fetcher(query)
        except CnkiServerBusyError as exc:
            missing.append(query)
            busy_streak += 1
            emit_fn(f"[检索] {index}/{len(queries)} 数据源暂时繁忙，已记入待补全队列（待补 {len(missing)} 条）")
            if busy_streak >= 3:
                aborted_by_busy = True
                emit_fn("[检索] 数据源暂时繁忙，首轮已暂停，未完成的检索已自动转入补全队列")
                break
            # 冷却 30s 让知网限流窗口衰减(连续短退避只会加重风控)
            emit_fn("[检索] 数据源繁忙，休息 30s 后继续")
            sleep_fn(30.0)
        except (crawler.CnkiCookieError, crawler.CnkiCaptchaBalanceError,
                crawler.CnkiRevisionError, crawler.CnkiSessionBlockedError):
            raise  # 闸口故障:自动处置已穷尽,上抛交外层以 error 终态推前端横幅
        except Exception as exc:
            # 偶发异常(网络抖动等)同样不丢单:进补漏队列
            missing.append(query)
            emit_fn(f"[检索] {index}/{len(queries)} 本次检索未成功，已记入待补全队列（待补 {len(missing)} 条）")
        else:
            busy_streak = 0  # 本条式子请求层正常(即使 0 结果),解除连续异常计数
            added = absorb(found)
            emit_fn(f"[检索] {index}/{len(queries)} 已获取 {len(found)} 条，新增 {added} 条，累计 {len(merged)} 条")
            if len(merged) >= target_count:
                emit_fn(f"[检索] 已达目标篇数（{target_count} 篇），停止后续检索")
                missing.clear()  # 达标后缺口无意义
                return merged, missing
    if aborted_by_busy:
        # 首轮提前中止:未跑到的式子也进入补漏队列,一个不丢
        missing.extend(queries[last_index:])

    # ---- 补漏:轮间冷却逐级拉长,跨过知网分钟级限流窗口 ----
    for round_index in range(rescue_rounds):
        if not missing:
            break
        cooldown = rescue_cooldowns[min(round_index, len(rescue_cooldowns) - 1)]
        emit_fn(f"[补全] 第 {round_index + 1}/{rescue_rounds} 轮：待补 {len(missing)} 条检索式，"
                f"稍后自动开始（可随时停止）")
        _sleep_in_chunks(sleep_fn, check_stopped, cooldown)
        still_missing: list[str] = []
        for order, query in enumerate(missing, start=1):
            check_stopped()
            if order > 1:
                sleep_fn(delay)
            emit_fn(f"[补全] 第 {round_index + 1} 轮 {order}/{len(missing)}：重试 {query}")
            try:
                found = fetcher(query)
            except CnkiServerBusyError as exc:
                still_missing.append(query)
                emit_fn("[补全] 数据源仍繁忙，该检索式留在补全队列")
            except (crawler.CnkiCookieError, crawler.CnkiCaptchaBalanceError,
                    crawler.CnkiRevisionError, crawler.CnkiSessionBlockedError):
                raise
            except Exception as exc:
                still_missing.append(query)
                emit_fn("[补全] 本次重试未成功，留待下轮")
            else:
                added = absorb(found)
                emit_fn(f"[补全] 补全成功：新增 {added} 条，累计 {len(merged)} 条")
                if len(merged) >= target_count:
                    emit_fn(f"[补全] 已达目标篇数（{target_count} 篇），停止补全")
                    still_missing.clear()
                    break
        missing = still_missing
    return merged, missing


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
    pool_task_id: str | None = None,
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
    # (类本体已提升到模块级:v9.8 自愈重启调度器是模块级函数,
    #  闭包类会造成 NameError;单类也修正了每次调用创建新异常类的隐患)
    def _check_stopped():
        # v9.6:同时检查 registry 的取消事件——调用方不传 stop_event 时
        # (retrieval/sources/cnki.py、writing/orchestrator.py),面板
        # POST /crawler/tasks/{id}/stop 置位的 state.cancel_event 此前无人
        # 轮询,爬虫只能重启进程才能停
        if (stop_event is not None and stop_event.is_set()) or state.cancel_event.is_set():
            raise _CnkiStopped("用户已手动停止")

    # 监控任务登记（Q-1 修复②）:注册提前到检索式预检之前,
    # 预检失败的任务也在面板留痕(failed 终态),杜绝「start 返回 running
    # 但面板永远无此任务」的静默丢失
    registry = _monitor.get_registry()
    task_id = pool_task_id or f"cnki-{time.strftime('%Y%m%d%H%M%S')}"
    state = registry.register(task_id, query=topic, params={
        "target_count": target_count,
        "max_pages": max_pages,
        "delay_seconds": float(crawler.CONFIG["runtime"]["delay_seconds"]),
    })
    # 桥接:监控面板/运维接口的取消事件与业务侧 stop_event 合一,
    # registry.stop_task 与 api 层手动停止任一置位都能协作式退出
    if stop_event is not None:
        state.cancel_event = stop_event

    raw_queries = [query.strip() for query in (expert_queries or []) if query.strip()]
    if not raw_queries and expert_query and expert_query.strip():
        raw_queries = [expert_query.strip()]
    if raw_queries:
        try:
            queries = [normalize_cnki_query(query) for query in raw_queries]
        except ValueError as exc:
            emit(stage="error", msg=f"知网检索式语法无效,任务未提交: {exc}", db=db_type)
            # 预检失败同样在面板落 failed 终态(与 API 层 422 预检互为双保险:
            # API 层拦住已知非法式,这里兜住直连 adapter 的调用方)
            try:
                registry.mark_failed(task_id, f"检索式语法无效: {exc}")
            except Exception:
                log.exception("[cnki] 预检失败终态登记异常")
            return {"status": "failed", "saved": 0, "skipped": 0, "error": str(exc)}
    else:
        queries = [topic]
    # v8.4:检索式落盘取证(plan_generated 只推 SSE 不落库,降级事故无法事后追查式子原文)
    _dump_plan_queries(queries, topic)

    # max_pages<=0 视为"翻到知网无结果为止";否则按页数计算 max_count
    page_size = int(crawler.CONFIG["search"]["page_size"] or 20)
    if max_pages is None or max_pages <= 0:
        max_count = None
        emit(stage="plan_generated", queries=queries, db=db_type, unbounded=True)
    else:
        max_count = max_pages * page_size
        emit(stage="plan_generated", queries=queries, db=db_type, max_count=max_count)
    emit(stage="search_submitted", db=db_type)
    # v9.7:新任务清零会话级空壳连击 —— 上一任务的风控状态不带入本任务
    crawler.reset_session_block_streak()
    # 把本次预计抓取上限打到日志面板,避免翻页数与用户预期对不上
    if max_count is None:
        emit(stage="log", msg=f"[计划] 共 {len(queries)} 条候选检索式,目标至少 {target_count} 篇,翻页上限=无", db=db_type)
    else:
        emit(stage="log", msg=f"[计划] 共 {len(queries)} 条候选检索式,目标至少 {target_count} 篇,每式最多 {max_pages} 页", db=db_type)

    def _sync_run() -> tuple[int, int]:
        """同步阻塞:列表 + 逐条摘要 + 入库,返回 (saved, skipped)。

        挂接爬虫的过程日志回调,把翻页/验证码/抓取进度实时推给前端 SSE;
        监控任务已在函数入口注册(Q-1 修复②),这里只推进 running 态与终态。
        v9.8:外层 agent 自愈重启循环 —— 检索整体失败(会话风控/网络故障)
        时冷却后重置会话自动重启;终态登记仍在 finally,保证只登记一次
        (重启期间任务保持 running,面板可见「自愈等待/自愈重启」阶段)。
        """
        registry.mark_running(task_id, stage="预检")
        # 绑定任务线程:crawler.safe_request 的请求记账/环形日志归属本任务
        crawler.set_current_task(task_id)
        crawler.set_log_callback(lambda m: emit(stage="log", msg=m, db=db_type))
        outcome = "done"
        error_msg = ""

        def _before_restart(attempt: int) -> None:
            # 重启前重置会话状态:清零空壳连击 + 更换会话凭证;
            # 配置代理时出口由代理池自动轮换,无需额外处理
            crawler.reset_session_block_streak()
            try:
                crawler.refresh_cookies("自愈重启前更换会话")
            except Exception:
                log.warning("[cnki] 自愈重启前 cookie 续期失败,沿用当前会话")
            progress["attempts"] = attempt + 1
            try:
                registry.mark_stage(task_id, "自愈重启")
            except Exception:
                pass

        try:
            return _run_with_auto_restart(
                _run_sync_inner,
                emit_fn=lambda msg: emit(stage="log", msg=msg, db=db_type),
                sleep_fn=crawler.sleep_jitter,
                check_stopped=_check_stopped,
                before_restart=_before_restart,
            )
        except _CnkiStopped as exc:
            outcome, error_msg = "stopped", str(exc)
            raise
        except Exception as exc:
            outcome, error_msg = "failed", str(exc)
            raise
        finally:
            crawler.set_log_callback(None)
            crawler.set_current_task(None)
            try:
                if outcome == "done":
                    registry.mark_done(task_id)
                elif outcome == "stopped":
                    registry.mark_stopped(task_id)
                else:
                    registry.mark_failed(task_id, error_msg)
            except Exception:
                log.exception("[cnki] 监控任务终态登记失败")

    def _run_sync_inner() -> tuple[int, int]:
        per_query_count = target_count if max_count is None else min(max_count, target_count)

        def fetcher(query: str) -> list[dict]:
            query_json = (
                crawler.build_expert_query(expert_str=query)
                if query.startswith(("SU=", "TI=", "KY=", "AB=", "FT="))
                else crawler.build_query(keyword=query)
            )
            found = crawler.fetch_all_list(query_json=query_json, max_count=per_query_count)
            # v8.7:总库混入学位论文,先剔除(只留期刊)
            found = _strip_thesis_items(found, emit, db_type)
            # v8.4 守门:知网静默降级(检索式未生效)时整批弃用,交调度器入补漏队列
            return _gate_list_items(query, found, db_type, emit)

        # 检索调度:失败式子入补漏队列,多轮冷却重试,不静默丢单(v9.3 产品级补漏)
        try:
            merged, missing = _run_with_rescue(
                queries,
                fetcher,
                emit_fn=lambda msg: emit(stage="log", msg=msg, db=db_type),
                sleep_fn=crawler.sleep_jitter,
                check_stopped=_check_stopped,
                delay_seconds=crawler.CONFIG["runtime"]["delay_seconds"],
                target_count=target_count,
            )
        except (crawler.CnkiCookieError, crawler.CnkiCaptchaBalanceError,
                crawler.CnkiRevisionError, crawler.CnkiSessionBlockedError) as exc:
            # v9 闸口故障(cookie 续期无效/超级鹰余额/知网改版/会话级风控):
            # 自动处置已穷尽,直接上抛,外层以 error 终态推前端横幅
            log.error("[cnki] 检索命中闸口故障,停止: %s", exc)
            raise
        if missing:
            emit(stage="log", msg=f"[缺口] {len(missing)} 条检索式经 {_RESCUE_ROUNDS} 轮自动补全仍未获取"
                 f": {'; '.join(missing)}", db=db_type)
        items = list(merged.values())[:target_count]
        emit(stage="search_done", ok=bool(items), total=len(items), db=db_type)
        if not items:
            # 产品级:0 篇入库必须给前端明确的失败终态,
            # 否则前端会把任务停在 active → 进度条显示「0 篇 已完成」
            emit(
                stage="error",
                msg="未获取到文献：数据源暂时不可用（可能受访问限制），"
                "请稍后重试，或适当调低目标篇数",
                db=db_type,
            )
            return 0, 0
        # 需求3:入库前清空同源历史,避免文献池累积多次检索的旧数据。
        # v9.6:清空动作延迟到「首批新数据即将入库」时执行(见下方块循环),
        # 详情抓取中途致命失败(GB/T API 故障/闸口熔断/手动停止)时,
        # 旧数据保持完好——不会出现「旧数据已删、新数据只有一半」的不可恢复丢失。
        saved = skipped = 0
        _cleared_once = False
        total = len(items)
        valid_items = [
            (idx, it) for idx, it in enumerate(items, start=1) if (it.get("url") or "").strip()
        ]
        skipped += len(items) - len(valid_items)

        def _fetch_one(url: str) -> dict:
            """工作线程:单篇摘要抓取。停止检查 + 抖动延迟,压低并发请求的节奏规律性。

            线程内绑定任务 ID:crawler.safe_request 的请求记账/环形日志归属本任务。
            """
            crawler.set_current_task(task_id)
            try:
                _check_stopped()
                time.sleep(random.uniform(0.3, 0.8))
                return crawler.fetch_abstract(url)
            finally:
                crawler.set_current_task(None)

        # 摘要阶段:动态并发池(scheduler.DynamicWorkerPool)驱动,分块消费。
        # - 块内并发度由池按滚动窗口延迟/失败率/风控信号自适应伸缩(min~max),
        #   命中验证码/限流立即收缩,替代原固定 3 线程模型(需求4);
        # - 块间串行保证:停止请求及时生效、进度条平滑推进、失败不跨块扩散。
        pool = _scheduler.get_pool() or _scheduler.configure_pool(crawler.CONFIG["runtime"])
        # v9.6:缺省时复用 registry 的取消事件——此前新建永不置位的 Event,
        # 面板停止对动态池批间中断完全无效(与 _check_stopped 同一断链)
        stop_flag = stop_event if stop_event is not None else state.cancel_event
        chunk_size = max(DETAIL_CONCURRENCY * 2, pool.max_workers * 2)
        registry.mark_stage(task_id, "摘要抓取")
        for chunk_start in range(0, len(valid_items), chunk_size):
            _check_stopped()
            chunk = valid_items[chunk_start:chunk_start + chunk_size]
            # 动态池消费本块:按期望并发度分批提交、批间重算并发度;
            # stop_flag 置位后批间中断(未开始的不提交),结果与输入同序(进度不跳号)
            outcomes = pool.run_items(
                lambda pair: _fetch_one(pair[1]["url"]),
                chunk,
                stop_event=stop_flag,
            )
            # 按提交顺序收结果,异常分类与原语义一致:
            # GB/T 缺失→跳过 / GB/T API 故障→停整次 / 手动停止→上抛 /
            # 闸口故障(cookie/打码/改版)→停整次 / 其他(SSL/超时/404)→跳过
            parsed: list[tuple[int, dict, dict]] = []
            for (idx, it), outcome in zip(chunk, outcomes):
                url = it.get("url") or ""
                if outcome is None:
                    # 池在批间被 stop_flag 中断,本条未执行 → 统一走停止出口
                    _check_stopped()
                    raise _CnkiStopped("用户已手动停止")
                _pair, detail, exc = outcome
                if exc is not None:
                    if isinstance(exc, crawler.CnkiGBTCitationMissing):
                        # 单篇极个别:详情页缺少 GB/T 7714 hidden input / 导出 API 无 GB/T 条目
                        # 跳这 paper,不阻塞整次
                        log.warning("[cnki] %d/%d 跳过(无 GB/T 7714): %s | %s", idx, total, exc, url[:80])
                        emit(stage="log",
                             msg=f"[摘要] {idx}/{total} 跳过,GB/T 7714 引文不可用(极个别): {exc}",
                             db=db_type)
                        skipped += 1
                        continue
                    if isinstance(exc, crawler.CnkiGBTCitationAPIFailed):
                        # 整次停:导出 API 本身挂了(网络/超时/限流/cookie 失效)
                        # 后续 paper 大概率也拿不到,直接告诉用户
                        log.error("[cnki] %d/%d GB/T 7714 导出 API 失败,停止整次: %s | %s",
                                  idx, total, exc, url[:80])
                        emit(stage="log",
                             msg=f"[摘要] {idx}/{total} 致命错误,停止整次: {exc}",
                             db=db_type)
                        raise exc
                    if isinstance(exc, _CnkiStopped):
                        raise exc
                    if isinstance(exc, (crawler.CnkiCookieError, crawler.CnkiCaptchaBalanceError,
                                        crawler.CnkiRevisionError)):
                        # v9 闸口故障:自动处置已穷尽(cookie 续期/打码熔断/改版哨兵),
                        # 继续烧完只会条条失败 → 上抛,外层统一以 error 终态推前端横幅
                        log.error("[cnki] %d/%d 闸口故障,停止整次: %s | %s", idx, total, exc, url[:80])
                        emit(stage="log", msg=f"[摘要] {idx}/{total} 闸口故障,停止整次: {exc}", db=db_type)
                        raise exc
                    # 其他错误(SSL/超时/详情页 404 等)→ 老逻辑:跳过
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
                try:
                    record = _detail_to_record(detail, db_type)
                except Exception as exc:
                    # 摘要解析或 paper identity 校验失败(缺作者/年等)→ 单篇跳过
                    registry.incr(task_id, "parse_errors")
                    log.warning("[cnki] 摘要解析失败 %s: %s", url[:80], exc)
                    emit(stage="log",
                         msg=f"[摘要] {idx}/{total} 解析失败,已跳过: {exc}",
                         db=db_type)
                    skipped += 1
                    continue
                # v8.3:报纸类条目(dbcode=CCND 或 GB/T 引文带 [N])不是学术论文,
                # 混入会拉低引用质量,过滤并记日志。
                if _is_news_item(record, url):
                    log.info("[cnki] %d/%d 过滤报纸类条目: %s", idx, total,
                             (record.get("title") or "")[:50])
                    emit(stage="log",
                         msg=(f"[摘要] {idx}/{total} 已过滤报纸类条目: "
                              f"{(record.get('title') or '')[:50]}"),
                         db=db_type)
                    skipped += 1
                    continue
                # v8.7:学位论文(硕士 CMFD/博士 CDFD)不入综述文献池,一律剔除。
                # 列表层已过滤,这里是详情级兜底(凭引文 [D] 标识双保险)。
                if _is_thesis_item(record, url):
                    log.info("[cnki] %d/%d 过滤学位论文: %s", idx, total,
                             (record.get("title") or "")[:50])
                    emit(stage="log",
                         msg=(f"[摘要] {idx}/{total} 已过滤学位论文(仅收期刊): "
                              f"{(record.get('title') or '')[:50]}"),
                         db=db_type)
                    skipped += 1
                    continue
                # 数据质量管线(需求3):字段标准化 + 缺失补全 + 只读评分。
                # record 就地清洗;flags 仅记账面板聚合,不写入 record(入库模型硬约束)
                record, q_report = _quality.quality_pipeline(record)
                registry.add_quality_report(task_id, q_report)
                parsed.append((idx, it, record))
            # v9.6:首批解析成功的记录就绪、即将入库时才清空同源旧数据
            # (先清后写,与原语义一致;此前清空提前到抓取前,中途致命失败
            # 会造成旧数据已删、新数据只有一半的不可恢复丢失)
            if not _cleared_once and parsed:
                _cleared_once = True
                cleared = _clear_source_records(db_type, pool_task_id)
                if cleared:
                    emit(stage="log",
                         msg=f"[入库] 已清空同源旧数据 {cleared} 篇,本次将覆盖写入",
                         db=db_type)
            # 本块解析成功的记录批量入库(单 session 单 commit)。
            # 入库失败只跳过本块,绝不杀整次任务:
            # 曾因一次 commit 异常直接逃出外层 except,数分钟抓取成果
            # 全部丢弃(saved 归 0)且前端无任何入库失败日志。
            try:
                ok_flags = (
                    _persist_records([record for _, _, record in parsed], pool_task_id)
                    if parsed else []
                )
            except Exception as exc:
                log.exception(
                    "[cnki] 块入库失败 %d-%d/%d",
                    chunk_start + 1, chunk_start + len(chunk), total,
                )
                emit(stage="log",
                     msg=(f"[入库] 第 {chunk_start + 1}-{chunk_start + len(chunk)}/{total} "
                          f"条块写入失败,已跳过 {len(parsed)} 篇: {exc}"),
                     db=db_type)
                ok_flags = [False] * len(parsed)
            # 按序补发进度事件与题录日志(saved 已在批量入库后确定)
            for (idx, _it, record), ok in zip(parsed, ok_flags):
                if ok:
                    saved += 1
                    registry.incr(task_id, "saved_total")
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
            # 块结束:同步真实进度到监控面板,异常退出时 except 分支可上报已入库数
            progress["saved"] = saved
            progress["skipped"] = skipped
            registry.update_progress(task_id, saved=saved, skipped=skipped)
        return saved, skipped

    # 真实进度(闭包共享):异常退出时拿不到 _sync_run 内部局部变量,
    # 用它上报「失败前已入库多少篇」,前端面板不再显示误导性的 0 篇。
    # attempts: 含自愈重启的总尝试次数,失败消息如实告知用户已自动重启过几轮。
    progress = {"saved": 0, "skipped": 0, "attempts": 1}

    def _failure_tail() -> str:
        tail = f"(本次已入库 {progress['saved']} 篇"
        if progress["attempts"] > 1:
            tail += f",已自动重启 {progress['attempts'] - 1} 次仍失败"
        return tail + ")"

    try:
        saved, skipped = await asyncio.to_thread(_sync_run)
    except _CnkiStopped as exc:
        # 用户手动停止:置位标志后循环抛错退出
        emit(stage="error", msg=f"{exc}{_failure_tail()}", db=db_type)
        return {
            "status": "failed",
            "saved": progress["saved"],
            "skipped": progress["skipped"],
            "reason": str(exc),
        }
    except Exception as exc:
        log.exception("cnki 爬虫异常")
        emit(stage="error", msg=f"{exc}{_failure_tail()}", db=db_type)
        return {
            "status": "failed",
            "saved": progress["saved"],
            "skipped": progress["skipped"],
            "reason": str(exc),
        }

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
    """调用爬虫 check_cookies() 探测登录态是否可用(不触发搜索/不扣题分)。"""
    try:
        ok = crawler.check_cookies()
        return {"ok": bool(ok),
                "detail": "登录状态正常" if ok else "登录状态无效或已过期，请刷新登录状态后重试"}
    except Exception as exc:
        log.warning("cookie 健康检查异常: %s", exc)
        return {"ok": False, "detail": "登录状态检查失败，请稍后重试"}
