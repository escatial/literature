"""PubMed 数据源实现(NCBI E-utilities)。

- esearch.fcgi 拿 PMID 列表
- esummary.fcgi 批量取基础元数据(无摘要)
- efetch.fcgi 批量补摘要(一次最多 200 个 PMID,摘要不在 esummary 里)
- 失败退避重试
"""
from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
import xml.etree.ElementTree as ET

import httpx

from retrieval.sources.base import AcademicSource, SourcePage
from retrieval.types import Paper, Source

log = logging.getLogger(__name__)

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

# 硬编码的硬筛选条件(替代原 SearchIntent.filters)
DEFAULT_PUBMED_YEAR_BACK = 5
DEFAULT_PUBMED_LANGS = ["eng"]
DEFAULT_PUBMED_TYPES = ["journal article", "review"]

# efetch 批量回填:每次最多 200 个 PMID(官方建议上限);批间隔 0.4s,
# 对齐 NCBI 无 API key 时 3 req/s 的限速要求。
EFETCH_BATCH_SIZE = 200
EFETCH_CHUNK_PAUSE = 0.4

# 模块级共享 httpx.Client:复用 TCP+TLS 连接,省掉每请求重新握手。
# httpx.Client 线程安全;超时按次覆盖(per-request timeout)。
_shared_client: httpx.Client | None = None
_client_lock = threading.Lock()


def _get_http_client() -> httpx.Client:
    global _shared_client
    if _shared_client is None:
        with _client_lock:
            if _shared_client is None:
                _shared_client = httpx.Client(timeout=30.0)
    return _shared_client


# NCBI 无 API key 全局限 3 req/s;检索式级并发(query_concurrency=2)会让瞬时
# 速率翻倍,必须全局节流兜底,否则 esummary 首页就会撞 429(实测复现)。
# 持锁 sleep 的串行化正是目的:全进程任意两次 PubMed HTTP 请求间隔 >= 0.4s。
_PUBMED_MIN_INTERVAL = 0.4
_pubmed_last_request = 0.0
_pubmed_pace_lock = threading.Lock()


def _pace_pubmed() -> None:
    """全局节流:任意两次 NCBI 请求间隔不小于 0.4s(≈2.5 req/s < 3 req/s 上限)。"""
    global _pubmed_last_request
    with _pubmed_pace_lock:
        now = time.monotonic()
        wait = _pubmed_last_request + _PUBMED_MIN_INTERVAL - now
        if wait > 0:
            time.sleep(wait)
            now = time.monotonic()
        _pubmed_last_request = now


def _pubmed_cooldown(seconds: float = 3.0) -> None:
    """撞 429 后全局冷却:把下一次允许请求的时间推向未来。

    后续所有 _pace_pubmed 会自动等到该时刻,避免连锁限流;
    NCBI 对同一 IP 有分钟级惩罚窗口,冷却叠加可逐步拉开间隔。
    """
    global _pubmed_last_request
    with _pubmed_pace_lock:
        floor = time.monotonic() + seconds
        if _pubmed_last_request < floor:
            _pubmed_last_request = floor


def _is_429(e: Exception) -> bool:
    """判断异常是否为 NCBI 429 Too Many Requests。"""
    return isinstance(e, httpx.HTTPStatusError) and e.response.status_code == 429


_PMID_URL_RE = re.compile(r"pubmed\.ncbi\.nlm\.nih\.gov/(\d+)")


def _pmid_of(paper: Paper) -> str:
    """从 source_url 提取真实 PMID。

    lit_id 已改为 build_lit_id 身份哈希,尾巴不再是 PMID;
    source_url 是平台原始返回(https://pubmed.ncbi.nlm.nih.gov/<uid>/),
    始终携带真实 uid。NCBI 对无效 ID 不报错,会静默返回无关文献
    (实测假哈希被解析成 PMID=5 的 1976 年文章),必须用真 PMID。
    """
    m = _PMID_URL_RE.search(paper.source_url or "")
    return m.group(1) if m else ""


class PubMedSource:
    name = "pubmed"

    def __init__(self, timeout: float = 30.0, mailto: str | None = None):
        self.timeout = timeout
        self.mailto = mailto  # NCBI 鼓励提供,用于联系滥用

    # === AcademicSource 协议 ===

    def build_query(self, intent) -> dict:
        """兼容旧 AcademicSource 协议(传 SearchIntent 时取主检索式)。

        新链路直接走 build_sub_query(query_string)。
        """
        boolean = (getattr(intent, "boolean_template", "") or "").strip() or ""
        return self._build_query_from_string(boolean)

    # v9.6:year_start/year_end 贯通——任务级年份窗口此前被静默替换为「近 N 年」
    def build_sub_query(self, query_string: str, year_start: int | None = None,
                        year_end: int | None = None) -> dict:
        """把 LLM 直接输出的 PubMed 检索式字符串组装成 E-utilities 请求。

        query_string: LLM 直接输出的完整 PubMed 检索式,如
          ("Understanding by Design"[tiab] OR UbD[tiab]) AND "math teaching"[tiab]
        year_start/year_end: 任务级发表年份窗口([dp] 限定);单边缺省时
          另一边回退默认(end=当前年,start=当年-DEFAULT_PUBMED_YEAR_BACK)。
        """
        return self._build_query_from_string(boolean=query_string,
                                             year_start=year_start, year_end=year_end)

    def _build_query_from_string(self, boolean: str, year_start: int | None = None,
                                 year_end: int | None = None) -> dict:
        """把布尔主体 + 默认年份/语言/类型组装成 PubMed E-utilities term。"""
        import datetime as _dt

        year = _dt.datetime.now().year
        ys = int(year_start) if year_start else year - DEFAULT_PUBMED_YEAR_BACK
        ye = int(year_end) if year_end else year
        clauses = [f"({boolean})"]
        clauses.append(f"{ys}:{ye}[dp]")
        # 一篇文章只属于一种语言:多语言必须 OR 分组
        clauses.append("(" + " OR ".join(f"{lang}[la]" for lang in DEFAULT_PUBMED_LANGS) + ")")
        # 同理,一篇文章不可能同时是 journal article 和 review
        clauses.append("(" + " OR ".join(f"{t}[pt]" for t in DEFAULT_PUBMED_TYPES) + ")")
        return {"term": " AND ".join(clauses), "tool": self.mailto or "lit-review-agent"}

    def execute(self, query: dict, page: int, per_page: int) -> SourcePage:
        retmax = min(per_page, 200)
        retstart = (page - 1) * retmax
        try:
            client = _get_http_client()
            # 第一步:拿 PMID 列表(全局节流,防并发撞 NCBI 429)
            _pace_pubmed()
            search_resp = client.get(
                f"{EUTILS}/esearch.fcgi",
                params={
                    "db": "pubmed", "term": query["term"],
                    "retmode": "json", "retmax": retmax, "retstart": retstart,
                    **({"tool": query["tool"]} if query.get("tool") else {}),
                },
                timeout=self.timeout,
            )
            search_resp.raise_for_status()
            es = search_resp.json().get("esearchresult", {})
            ids = es.get("idlist", []) or []
            total = int(es.get("count", 0))
            if not ids:
                return SourcePage(papers=[], total=total, has_next=False,
                                  page=page, raw_query=query)
            # 第二步:拿摘要(元数据 + 摘要一起拿)
            _pace_pubmed()
            sum_resp = client.get(
                f"{EUTILS}/esummary.fcgi",
                params={"db": "pubmed", "id": ",".join(ids), "retmode": "json"},
                timeout=self.timeout,
            )
            sum_resp.raise_for_status()
            result = sum_resp.json().get("result", {})
            papers = [self._parse(uid, result.get(uid) or {}) for uid in ids if result.get(uid)]
            # esummary 不含 abstract;缺摘要的条目由控制器的
            # pool.fill_missing_async 统一走 efetch 批量回填。
            has_next = retstart + len(papers) < total
            return SourcePage(papers=papers, total=total, has_next=has_next,
                              page=page, raw_query=query)
        except Exception as e:
            if _is_429(e):
                # 全局冷却,给后续请求(含并发的另一个检索式)留出生存空间
                _pubmed_cooldown(3.0)
                log.warning("PubMed 第 %d 页撞 429,全局冷却 3s", page)
            else:
                log.warning("PubMed 第 %d 页失败: %s", page, e)
            return SourcePage(papers=[], total=0, has_next=False, page=page, raw_query=query)

    def fetch_abstract_if_missing(self, paper: Paper) -> Paper | None:
        """通过 efetch.fcgi 取单条 PubMed 记录的 XML 摘要(批量路径的兜底)。"""
        if paper.abstract:
            return paper
        if not paper.lit_id.startswith("lit_pubmed_"):
            return None
        pmid = _pmid_of(paper)
        if not pmid:
            return None
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                _pace_pubmed()
                resp = _get_http_client().get(
                    f"{EUTILS}/efetch.fcgi",
                    params={"db": "pubmed", "id": pmid, "retmode": "xml"},
                    timeout=self.timeout,
                )
                resp.raise_for_status()
                abstract = self._extract_abstract_from_xml(resp.text)
                if abstract:
                    paper.abstract = abstract
                return paper
            except (httpx.ConnectTimeout, httpx.ReadTimeout) as e:
                last_err = e
                time.sleep(2 ** attempt)
            except Exception as e:
                log.warning("PubMed efetch %s 失败: %s", pmid, e)
                return None
        log.warning("PubMed efetch %s 全部重试失败: %s", pmid, last_err)
        return None

    def fetch_abstracts_batch(self, papers: list[Paper]) -> dict[str, str]:
        """批量 efetch 回填摘要:一次请求最多带 200 个 PMID。

        替代逐条单请求(400 篇缺摘要 = 400 次 HTTP),批量后仅需 2 次请求。
        返回 {lit_id: abstract};单批失败记日志跳过(回填是尽力而为)。
        pool.fill_missing_async 检测到本方法时自动切到批量路径。
        """
        pmid_to_lit: dict[str, str] = {}
        for p in papers:
            if p.abstract or not p.lit_id.startswith("lit_pubmed_"):
                continue
            pmid = _pmid_of(p)
            if pmid:
                pmid_to_lit[pmid] = p.lit_id
        out: dict[str, str] = {}
        pmids = list(pmid_to_lit)
        for i in range(0, len(pmids), EFETCH_BATCH_SIZE):
            chunk = pmids[i:i + EFETCH_BATCH_SIZE]
            for attempt in range(2):
                try:
                    _pace_pubmed()
                    resp = _get_http_client().get(
                        f"{EUTILS}/efetch.fcgi",
                        params={"db": "pubmed", "id": ",".join(chunk), "retmode": "xml"},
                        timeout=self.timeout,
                    )
                    resp.raise_for_status()
                    got = self._extract_abstracts_from_batch_xml(resp.text)
                    if not got:
                        # HTTP 200 但 0 篇摘要:NCBI 惩罚窗口内的软限流常返回空集,
                        # 必须当失败重试,否则静默 0 补齐(实测复现)。
                        log.warning("PubMed 批量 efetch(第 %d 批)第 %d 次返回 200 但解析 0 篇(疑似软限流)",
                                    i // EFETCH_BATCH_SIZE + 1, attempt + 1)
                        _pubmed_cooldown(3.0)
                        time.sleep(2 + attempt * 3)
                        continue
                    for pmid, abstract in got.items():
                        lit_id = pmid_to_lit.get(pmid)
                        if lit_id and abstract:
                            out[lit_id] = abstract
                    break
                except Exception as e:
                    if _is_429(e):
                        _pubmed_cooldown(3.0)
                        log.warning("PubMed 批量 efetch(第 %d 批)撞 429,全局冷却 3s",
                                    i // EFETCH_BATCH_SIZE + 1)
                    else:
                        log.warning("PubMed 批量 efetch(第 %d 批,%d 个 PMID)第 %d 次失败: %s",
                                    i // EFETCH_BATCH_SIZE + 1, len(chunk), attempt + 1, e)
                    time.sleep(1 + attempt)
            # 批间隔限速保护(NCBI 无 key 3 req/s)
            if i + EFETCH_BATCH_SIZE < len(pmids):
                time.sleep(EFETCH_CHUNK_PAUSE)
        filled = len(out)
        log.info("PubMed 批量摘要回填:请求 %d 批,补齐 %d/%d 篇",
                 (len(pmids) + EFETCH_BATCH_SIZE - 1) // EFETCH_BATCH_SIZE,
                 filled, len(pmids))
        return out

    def fetch_references(self, paper: Paper, depth: int = 1) -> list[Paper]:
        """PubMed 引用关系较弱(没有直接 references 端点);
        退化为:用 paper.title 在 PubMed 里反查同标题的引用。
        实际生产建议走 OpenAlex 的 references 路径。"""
        return []

    def health_check(self) -> bool:
        try:
            resp = _get_http_client().get(f"{EUTILS}/einfo.fcgi", params={"db": "pubmed"}, timeout=10.0)
            return resp.status_code == 200
        except Exception:
            return False

    # === 内部 ===

    def _parse(self, uid: str, record: dict) -> Paper:
        date_text = str(record.get("pubdate") or record.get("sortpubdate") or "")
        year_match = re.search(r"\b(19|20)\d{2}\b", date_text)
        title = str(record.get("title") or "").strip()
        authors = []
        for author in record.get("authors") or []:
            lastname = str(author.get("lastname") or "").strip()
            forename = str(author.get("forename") or author.get("firstname") or "").strip()
            name = f"{forename} {lastname}".strip() if lastname and forename else str(author.get("name") or "").strip()
            if name:
                authors.append(name)
        year = int(year_match.group(0)) if year_match else 0
        article_ids = record.get("articleids") or []
        doi = next(
            (str(item.get("value")) for item in article_ids if item.get("idtype") == "doi"),
            None,
        )
        return Paper(
            lit_id=__import__("retrieval.paper_identity", fromlist=["build_lit_id"]).build_lit_id(
                source="pubmed", title=title, authors=authors, year=year, doi=doi,
            ),
            source=Source.PUBMED,
            title=title,
            authors=authors,
            journal=str(record.get("fulljournalname") or record.get("source") or ""),
            year=year,
            volume=str(record.get("volume") or "") or None,
            issue=str(record.get("issue") or "") or None,
            pages=str(record.get("pages") or "") or None,
            doi=doi,
            source_url=f"https://pubmed.ncbi.nlm.nih.gov/{uid}/",
        )

    def _extract_abstract_from_xml(self, xml_text: str) -> str | None:
        """简单 XML 解析:抓 <AbstractText>...</AbstractText>。"""
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            # 回退:用正则抓 <AbstractText ...>...</AbstractText>
            m = re.search(r"<AbstractText[^>]*>(.*?)</AbstractText>", xml_text, re.DOTALL)
            return re.sub(r"<[^>]+>", "", m.group(1)).strip() if m else None
        abstracts = root.findall(".//AbstractText")
        if not abstracts:
            return None
        parts = []
        for a in abstracts:
            label = a.attrib.get("Label", "")
            text = "".join(a.itertext()).strip()
            if text:
                parts.append(f"{label}: {text}" if label else text)
        return "\n".join(parts) or None

    def _extract_abstracts_from_batch_xml(self, xml_text: str) -> dict[str, str]:
        """解析批量 efetch XML(多个 PubmedArticle),返回 {pmid: abstract}。"""
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            log.warning("PubMed 批量 efetch 返回非法 XML,跳过本批")
            return {}
        out: dict[str, str] = {}
        for art in root.findall(".//PubmedArticle"):
            pmid_el = art.find(".//MedlineCitation/PMID")
            if pmid_el is None or not (pmid_el.text or "").strip():
                continue
            parts = []
            for a in art.findall(".//AbstractText"):
                label = a.attrib.get("Label", "")
                text = "".join(a.itertext()).strip()
                if text:
                    parts.append(f"{label}: {text}" if label else text)
            abstract = "\n".join(parts)
            if abstract:
                out[pmid_el.text.strip()] = abstract
        return out
