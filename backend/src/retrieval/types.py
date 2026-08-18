"""检索结果统一类型。字段值都来自平台原始返回,工具不构造任何字段。"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from enum import Enum


class Source(str, Enum):
    CNKI = "cnki"
    PUBMED = "pubmed"
    OPENALEX = "openalex"
    CROSSREF = "crossref"
    USER_IMPORTED = "user_imported"  # 中文手动导入


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

    def to_dict(self) -> dict:
        d = asdict(self)
        # 把 Enum 转成可 JSON 序列化的字符串
        d["source"] = self.source.value if isinstance(self.source, Source) else str(self.source)
        return d
