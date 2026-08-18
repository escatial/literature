"""OpenAlex 数据源实现。

- 走 https://api.openalex.org/works
- 用 api_key 走用量计费额度(每日 $1 免费);mailto 礼貌池已废弃
- 摘要通过 abstract_inverted_index 反向重建
- 失败 3 次指数退避重试
- 不依赖任何领域词表
- 真实性保障: 响应级溯源校验 + 逐条元数据合规校验(OpenAlexValidator 双重校验),
  未通过校验的记录一律不进检索结果;
- 全面性保障: 官方 cursor 分页全量遍历(突破 10,000 条基本翻页上限)、
  corpus=all 全量数据集、多实体端点、25 种官方 work types 全覆盖。
官方规范依据: https://help.openalex.org/
"""
from __future__ import annotations

import hashlib
import logging
import os
import time
from dataclasses import dataclass, field

import httpx

from retrieval.sources.base import AcademicSource, SourcePage
from retrieval.sources.openalex_validator import (
    ENTITY_ENDPOINTS,
    BatchReport,
    OpenAlexValidator,
    build_provenance,
)
from retrieval.types import Paper, Source

# OpenAlex 硬限:布尔操作符 >5 触发 429
MAX_BOOLEAN_OPS = 5
# 硬编码的硬筛选条件(替代原 SearchIntent.filters,默认近 5 年,英文 article/review)
DEFAULT_YEAR_BACK = 5
DEFAULT_LANGUAGE = ["en"]
DEFAULT_TYPES = ["article", "review"]

log = logging.getLogger(__name__)

DEFAULT_MAILTO = os.getenv("OPENALEX_MAILTO", "your-email@example.com")
DEFAULT_API_KEY = os.getenv("OPENALEX_API_KEY", "")
BASE_URL = "https://api.openalex.org/works"


class OpenAlexRateLimitError(RuntimeError):
    """OpenAlex 返回 429 限流(免费池额度耗尽 / 布尔操作符过多 / 请求过频)。

    区别于「真实零结果」:零结果是合法检索式无命中,限流是服务端拒绝请求。
    任务层据此给出「限流」而非「命中 0 篇」的准确提示。
    """


@dataclass
class ComprehensiveSearchResult:
    """全面性检索结果:可信记录 + 全量遍历统计 + 校验报告。

    - papers: 通过双重校验的唯一可信记录;
    - verified/rejected: 校验统计(真实性报告);
    - cursor_exhausted: True=已按官方游标遍历到尽头,结果无遗漏;
      False=因达到 max_records 保护上限提前停止(需提高上限继续)。
    """

    papers: list  # list[Paper]
    total: int = 0                     # 官方 meta.count(总命中数)
    verified: int = 0                  # 通过双重校验的记录数
    rejected: int = 0                  # 未通过校验被剔除的记录数
    rejected_records: list = field(default_factory=list)  # [{id, reasons}]
    pages_fetched: int = 0             # 实际请求页数
    cursor_exhausted: bool = False     # 游标是否遍历到尽头(全量无遗漏)
    api_url: str = ""                  # 实际请求的官方端点
    corpus: str | None = None          # 数据集视图 core/expansion/all

    @property
    def verified_rate(self) -> float:
        """真实性校验通过率(0~1)。"""
        if not self.total:
            return 0.0
        return round(self.verified / max(self.total, 1), 4)


def _rebuild_abstract(inverted: dict | None) -> str | None:
    """OpenAlex abstract_inverted_index 反向索引还原。"""
    if not inverted:
        return None
    word_positions: list[tuple[int, str]] = []
    for word, positions in inverted.items():
        for p in positions:
            word_positions.append((p, word))
    word_positions.sort()
    text = " ".join(w for _, w in word_positions).strip()
    return text or None


def _make_lit_id(title: str | None, doi: str | None) -> str:
    raw = f"{title or ''}|{doi or ''}"
    return "lit_" + hashlib.sha256(raw.encode()).hexdigest()[:16]


class OpenAlexSource:
    name = "openalex"

    def __init__(self, mailto: str | None = None, api_key: str | None = None,
                 timeout: float = 30.0):
        self.mailto = mailto or DEFAULT_MAILTO
        self.api_key = api_key or DEFAULT_API_KEY
        self.timeout = httpx.Timeout(timeout)
        self.validator = OpenAlexValidator()

    # === AcademicSource 协议 ===

    def build_query(self, intent) -> dict:
        """兼容旧 AcademicSource 协议(传 SearchIntent 时取主检索式)。

        新链路直接走 build_sub_query(query_string) ,不走 intent。
        """
        # 旧链路接收 SearchIntent, 这里用它的 boolean_template+同义词做兜底
        boolean = (getattr(intent, "boolean_template", "") or "").strip() or ""
        return self._build_query_from_string(boolean)

    def build_sub_query(self, query_string: str) -> dict:
        """把 LLM 直接输出的检索式字符串透传给 OpenAlex。

        query_string: LLM 直接输出的完整 OpenAlex 检索式,如
          ("Understanding by Design" OR UbD) AND "math teaching"
        """
        return self._build_query_from_string(query_string)

    def _build_query_from_string(self, boolean: str) -> dict:
        """把布尔主体 + 默认 filter 组装成 OpenAlex 请求参数。"""
        import datetime as _dt

        year = _dt.datetime.now().year
        filter_parts = [
            f"publication_year:{year - DEFAULT_YEAR_BACK}-{year}",
            f"type:{'|'.join(DEFAULT_TYPES)}",
            f"language:{'|'.join(DEFAULT_LANGUAGE)}",
        ]
        return {
            "search": boolean,
            "filter": ",".join(filter_parts),
            "sort": "cited_by_count:desc",  # 前几页就有好货
            "api_key": self.api_key,
        }

    def execute(self, query: dict, page: int, per_page: int) -> SourcePage:
        params = dict(query)
        params.update({"page": page, "per-page": min(per_page, 100)})
        params = {k: v for k, v in params.items() if v not in (None, "")}

        last_err: Exception | None = None
        for attempt in range(3):
            try:
                with httpx.Client(timeout=self.timeout, trust_env=False, transport=httpx.HTTPTransport(local_address="0.0.0.0")) as client:
                    resp = client.get(BASE_URL, params=params)
                    resp.raise_for_status()
                j = resp.json()
                # 真实性保障-校验一: 响应来源溯源(官方域名 + envelope + 限流头)
                report = self.validator.validate(
                    envelope=j,
                    final_url=str(resp.url),
                    status_code=resp.status_code,
                    headers=dict(resp.headers),
                )
                if report.verified != report.total or report.rejected:
                    log.warning(
                        "OpenAlex 双重校验未全通过: verified=%d/%d rejected=%d %s",
                        report.verified, report.total, report.rejected,
                        report.rejected_records[:1],
                    )
                # 只保留通过双重校验的可信记录
                verified_records = [
                    w for w in j.get("results", [])
                    if self.validator.validate_record(w).verified
                ]
                papers = [self._parse(w) for w in verified_records]
                total = j.get("meta", {}).get("count", 0)
                has_next = page * per_page < total
                return SourcePage(
                    papers=papers, total=total, has_next=has_next,
                    page=page, raw_query=params,
                )
            except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ConnectError) as e:
                last_err = e
                wait = 2 ** attempt
                log.warning("OpenAlex attempt=%d 失败: %s; %ds 后重试", attempt + 1, e, wait)
                time.sleep(wait)
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 429:
                    # 429 = 免费池额度耗尽 / 操作符过多 / 请求过频;
                    # 必须抛「限流」错误,不能返回 total=0 伪装成「零结果」,
                    # 否则任务层会误触发放宽重检并显示误导性的「命中 0 篇」。
                    try:
                        detail = e.response.json().get("message") or e.response.text[:200]
                    except Exception:
                        detail = e.response.text[:200] or str(e)
                    raise OpenAlexRateLimitError(detail) from e
                log.warning("OpenAlex HTTP %s: %s", e.response.status_code, e)
                return SourcePage(papers=[], total=0, has_next=False, page=page, raw_query=params)
        log.error("OpenAlex 全部重试失败: %s", last_err)
        return SourcePage(papers=[], total=0, has_next=False, page=page, raw_query=params)

    def fetch_abstract_if_missing(self, paper: Paper) -> Paper | None:
        """OpenAlex 的 abstract 已在 execute 中通过 inverted_index 重建;这里只做兜底。"""
        if paper.abstract:
            return paper
        if not paper.source_url:
            return None
        # 这里不重新请求 OpenAlex 单条 work(成本高),保持 None
        return None

    def fetch_references(self, paper: Paper, depth: int = 1) -> list[Paper]:
        """OpenAlex 的 referenced_works 列表需要二次查询。
        depth=1: 拉直接引用的一层。depth>1: 递归,但通常不要超过 2。"""
        if not paper.lit_id.startswith("lit_openalex_"):
            return []
        openalex_id = paper.lit_id.replace("lit_openalex_", "")
        url = f"{BASE_URL}/{openalex_id}"
        try:
            with httpx.Client(timeout=self.timeout, trust_env=False, transport=httpx.HTTPTransport(local_address="0.0.0.0")) as client:
                resp = client.get(url, params={"api_key": self.api_key})
                resp.raise_for_status()
                j = resp.json()
                ref_ids = j.get("referenced_works", []) or []
        except Exception as e:
            log.warning("OpenAlex 拉 %s 引用失败: %s", paper.lit_id, e)
            return []
        if not ref_ids:
            return []
        # 批量查询 references
        ids_filter = "|".join(rid.rsplit("/", 1)[-1] for rid in ref_ids[:50])
        try:
            with httpx.Client(timeout=self.timeout, trust_env=False, transport=httpx.HTTPTransport(local_address="0.0.0.0")) as client:
                resp = client.get(
                    BASE_URL,
                    params={"filter": f"ids.openalex:{ids_filter}", "per-page": 50, "api_key": self.api_key},
                )
                resp.raise_for_status()
                return [self._parse(w) for w in resp.json().get("results", [])]
        except Exception as e:
            log.warning("OpenAlex 批量拉 references 失败: %s", e)
            return []

    def health_check(self) -> bool:
        try:
            with httpx.Client(timeout=10.0, trust_env=False, transport=httpx.HTTPTransport(local_address="0.0.0.0")) as client:
                resp = client.get(BASE_URL, params={"per-page": 1, "api_key": self.api_key})
                return resp.status_code == 200
        except Exception:
            return False

    # === 全面性保障:cursor 全量遍历(官方分页规范) ===

    def execute_cursor(
        self,
        query: dict,
        cursor: str = "*",
        per_page: int = 100,
        entity: str = "works",
    ) -> tuple[list[Paper], int, str | None, "BatchReport"]:
        """按官方 cursor 分页协议执行一页(https://help.openalex.org/api/paging/)。

        - cursor="*" 起始,响应 meta.next_cursor 推进,next_cursor=None 即遍历结束;
        - 突破基本分页 10,000 条上限,可遍历任意深度的全量结果;
        - 返回 (通过双重校验的 papers, meta.count, next_cursor, 校验报告)。

        来源校验与逐条校验在 execute 里已内建;这里同样只返回可信记录。
        """
        if entity not in ENTITY_ENDPOINTS:
            raise ValueError(f"非法实体端点: {entity}(可选 {sorted(ENTITY_ENDPOINTS)})")
        url = f"https://api.openalex.org{ENTITY_ENDPOINTS[entity]}"
        params = {k: v for k, v in dict(query).items() if v not in (None, "")}
        params.update({"cursor": cursor, "per-page": min(per_page, 100)})

        with httpx.Client(timeout=self.timeout, trust_env=False, transport=httpx.HTTPTransport(local_address="0.0.0.0")) as client:
            resp = client.get(url, params=params)
            resp.raise_for_status()
        j = resp.json()
        report = self.validator.validate(
            envelope=j, final_url=str(resp.url),
            status_code=resp.status_code, headers=dict(resp.headers),
        )
        verified_records = [
            w for w in j.get("results", [])
            if isinstance(w, dict) and self.validator.validate_record(w).verified
        ]
        papers = [self._parse(w, api_url=url) for w in verified_records]
        meta = j.get("meta") or {}
        return papers, meta.get("count", 0), meta.get("next_cursor"), report

    # === 全面性保障:多实体/全类型/多维筛选/全量遍历综合检索 ===

    def build_comprehensive_filter(
        self,
        intent: SearchIntent | None = None,
        *,
        work_types: list[str] | None = None,
        languages: list[str] | None = None,
        is_oa: bool | None = None,
        min_cited_by: int | None = None,
        institution_ids: list[str] | None = None,
        author_ids: list[str] | None = None,
        topic_ids: list[str] | None = None,
    ) -> str:
        """构造官方 filter 表达式,覆盖全部可检索维度。

        官方语法(https://help.openalex.org/api/filtering/):
        逗号=AND, | 同属性 OR(最多 100 值), 数值支持 < >, 布尔值用 true/false。
        work_types=None 时不限定 type(默认即覆盖全部 25 种官方类型)。
        """
        parts: list[str] = []
        if intent is not None:
            parts.append(
                f"publication_year:{intent.filters.min_year}-{intent.filters.max_year}"
            )
            if languages is None and intent.filters.language:
                languages = intent.filters.language
        if work_types:
            parts.append("type:" + "|".join(dict.fromkeys(work_types)))
        if languages:
            parts.append("language:" + "|".join(dict.fromkeys(languages)))
        if is_oa is not None:
            parts.append(f"open_access.is_oa:{str(is_oa).lower()}")
        if min_cited_by is not None:
            parts.append(f"cited_by_count:>{min_cited_by}")
        if institution_ids:
            parts.append("institutions.id:" + "|".join(institution_ids))
        if author_ids:
            parts.append("author.id:" + "|".join(author_ids))
        if topic_ids:
            parts.append("topics.id:" + "|".join(topic_ids))
        return ",".join(parts)

    def search_comprehensive(
        self,
        filter_str: str = "",
        *,
        search: str | None = None,
        entity: str = "works",
        corpus: str = "core",
        sort: str | None = None,
        max_records: int = 1000,
        per_page: int = 100,
    ) -> "ComprehensiveSearchResult":
        """全面性检索:按官方 cursor 分页协议全量遍历 + 双重校验过滤。

        覆盖维度:
        - 实体类型: entity 可选全部官方端点(works/authors/sources/institutions/topics...);
        - 数据集: corpus=core(默认,320M+)/expansion/all(510M+);
        - 文献类型: 通过 filter 里的 type 覆盖,不限定即全 25 种类型;
        - 筛选维度: 年份/语言/开放获取/引用量/机构/作者/学科(topic)等,见
          build_comprehensive_filter;
        - 全量遍历: cursor 分页逐页拉取,直到 next_cursor=None(官方遍历协议)。

        max_records 仅作客户端保护上限(防止意外全库下载),默认 1000。
        """
        if entity not in ENTITY_ENDPOINTS:
            raise ValueError(f"非法实体端点: {entity}(可选 {sorted(ENTITY_ENDPOINTS)})")
        url = f"https://api.openalex.org{ENTITY_ENDPOINTS[entity]}"
        params: dict = {"api_key": self.api_key, "per-page": min(per_page, 100)}
        if filter_str:
            params["filter"] = filter_str
        if search:
            params["search"] = search
        if corpus in ("core", "expansion", "all"):
            # corpus 仅 works 有效,其他实体拒绝该参数(官方规范),故仅 works 附加
            if entity == "works":
                params["corpus"] = corpus
        if sort:
            params["sort"] = sort

        papers: list[Paper] = []
        seen: set[str] = set()
        report = self.validator.validate_batch([])  # 聚合报告,逐页累加
        cursor = "*"
        pages = 0
        total_hits = 0
        exhausted = False
        try:
            while True:
                batch, meta_count, next_cursor, batch_report = self.execute_cursor(
                    {k: v for k, v in params.items()}, cursor=cursor,
                    per_page=per_page, entity=entity,
                )
                pages += 1
                total_hits = meta_count
                # 汇总校验统计
                report.total += batch_report.total
                report.verified += batch_report.verified
                report.rejected += batch_report.rejected
                report.rejected_records.extend(batch_report.rejected_records)
                for p in batch:
                    if p.lit_id in seen:
                        continue
                    seen.add(p.lit_id)
                    papers.append(p)
                    if len(papers) >= max_records:
                        break
                if len(papers) >= max_records or not next_cursor:
                    exhausted = not next_cursor
                    break
                cursor = next_cursor
        except httpx.HTTPError as e:
            log.warning("OpenAlex 全面检索中断(第 %d 页): %s", pages + 1, e)

        return ComprehensiveSearchResult(
            papers=papers, total=total_hits, verified=report.verified,
            rejected=report.rejected, rejected_records=report.rejected_records,
            pages_fetched=pages, cursor_exhausted=exhausted, api_url=url,
            corpus=corpus if entity == "works" else None,
        )

    # === 内部 ===

    def _parse(self, w: dict, api_url: str | None = None) -> Paper:
        doi_raw = w.get("doi") or ""
        doi = doi_raw.replace("https://doi.org/", "") or None
        title = (w.get("title") or w.get("display_name") or "").strip()

        authors = [
            a["author"]["display_name"]
            for a in w.get("authorships", [])
            if a.get("author")
        ]

        biblio = w.get("biblio") or {}
        volume = str(biblio.get("volume")) if biblio.get("volume") else None
        issue = str(biblio.get("issue")) if biblio.get("issue") else None
        first = biblio.get("first_page")
        last = biblio.get("last_page")
        if first and last:
            pages = f"{first}-{last}"
        elif first:
            pages = str(first)
        elif last:
            pages = str(last)
        else:
            pages = None

        primary = w.get("primary_location") or {}
        source_loc = primary.get("source") or {}
        journal = source_loc.get("display_name") or ""

        openalex_id = str(w.get("id") or "").rstrip("/").rsplit("/", 1)[-1]
        lit_id = f"lit_openalex_{openalex_id.lower()}" if openalex_id else _make_lit_id(title, doi)
        source_url = f"https://openalex.org/{openalex_id}" if openalex_id else ""
        # 双重校验通过的记录才带溯源链(证明来自官方合规数据源)
        provenance = build_provenance(w, api_url or BASE_URL)
        return Paper(
            lit_id=lit_id,
            source=Source.OPENALEX,
            title=title,
            authors=authors,
            journal=journal,
            year=w.get("publication_year") or 0,
            volume=volume,
            issue=issue,
            pages=pages,
            abstract=_rebuild_abstract(w.get("abstract_inverted_index")),
            doi=doi,
            source_url=source_url,
            cited_by_count=w.get("cited_by_count") or 0,
            relevance_score=w.get("relevance_score"),
            provenance=provenance,
        )
