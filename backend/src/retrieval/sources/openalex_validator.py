"""OpenAlex 数据真实性双重校验器。

官方依据: https://help.openalex.org/
- /data/            -> 实体类型、ID 方案、公共属性
- /data/work-types/ -> 25 个官方 work type 受控词表
- /api/             -> 响应 envelope(meta + results)、官方响应头、域名
- /api/authentication/ -> X-RateLimit-* 响应头

两类校验:
1. 数据来源溯源校验(provenance): 响应必须来自官方 api.openalex.org 域名、
   携带官方 envelope 与限流头;每条记录的 id 必须是指向 openalex.org 的合法
   OpenAlex ID(原生实体 W/A/S/I/P/F/G/T 短 ID 或 namespaced 长 ID)。
   这确保结果只来自 OpenAlex 官方合规数据源,杜绝伪造/仿冒域名响应。

2. 元数据字段合规性校验(compliance): 逐条记录校验官方数据规范——
   type 必须在 25 个官方 work types 内;publication_year 在合理学术时间窗内;
   DOI 必须是官方 doi.org 规范形式;publication_date 符合 ISO 日期形态;
   language 符合 iso639 两/三位码形态;authorships / primary_location 结构合规。

通过校验 -> verified 记录,附带 provenance 溯源链(record id + API 源 + 抓取时间);
未通过 -> rejected 记录 + 具体原因列表,一律不得进入检索结果。
"""
from __future__ import annotations

import datetime
import re
from dataclasses import dataclass, field

# 官方 API 基地址与数据域名
OFFICIAL_API_BASE = "https://api.openalex.org"
OFFICIAL_DATA_DOMAIN = "openalex.org"

# 官方 25 种 work types(https://help.openalex.org/data/work-types/)
# 校验 type 字段合规;也用作"全类型覆盖"遍历时的枚举来源。
WORK_TYPES = frozenset({
    "article", "book", "book-chapter", "book-review", "conference-abstract",
    "conference-paper", "data-paper", "dataset", "dissertation", "editorial",
    "erratum", "letter", "libguides", "other", "paratext", "peer-review",
    "preprint", "reference-entry", "report", "retraction", "review",
    "software", "software-paper", "standard", "supplementary-materials",
})

# 官方实体端点(https://help.openalex.org/api/endpoints/)
# works 是唯一默认被检索的实体;其余实体类型可按需扩展检索(全面性保障)。
ENTITY_ENDPOINTS = {
    "works": "/works",
    "authors": "/authors",
    "sources": "/sources",
    "institutions": "/institutions",
    "publishers": "/publishers",
    "funders": "/funders",
    "awards": "/awards",
    "topics": "/topics",
    "subfields": "/subfields",
    "fields": "/fields",
    "domains": "/domains",
    "keywords": "/keywords",
    "sdgs": "/sdgs",
    "concepts": "/concepts",  # deprecated,官方建议用 topics
}

# 原生实体 ID 前缀(https://help.openalex.org/data/#the-openalex-id-scheme)
NATIVE_ID_PREFIXES = frozenset("WASIPFGT")

# 合理学术时间窗:OpenAlex 收录最早期刊为 1665 年(Philosophical Transactions)。
EARLIEST_YEAR = 1665

# iso639_1 两字母语言码形态(OpenAlex language 字段)
_LANG_RE = re.compile(r"^[a-z]{2,3}$")
# DOI 规范形态: https://doi.org/10.xxxx/... 或裸 10.xxxx/...
_DOI_RE = re.compile(r"^(?:https://doi\.org/)?10\.\d{4,9}/.+$", re.IGNORECASE)
# publication_date 形态: YYYY / YYYY-MM / YYYY-MM-DD
_DATE_RE = re.compile(r"^\d{4}(?:-\d{2}(?:-\d{2})?)?$")
# OpenAlex 原生短 ID,如 W2741809807
_NATIVE_ID_RE = re.compile(r"^[WASIPFGT]\d{5,}$")


def _is_valid_date(s: str) -> bool:
    """校验 publication_date 的真实日期合法性。

    除形态匹配外,还要求月份 ∈ [1,12]、日 ∈ 当月天数、年份在学术时间窗内,
    杜绝 2024-13-99 这类"形态合法但日期非法"的伪造数据混入。
    兼容部分日期(YYYY / YYYY-MM / YYYY-MM-DD)。
    """
    if not _DATE_RE.match(s):
        return False
    try:
        parts = s.split("-")
        y = int(parts[0])
        if not (EARLIEST_YEAR <= y <= datetime.date.today().year):
            return False
        if len(parts) >= 2:
            m = int(parts[1])
            if not (1 <= m <= 12):
                return False
        if len(parts) == 3:
            import calendar
            d = int(parts[2])
            if not (1 <= d <= calendar.monthrange(y, m)[1]):
                return False
        return True
    except (ValueError, TypeError):
        return False


@dataclass
class ValidationIssue:
    """单条记录的单个校验问题。"""

    field: str          # 出问题的字段名
    reason: str         # 问题描述


@dataclass
class RecordOutcome:
    """单条记录的双重校验结果。"""

    verified: bool                  # True=通过全部校验
    record_id: str = ""             # 记录 id(openalex.org 短 ID 或 namespaced)
    issues: list[ValidationIssue] = field(default_factory=list)

    @property
    def reasons(self) -> list[str]:
        return [f"{i.field}: {i.reason}" for i in self.issues]


@dataclass
class BatchReport:
    """一批记录(一个响应/一次遍历)的校验报告。"""

    total: int = 0                 # 待校验记录数
    verified: int = 0              # 通过校验
    rejected: int = 0              # 未通过
    rejected_records: list[dict] = field(default_factory=list)  # {id, reasons}

    def add(self, outcome: RecordOutcome) -> None:
        self.total += 1
        if outcome.verified:
            self.verified += 1
        else:
            self.rejected += 1
            self.rejected_records.append({
                "id": outcome.record_id or "(无合法 ID)",
                "reasons": outcome.reasons,
            })


class OpenAlexValidator:
    """双重校验:数据来源溯源 + 元数据字段合规。"""

    # === 校验一:数据来源溯源(provenance) ===

    def validate_response_source(
        self,
        final_url: str | None,
        status_code: int | None,
        headers: dict | None,
        envelope: dict | None,
    ) -> RecordOutcome:
        """校验一次 API 响应的来源真实性。

        - 最终 URL 必须来自官方 api.openalex.org(防止被劫持/仿冒域名);
        - HTTP 必须 200(官方成功语义);
        - 必须携带官方 envelope: meta.count(int) + results(list);
        - 必须携带官方限流头 X-RateLimit-Limit(官方响应的标志性头)。
        """
        issues: list[ValidationIssue] = []
        record_id = "response"

        if not final_url or not str(final_url).startswith(OFFICIAL_API_BASE):
            issues.append(ValidationIssue(
                "url", f"响应来自 {final_url or '<未知>'},非官方 {OFFICIAL_API_BASE}"
            ))
        if status_code != 200:
            issues.append(ValidationIssue("status", f"HTTP {status_code},非官方成功状态 200"))
        meta = (envelope or {}).get("meta")
        results = (envelope or {}).get("results")
        if not isinstance(meta, dict) or not isinstance(meta.get("count"), int):
            issues.append(ValidationIssue("envelope", "缺少官方 meta.count(总命中数)"))
        if not isinstance(results, list):
            issues.append(ValidationIssue("envelope", "缺少官方 results 列表"))
        if not headers or "x-ratelimit-limit" not in {k.lower() for k in headers}:
            issues.append(ValidationIssue(
                "headers", "缺少官方 X-RateLimit-* 响应头(非官方 API 响应)"
            ))

        return RecordOutcome(verified=not issues, record_id=record_id, issues=issues)

    def validate_record_id(self, record_id: str | None) -> RecordOutcome:
        """校验记录 id 是合法 OpenAlex ID。

        原生实体: 短 ID(W2741809807) 或 https://openalex.org/W2741809807;
        非原生实体: namespaced 长 ID(如 topics/T12345、sdgs/2)。
        """
        issues: list[ValidationIssue] = []
        if not record_id:
            return RecordOutcome(False, "", [ValidationIssue("id", "缺少 id")])
        rid = str(record_id)
        # namespaced 长 ID: https://openalex.org/{entity}/{id}
        if rid.startswith(f"https://{OFFICIAL_DATA_DOMAIN}/"):
            tail = rid[len(f"https://{OFFICIAL_DATA_DOMAIN}/"):]
            parts = tail.split("/")
            if len(parts) >= 2 and parts[0] in ENTITY_ENDPOINTS:
                return RecordOutcome(True, tail)
            if len(parts) == 1 and _NATIVE_ID_RE.match(parts[0]):
                return RecordOutcome(True, parts[0])
            issues.append(ValidationIssue("id", f"id 形态不合法: {rid}"))
        # 裸短 ID(仅原生实体)
        elif _NATIVE_ID_RE.match(rid) and rid[0] in NATIVE_ID_PREFIXES:
            return RecordOutcome(True, rid)
        else:
            issues.append(ValidationIssue("id", f"id 不指向官方域名: {rid}"))
        return RecordOutcome(False, rid, issues)

    # === 校验二:元数据字段合规(compliance) ===

    def validate_record(self, record: dict) -> RecordOutcome:
        """逐字段校验一条 work(或通用实体)记录的元数据合规性。"""
        issues: list[ValidationIssue] = []
        id_outcome = self.validate_record_id(record.get("id"))
        if not id_outcome.verified:
            issues.extend(id_outcome.issues)
        record_id = id_outcome.record_id

        # type 必须在官方 25 种 work types 内
        wtype = record.get("type")
        if wtype is not None and wtype not in WORK_TYPES:
            issues.append(ValidationIssue(
                "type", f"{wtype!r} 不在官方 work types 枚举内"
            ))

        # 年份在合理学术时间窗 [1665, 当前年]
        year = record.get("publication_year")
        if year is not None:
            if not isinstance(year, int) or not (EARLIEST_YEAR <= year <= datetime.date.today().year):
                issues.append(ValidationIssue(
                    "publication_year", f"{year!r} 超出学术时间窗"
                ))

        # 标题非空(works 实体核心字段)
        title = (record.get("title") or record.get("display_name") or "").strip()
        if not title:
            issues.append(ValidationIssue("title", "标题为空"))

        # DOI 必须符合官方 doi.org 规范形态(works 的 canonical external id)
        doi = record.get("doi")
        if doi is not None and not _DOI_RE.match(str(doi)):
            issues.append(ValidationIssue("doi", f"DOI 形态非法: {doi}"))

        # 日期形态 ISO 兼容 + 真实日期合法(月/日/年窗)
        pub_date = record.get("publication_date")
        if pub_date is not None and not _is_valid_date(str(pub_date)):
            issues.append(ValidationIssue("publication_date", f"日期形态/取值非法: {pub_date}"))

        # 语言 iso639 两/三位码
        lang = record.get("language")
        if lang is not None and not _LANG_RE.match(str(lang)):
            issues.append(ValidationIssue("language", f"language 形态非法: {lang}"))

        # authorships 结构: 列表,每项含 author 对象(works 特有)
        authorships = record.get("authorships")
        if authorships is not None:
            if not isinstance(authorships, list):
                issues.append(ValidationIssue("authorships", "authorships 必须是列表"))
            else:
                for i, a in enumerate(authorships):
                    if not isinstance(a, dict) or not isinstance(a.get("author"), dict):
                        issues.append(ValidationIssue(
                            "authorships", f"第 {i} 项缺少 author 对象"
                        ))

        # primary_location.source 结构: source 对象(可为 null)
        primary = record.get("primary_location")
        if primary is not None:
            if not isinstance(primary, dict):
                issues.append(ValidationIssue("primary_location", "primary_location 必须是对象"))
            elif primary.get("source") is not None and not isinstance(primary.get("source"), dict):
                issues.append(ValidationIssue("primary_location", "source 必须是对象"))

        return RecordOutcome(verified=not issues, record_id=record_id, issues=issues)

    def validate_batch(self, records: list[dict]) -> BatchReport:
        """批量校验并生成报告。"""
        report = BatchReport()
        for rec in records or []:
            report.add(self.validate_record(rec if isinstance(rec, dict) else {}))
        return report

    # === 便捷入口 ===

    def validate(
        self,
        envelope: dict | None,
        final_url: str | None = None,
        status_code: int | None = None,
        headers: dict | None = None,
    ) -> BatchReport:
        """响应级溯源校验 + 逐条记录合规校验,一步完成。

        返回 BatchReport;verified 条数 = 双重校验全部通过的可信记录数。
        """
        src = self.validate_response_source(final_url, status_code, headers, envelope)
        records = ((envelope or {}).get("results") or []) if isinstance(envelope, dict) else []
        report = self.validate_batch(records)
        if not src.verified:
            # 溯源不过关:整批不可信,全部判为 rejected
            report.verified = 0
            report.rejected = report.total
            for issue in src.issues:
                report.rejected_records.insert(0, {
                    "id": issue.field, "reasons": [issue.reason],
                })
        return report


def build_provenance(record: dict, api_url: str | None = None) -> dict:
    """为一条已验证记录构建溯源链(来源校验的落地产物)。"""
    return {
        "record_id": str(record.get("id") or ""),
        "source": "OpenAlex 官方 API",
        "api_url": api_url or f"{OFFICIAL_API_BASE}/works",
        "domain": OFFICIAL_DATA_DOMAIN,
        "fetched_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
    }


__all__ = [
    "OpenAlexValidator", "BatchReport", "RecordOutcome", "ValidationIssue",
    "build_provenance", "WORK_TYPES", "ENTITY_ENDPOINTS",
    "OFFICIAL_API_BASE", "OFFICIAL_DATA_DOMAIN",
]
