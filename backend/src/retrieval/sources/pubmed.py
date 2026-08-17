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

from retrieval.intent import SearchIntent, core_concepts
from retrieval.sources.base import AcademicSource, SourcePage
from retrieval.types import Paper, Source

log = logging.getLogger(__name__)

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"


class PubMedSource:
    name = "pubmed"

    def __init__(self, timeout: float = 30.0, mailto: str | None = None):
        self.timeout = timeout
        self.mailto = mailto  # NCBI 鼓励提供,用于联系滥用

    # === AcademicSource 协议 ===

    def build_query(self, intent: SearchIntent) -> dict:
        """PubMed 语法:
        - 布尔用 AND / OR / NOT
        - 短语要双引号
        - 字段后缀:[tiab]=title+abstract, [ti]=title, [dp]=date, [la]=language, [pt]=publication type
        - 语言代码是 NCBI 三位码(eng/chi/...),不是 ISO 两位码(en/zh)
        - MeSH 词自动扩展(无需手动加 [mesh])

        PubMed 无布尔操作符上限,但"多概念全 AND"交集可能过窄直接零结果
        (实测 4 组精确短语全交 = 0)。这里与 OpenAlex 一致只保留核心 2 概念
        (同义词保留全量),保证首轮即有结果、展示与执行一致。
        """
        return self._build_query_from_boolean(
            intent, self._render_boolean_core(intent)
        )

    def build_sub_query(self, intent: SearchIntent, concept_ids: list[str]) -> dict:
        """按概念子集渲染子检索式(英文拆分链路)。

        PubMed 无操作符上限,同义词全量保留,保证子式语义完整。
        """
        return self._build_query_from_boolean(
            intent, self._render_boolean_core(intent, concept_ids)
        )

    def _build_query_from_boolean(self, intent: SearchIntent, boolean: str) -> dict:
        """布尔主体 + 年份/语言/类型/排除词组装。"""
        f = intent.filters
        clauses = [f"({boolean})"]
        if f.min_year is not None and f.max_year is not None:
            clauses.append(f"{f.min_year}:{f.max_year}[dp]")
        langs = [self._lang_code(lang) for lang in f.language if lang]
        if langs:
            # 一篇文章只属于一种语言:多语言必须 OR 分组,AND 连接必然零结果
            clauses.append("(" + " OR ".join(f"{lang}[la]" for lang in langs) + ")")
        if f.allowed_types:
            # 同理,一篇文章不可能同时是 journal article 和 review
            mapping = {"article": "journal article", "review": "review"}
            pts = [mapping.get(t, t) for t in f.allowed_types]
            clauses.append("(" + " OR ".join(f"{pt}[pt]" for pt in pts) + ")")
        for ex in intent.exclude_terms:
            ex_q = f'"{ex}"' if " " in ex else ex
            clauses.append(f"NOT {ex_q}[tiab]")
        return {"term": " AND ".join(clauses), "tool": self.mailto or "lit-review-agent"}

    def _lang_code(self, code: str) -> str:
        """ISO 639-1 两位码 -> NCBI 三位码;不认识的保留原样。"""
        mapping = {
            "en": "eng", "zh": "chi", "de": "ger", "fr": "fre", "es": "spa",
            "ja": "jpn", "ru": "rus", "ko": "kor", "it": "ita", "pt": "por",
        }
        return mapping.get(code, code)

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

    def _render_boolean_core(self, intent: SearchIntent,
                             concept_ids: list[str] | None = None) -> str:
        """布尔主体:保留核心 2 概念或指定概念子集(同义词保留全量,PubMed 无 ops 限制)。"""
        if concept_ids is None:
            selected = core_concepts(intent)
        else:
            selected = [c for c in intent.concepts if c.id in concept_ids]
        groups: list[str] = []
        for c in selected:
            syns = list(dict.fromkeys([c.label_en, *c.synonyms_en]))
            field_tag = {"title": "[ti]", "title_abstract": "[tiab]", "abstract": "[ab]"}.get(c.field, "[tiab]")
            quoted = [f'"{s}"{field_tag}' if " " in s else f"{s}{field_tag}" for s in syns]
            groups.append("(" + " OR ".join(quoted) + ")")
        return " AND ".join(groups)

    def _render_boolean(self, intent: SearchIntent) -> str:
        import re as _re
        groups: dict[str, str] = {}
        for c in intent.concepts:
            syns = list(dict.fromkeys([c.label_en, *c.synonyms_en]))
            field_tag = {"title": "[ti]", "title_abstract": "[tiab]", "abstract": "[ab]"}.get(c.field, "[tiab]")
            quoted = [f'"{s}"{field_tag}' if " " in s else f"{s}{field_tag}" for s in syns]
            groups[c.id] = "(" + " OR ".join(quoted) + ")"
        pattern = _re.compile(r"\b([A-Z])\b")

        def _sub(m):
            return groups.get(m.group(1), m.group(0))

        return pattern.sub(_sub, intent.boolean_template)

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
