#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""数据质量保障：格式标准化 + 完整性校验 + 异常标记 + 质量评分（0-100）。

定位说明（重要，避免与既有逻辑重复）：
- ``retrieval.paper_identity.repair_paper_fields`` 已负责**兜底补全**
  （year←引文提取、abstract←abstract_text、raw_citation 拼装）；
- ``retrieval.paper_identity.validate_paper_identity`` 已负责**入库硬校验**
  （title/authors/year/abstract 缺失直接 raise）；
- 本模块是其上的**增量评估层**：
  1) 字段格式标准化（空白/HTML 残留/作者列表清洗）——原地清洗、不改键集合；
  2) 确定性补全（仅从 raw_citation 提取 DOI:xxx 这类有把握的来源，绝不编造）；
  3) 异常标记 flags（缺 DOI/缺刊名/年份越界/摘要过短……只标记不阻断）；
  4) 加权质量评分，供监控面板与任务报告消费。

约束：flags **不写入** record（PaperModel(**record) 有未知键即崩），由调用方
（cnki_adapter）把 QualityReport 交给 monitor 统计。
本模块不 import crawler。
"""

from __future__ import annotations

import html as _html
import re
import time
from dataclasses import dataclass, field

# 评分权重（合计 100）：核心字段 title/authors/abstract 权重高
_SCORE_WEIGHTS = {
    "title": 25,
    "authors": 20,
    "abstract": 25,
    "journal": 10,
    "year": 10,
    "traceable": 10,   # doi 或 source_url 可溯源
}
_FLAG_PENALTY = 5  # 每个 flag 追加扣分

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_Doi_IN_TEXT_RE = re.compile(r"(?i)\bDOI\s*[:：]?\s*(10\.\d{4,9}/[^\s\"<>，。;；]+)")
_Doi_VALIDATE_RE = re.compile(r"^10\.\d{4,9}/\S+$")


# ========================== 质量报告 ==========================
@dataclass
class QualityReport:
    """单条记录的质量评估结果（monitor 聚合、面板展示）。"""

    score: int = 100                 # 0-100
    flags: list[str] = field(default_factory=list)   # 异常标记（如 missing_doi）
    filled: list[str] = field(default_factory=list)  # 本模块补全过的字段
    normalized: list[str] = field(default_factory=list)  # 本模块清洗过的字段
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "score": self.score,
            "flags": list(self.flags),
            "filled": list(self.filled),
            "normalized": list(self.normalized),
            "ts": self.ts,
        }


# ========================== 字段标准化 ==========================
def _clean_text(value: str) -> str:
    """通用文本清洗：HTML 实体/标签残留 → 折叠空白 → strip。"""
    if not value:
        return ""
    text = _html.unescape(value)
    text = _TAG_RE.sub("", text)         # 摘要里偶发 <br>/<sub> 残留
    text = _WS_RE.sub(" ", text)
    return text.strip()


def normalize_fields(record: dict) -> list[str]:
    """原地标准化文本字段，返回实际发生清洗的字段名列表。

    覆盖字段：title / abstract / abstract_text / journal / quote_text /
    raw_citation / authors（列表逐元素清洗+去空+保序去重）/ doi（小写、去前缀）。
    只清洗 record 已有的键，**不新增键**（DB 约束兼容）。
    """
    changed: list[str] = []
    for key in ("title", "abstract", "abstract_text", "journal", "quote_text"):
        if key in record:
            cleaned = _clean_text(str(record[key] or ""))
            if cleaned != record[key]:
                record[key] = cleaned
                changed.append(key)
    # raw_citation 是多行引文：折叠空白但保留换行结构
    if "raw_citation" in record:
        raw = str(record["raw_citation"] or "")
        cleaned = "\n".join(_clean_text(line) for line in raw.splitlines()).strip()
        if cleaned != record["raw_citation"]:
            record["raw_citation"] = cleaned
            changed.append("raw_citation")
    if "authors" in record:
        authors = record["authors"]
        if isinstance(authors, list):
            seen: set[str] = set()
            cleaned_list: list = []
            for a in authors:
                name = _clean_text(str(a or ""))
                if name and name not in seen:
                    seen.add(name)
                    cleaned_list.append(name if isinstance(a, str) else a)
            if cleaned_list != authors:
                record["authors"] = cleaned_list
                changed.append("authors")
    if "doi" in record:
        doi = str(record["doi"] or "").strip().lower()
        doi = re.sub(r"(?i)^https?://(dx\.)?doi\.org/", "", doi)
        doi = re.sub(r"(?i)^doi\s*[:：]\s*", "", doi)
        if doi != record["doi"]:
            record["doi"] = doi
            changed.append("doi")
    return changed


# ========================== 确定性补全 ==========================
def fill_missing(record: dict) -> list[str]:
    """缺失值自动补全（只做确定性补全，返回补全的字段名）。

    当前规则：doi 缺失时从 raw_citation 里的 "DOI:10.xxxx/yyy" 模式提取——
    引文原文里的 DOI 是权威来源，不属于编造；提取不到就留给 flags 标记。
    """
    filled: list[str] = []
    if not str(record.get("doi") or "").strip():
        citation = str(record.get("raw_citation") or "")
        m = _Doi_IN_TEXT_RE.search(citation)
        if m:
            doi = m.group(1).rstrip(".,;")
            record["doi"] = doi
            filled.append("doi")
    return filled


# ========================== 完整性与异常校验 ==========================
def assess(record: dict, now_year: int | None = None) -> QualityReport:
    """完整性校验 + 异常标记 + 加权评分。只读 record，不修改。"""
    now_year = now_year or time.localtime().tm_year
    flags: list[str] = []
    score = 0

    title = str(record.get("title") or "").strip()
    if len(title) >= 4:
        score += _SCORE_WEIGHTS["title"]
    else:
        flags.append("title_too_short")

    authors = record.get("authors") or []
    if isinstance(authors, str):
        authors = [authors] if authors.strip() else []
    if authors:
        score += _SCORE_WEIGHTS["authors"]
    else:
        flags.append("authors_empty")

    abstract = str(record.get("abstract") or "").strip()
    if len(abstract) >= 30:
        score += _SCORE_WEIGHTS["abstract"]
    elif abstract:
        flags.append("abstract_too_short")
    else:
        flags.append("missing_abstract")

    if str(record.get("journal") or "").strip():
        score += _SCORE_WEIGHTS["journal"]
    else:
        flags.append("missing_journal")

    year = record.get("year")
    if isinstance(year, int) and 1900 <= year <= now_year + 1:
        score += _SCORE_WEIGHTS["year"]
    else:
        flags.append("suspicious_year")

    doi = str(record.get("doi") or "").strip()
    if doi:
        # 有 DOI 但格式非法 → 视为缺 DOI 并标记格式异常
        if _Doi_VALIDATE_RE.match(doi):
            score += _SCORE_WEIGHTS["traceable"]
        else:
            flags.append("doi_format_invalid")
    elif str(record.get("source_url") or "").strip():
        score += _SCORE_WEIGHTS["traceable"]
    else:
        flags.append("missing_traceable")

    # flag 追加扣分（下限 0）
    score = max(0, score - _FLAG_PENALTY * len(flags))
    return QualityReport(score=int(score), flags=flags)


# ========================== 一站式管线 ==========================
def quality_pipeline(record: dict, now_year: int | None = None) -> tuple:
    """标准化 → 补全 → 评估，返回 (record本体, QualityReport)。

    record 原地修改（值清洗/补全），键集合不变；调用方拿 report 去
    monitor 记账，flags 永不进入 record。
    """
    normalized = normalize_fields(record)
    filled = fill_missing(record)
    report = assess(record, now_year=now_year)
    report.normalized = normalized
    report.filled = filled
    return record, report


def aggregate_score(reports: list) -> dict:
    """批量质量报告聚合（任务级）：平均分/各 flag 计数/分布。"""
    if not reports:
        return {"count": 0, "avg_score": 0, "flags": {}, "min_score": 0}
    flag_count: dict[str, int] = {}
    for r in reports:
        for f in r.flags:
            flag_count[f] = flag_count.get(f, 0) + 1
    scores = [r.score for r in reports]
    return {
        "count": len(reports),
        "avg_score": round(sum(scores) / len(scores), 1),
        "min_score": min(scores),
        "flags": flag_count,
    }
