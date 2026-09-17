"""检索结果统一类型。字段值都来自平台原始返回,工具不构造任何字段。"""
from __future__ import annotations
import re
from dataclasses import dataclass, field, asdict
from enum import Enum


class Source(str, Enum):
    CNKI = "cnki"
    PUBMED = "pubmed"
    OPENALEX = "openalex"
    CROSSREF = "crossref"
    GOOGLE_SCHOLAR = "google_scholar"
    USER_IMPORTED = "user_imported"  # 中文手动导入


def normalize_source(value: object) -> Source:
    """Normalize API/UI source values, including ``Source.OPENALEX`` strings."""
    if isinstance(value, Source):
        return value
    raw = str(value or "").strip()
    if raw.startswith("Source."):
        raw = raw[7:]
    try:
        return Source(raw.lower())
    except ValueError as exc:
        raise ValueError(f"不支持的文献来源: {value!r}") from exc


_CNKI_PUBLICATION_YEAR_RE = re.compile(
    r"\[(?:J|J/OL)\]\.?\s*[^,\n]+,\s*((?:19|20)\d{2})(?=[,.(])",
    re.IGNORECASE,
)


def canonical_publication_year(
    source: Source | str,
    year: int | None,
    raw_citation: str | None,
) -> int:
    """Return the publication year used consistently in prose and references.

    Some historical CNKI snapshots contain the online-first year in ``year``
    but a different final publication year in the original GB/T citation.  The
    reference renderer intentionally preserves that original citation, so the
    narrative author/year must use the same publication year.  Access dates in
    ``[YYYY-MM-DD]`` are deliberately ignored by the journal-pattern regex.
    """
    normalized = source.value if isinstance(source, Source) else str(source or "").lower()
    if normalized in {Source.CNKI.value, Source.USER_IMPORTED.value} and raw_citation:
        match = _CNKI_PUBLICATION_YEAR_RE.search(raw_citation)
        if match:
            return int(match.group(1))
    return int(year or 0)


@dataclass
class Paper:
    """单篇文献的最小元数据集。

    所有字段值都来自平台原始返回(或中文粘贴的原文),
    工具不构造、不拼接任何字段值。
    """
    lit_id: str             # 本工具生成的内部 ID,SHA256(title|doi)[:16]
    source: Source          # 来源

    # 核心元数据字段
    title: str
    authors: list[str]
    journal: str
    year: int
    volume: str | None = None
    issue: str | None = None
    pages: str | None = None

    # 可选字段
    abstract: str | None = None
    doi: str | None = None
    source_url: str = ""    # 原文跳转链接(只读,不下载)
    cited_by_count: int = 0
    journal_level: str | None = None  # SCI/SSCI/AHCI/ESCI

    # LLM 计算字段(由平台返回/或同源计算,非构造)
    relevance_score: float | None = None

    # 真实性保障:数据来源溯源链(官方 API 地址 + 记录 id + 抓取时间),
    # 由 OpenAlexValidator 双重校验通过后填充,证明该记录来自官方合规数据源。
    provenance: dict | None = None

    # 中文专用:用户粘贴的原始 GB/T 7714 引文字符串(原样保留)
    raw_citation: str | None = None

    def __post_init__(self) -> None:
        self.year = canonical_publication_year(
            self.source, self.year, self.raw_citation
        )

    def to_dict(self) -> dict:
        d = asdict(self)
        # 把 Enum 转成可 JSON 序列化的字符串
        d["source"] = self.source.value if isinstance(self.source, Source) else str(self.source)
        return d
