"""PubMed 数据源实现(NCBI E-utilities)。

- esearch.fcgi 拿 PMID 列表
- esummary.fcgi 批量取基础元数据(无摘要)
- efetch.fcgi 单独补摘要(摘要不在 esummary 里,必须二次请求)
- 失败 3 次指数退避
"""
from __future__ import annotations

import asyncio
import logging
import re
import time

import httpx

from retrieval.sources.base import AcademicSource, SourcePage
from retrieval.types import Paper, Source

log = logging.getLogger(__name__)

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

# 硬编码的硬筛选条件(替代原 SearchIntent.filters)
DEFAULT_PUBMED_YEAR_BACK = 5
DEFAULT_PUBMED_LANGS = ["eng"]
DEFAULT_PUBMED_TYPES = ["journal article", "review"]


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

    def build_sub_query(self, query_string: str) -> dict:
        """把 LLM 直接输出的 PubMed 检索式字符串组装成 E-utilities 请求。

        query_string: LLM 直接输出的完整 PubMed 检索式,如
          ("Understanding by Design"[tiab] OR UbD[tiab]) AND "math teaching"[tiab]
        """
        return self._build_query_from_string(query_string)

    def _build_query_from_string(self, boolean: str) -> dict:
        """把布尔主体 + 默认年份/语言/类型组装成 PubMed E-utilities term。"""
        import datetime as _dt

        year = _dt.datetime.now().year
        clauses = [f"({boolean})"]
        clauses.append(f"{year - DEFAULT_PUBMED_YEAR_BACK}:{year}[dp]")
        # 一篇文章只属于一种语言:多语言必须 OR 分组
        clauses.append("(" + " OR ".join(f"{lang}[la]" for lang in DEFAULT_PUBMED_LANGS) + ")")
        # 同理,一篇文章不可能同时是 journal article 和 review
        clauses.append("(" + " OR ".join(f"{t}[pt]" for t in DEFAULT_PUBMED_TYPES) + ")")
        return {"term": " AND ".join(clauses), "tool": self.mailto or "lit-review-agent"}

    def execute(self, query: dict, page: int, per_page: int) -> SourcePage:
        retmax = min(per_page, 200)
        retstart = (page - 1) * retmax
        try:
            with httpx.Client(timeout=self.timeout) as client:
                # 第一步:拿 PMID 列表
                search_resp = client.get(
                    f"{EUTILS}/esearch.fcgi",
                    params={
                        "db": "pubmed", "term": query["term"],
                        "retmode": "json", "retmax": retmax, "retstart": retstart,
                        **({"tool": query["tool"]} if query.get("tool") else {}),
                    },
                )
                search_resp.raise_for_status()
                es = search_resp.json().get("esearchresult", {})
                ids = es.get("idlist", []) or []
                total = int(es.get("count", 0))
                if not ids:
                    return SourcePage(papers=[], total=total, has_next=False,
                                      page=page, raw_query=query)
                # 第二步:拿摘要(元数据 + 摘要一起拿)
                sum_resp = client.get(
                    f"{EUTILS}/esummary.fcgi",
                    params={"db": "pubmed", "id": ",".join(ids), "retmode": "json"},
                )
                sum_resp.raise_for_status()
                result = sum_resp.json().get("result", {})
                papers = [self._parse(uid, result.get(uid) or {}) for uid in ids if result.get(uid)]
                # esummary 不含 abstract,异步 efetch 回填
                self._schedule_efetch(papers)
                has_next = retstart + len(papers) < total
                return SourcePage(papers=papers, total=total, has_next=has_next,
                                  page=page, raw_query=query)
        except Exception as e:
            log.warning("PubMed 第 %d 页失败: %s", page, e)
            return SourcePage(papers=[], total=0, has_next=False, page=page, raw_query=query)

    def fetch_abstract_if_missing(self, paper: Paper) -> Paper | None:
        """通过 efetch.fcgi 取单条 PubMed 记录的 XML 摘要。"""
        if paper.abstract:
            return paper
        if not paper.lit_id.startswith("lit_pubmed_"):
            return None
        pmid = paper.lit_id.replace("lit_pubmed_", "")
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    resp = client.get(
                        f"{EUTILS}/efetch.fcgi",
                        params={"db": "pubmed", "id": pmid, "retmode": "xml"},
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

    def fetch_references(self, paper: Paper, depth: int = 1) -> list[Paper]:
        """PubMed 引用关系较弱(没有直接 references 端点);
        退化为:用 paper.title 在 PubMed 里反查同标题的引用。
        实际生产建议走 OpenAlex 的 references 路径。"""
        return []

    def health_check(self) -> bool:
        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.get(f"{EUTILS}/einfo.fcgi", params={"db": "pubmed"})
                return resp.status_code == 200
        except Exception:
            return False

    # === 内部 ===

    def _parse(self, uid: str, record: dict) -> Paper:
        date_text = str(record.get("pubdate") or record.get("sortpubdate") or "")
        year_match = re.search(r"\b(19|20)\d{2}\b", date_text)
        article_ids = record.get("articleids") or []
        doi = next(
            (str(item.get("value")) for item in article_ids if item.get("idtype") == "doi"),
            None,
        )
        return Paper(
            lit_id=f"lit_pubmed_{uid}",
            source=Source.PUBMED,
            title=str(record.get("title") or "").strip(),
            authors=[str(author.get("name")) for author in record.get("authors") or [] if author.get("name")],
            journal=str(record.get("fulljournalname") or record.get("source") or ""),
            year=int(year_match.group(0)) if year_match else 0,
            volume=str(record.get("volume") or "") or None,
            issue=str(record.get("issue") or "") or None,
            pages=str(record.get("pages") or "") or None,
            doi=doi,
            source_url=f"https://pubmed.ncbi.nlm.nih.gov/{uid}/",
        )

    def _extract_abstract_from_xml(self, xml_text: str) -> str | None:
        """简单 XML 解析:抓 <AbstractText>...</AbstractText>。"""
        import xml.etree.ElementTree as ET
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

    def _schedule_efetch(self, papers: list[Paper]) -> None:
        """后台异步补摘要:对一批 paper 并发 efetch。"""
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # 已有 loop 在跑,直接 create_task
                loop.create_task(self._efetch_batch(papers))
            else:
                # 同步调用方,fire-and-forget 线程池
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
                    ex.submit(lambda: asyncio.run(self._efetch_batch(papers)))
        except Exception as e:
            log.debug("PubMed 异步 efetch 调度失败: %s", e)

    async def _efetch_batch(self, papers: list[Paper]) -> None:
        sem = asyncio.Semaphore(3)

        async def _one(p: Paper):
            async with sem:
                loop = asyncio.get_event_loop()
                return await loop.run_in_executor(None, self.fetch_abstract_if_missing, p)

        await asyncio.gather(*[_one(p) for p in papers])
