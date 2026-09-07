"""核查1:文献引用准确性。

执行节点:run_screening_done / apply_citation_numbering 之后。

目的:逐一核验入选文献的原文出处、核心观点、数据结论的真实性与匹配度,
确保不存在错误引用、虚假引用或曲解原文的情况。

本项目已有的真实性护栏(继承使用,不重复实现):
- Paper.provenance     OpenAlex/PubMed 校验通过后的官方溯源链
- OpenAlexValidator    API 二次校验
- relevance.grade_papers 用 LLM 四维度评分降低幻觉

本核查在它们之上再做"数据完备性 + 内部一致性 + 元数据一致"的硬校验,
凡是缺元数据导致无法佐证的文献都视为"待复查",并阻断最终输出。
"""
from __future__ import annotations

import logging
from collections import Counter
from typing import Iterable

from qa.models import QACheckResult, QAIssue
from qa.rules import QACheckStatus
from retrieval.types import Paper, Source


log = logging.getLogger(__name__)


# 真实数据源(平台级官方 API)。人工粘贴的中文条目另列。
_OFFICIAL_SOURCES: frozenset[Source] = frozenset({
    Source.OPENALEX,
    Source.PUBMED,
    Source.CROSSREF,
})

# 没有 provenance 的官方源条目:在数据库落地前都已通过 OpenAlexValidator,
# 但 Paper 在内存里未必带 provenance(从 history_snapshot / user_imported 等还
# 原时可能丢);我们通过 paper.source + 字段完备度推断是否可信。
_NEED_PROVENANCE_OFFICIAL = frozenset({Source.OPENALEX, Source.PUBMED})


def _has_author(a: str) -> bool:
    a = (a or "").strip()
    if not a:
        return False
    # 屏蔽 "佚名" / "et al." / "无作者" 等无效署名
    low = a.lower()
    if low in {"佚名", "unknown", "n.a.", "n/a", "无作者", "et al."}:
        return False
    # 纯标点不计入
    return any(ch.isalnum() for ch in a)


def _check_metadata_completeness(papers: Iterable[Paper]) -> list[QAIssue]:
    """元数据完整性:author/title/year/journal 必须非空。"""
    issues: list[QAIssue] = []
    for paper in papers:
        path = f"paper/{paper.lit_id}"
        if not (paper.title or "").strip():
            issues.append(QAIssue(
                "ACCURACY_MISSING_TITLE", QACheckStatus.FAIL,
                "title", path, "文献标题为空,无法核验真实性",
                lit_id=paper.lit_id,
            ))
        if not paper.year or paper.year <= 0:
            issues.append(QAIssue(
                "ACCURACY_MISSING_YEAR", QACheckStatus.FAIL,
                "year", path, "文献年份缺失/异常,无法核验引用",
                lit_id=paper.lit_id,
            ))
        if not paper.journal or not paper.journal.strip():
            issues.append(QAIssue(
                "ACCURACY_MISSING_JOURNAL", QACheckStatus.WARN,
                "journal", path, "文献期刊名为空,可能影响规范著录",
                lit_id=paper.lit_id,
            ))
        authors = list(paper.authors or [])
        if not authors:
            issues.append(QAIssue(
                "ACCURACY_MISSING_AUTHORS", QACheckStatus.FAIL,
                "authors", path, "文献作者缺失,无法核验署名真实性",
                lit_id=paper.lit_id,
            ))
        else:
            bad = [a for a in authors if not _has_author(a)]
            if bad:
                issues.append(QAIssue(
                    "ACCURACY_BAD_AUTHOR", QACheckStatus.WARN,
                    "authors", path,
                    f"含无效署名: {bad[:3]}",
                    lit_id=paper.lit_id,
                ))
    return issues


def _check_dup_identity(papers: Iterable[Paper]) -> list[QAIssue]:
    """检测同一篇文献出现多条近似条目导致"重复引用"。

    启发式:title 归一化后同名且年份相同时视为同一条,只能保留 1 条入选。
    """
    issues: list[QAIssue] = []
    buckets: dict[tuple[str, int], list[Paper]] = {}
    for paper in papers:
        if not paper.title or not paper.year:
            continue
        key = (paper.title.strip().lower(), int(paper.year))
        buckets.setdefault(key, []).append(paper)
    for (title, year), group in buckets.items():
        if len(group) > 1:
            for dup in group[1:]:
                issues.append(QAIssue(
                    "ACCURACY_DUPLICATE", QACheckStatus.FAIL,
                    "title", f"paper/{dup.lit_id}",
                    "疑似与现有条目重复(同年同题),请人工合并或剔除",
                    lit_id=dup.lit_id,
                    snippet=f"{title[:60]} ({year})",
                ))
    return issues


def _check_source_authority(papers: Iterable[Paper]) -> list[QAIssue]:
    """来源权威性:官方 API 源必须有 provenance 字段;USER_IMPORTED 的 raw_citation
    必须非空(否则无法核验真实性)。"""
    issues: list[QAIssue] = []
    for paper in papers:
        if paper.source in _NEED_PROVENANCE_OFFICIAL and not paper.provenance:
            issues.append(QAIssue(
                "ACCURACY_NO_PROVENANCE", QACheckStatus.WARN,
                "provenance", f"paper/{paper.lit_id}",
                "官方源条目缺 provenance 字段,溯源链不可见",
                lit_id=paper.lit_id,
            ))
        if paper.source == Source.USER_IMPORTED and not (paper.raw_citation or "").strip():
            issues.append(QAIssue(
                "ACCURACY_NO_RAW_CITATION", QACheckStatus.FAIL,
                "raw_citation", f"paper/{paper.lit_id}",
                "中文条目缺少原始引文,无法核验真实性",
                lit_id=paper.lit_id,
            ))
    return issues


def _check_year_sanity(papers: Iterable[Paper], max_year: int) -> list[QAIssue]:
    """防止年份穿越:不能让 2026 年以后的论文(还没出版)出现在 2026 年综述里。"""
    issues: list[QAIssue] = []
    for paper in papers:
        if paper.year and paper.year > max_year:
            issues.append(QAIssue(
                "ACCURACY_FUTURE_YEAR", QACheckStatus.FAIL,
                "year", f"paper/{paper.lit_id}",
                f"文献年份 {paper.year} 晚于本年 ({max_year}),疑似虚假或论文信息有误",
                lit_id=paper.lit_id,
            ))
    return issues


def check_citation_accuracy(
    papers: list[Paper],
    *,
    max_year: int | None = None,
) -> QACheckResult:
    """对外的统一入口。返回结构化结果,不抛异常(异常由上游报告层捕获)。

    - papers   : 入选写作的全部文献(quota_papers)
    - max_year: 用于年份上限检查;默认 2026。
    """
    import datetime
    if max_year is None:
        max_year = datetime.datetime.now().year

    result = QACheckResult(
        check_id="accuracy_001",
        name="文献引用准确性核查",
        status=QACheckStatus.PASS,
        metrics={
            "inspected": len(papers),
            "by_source": dict(Counter(p.source.value for p in papers)),
            "max_year_allowed": max_year,
        },
    )

    all_issues: list[QAIssue] = []
    all_issues.extend(_check_metadata_completeness(papers))
    all_issues.extend(_check_dup_identity(papers))
    all_issues.extend(_check_source_authority(papers))
    all_issues.extend(_check_year_sanity(papers, max_year))

    has_fail = any(i.severity == QACheckStatus.FAIL for i in all_issues)
    has_warn = any(i.severity == QACheckStatus.WARN for i in all_issues)
    result.issues = all_issues
    result.metrics["issues_count"] = len(all_issues)
    result.metrics["fail_count"] = sum(1 for i in all_issues if i.severity == QACheckStatus.FAIL)
    result.metrics["warn_count"] = sum(1 for i in all_issues if i.severity == QACheckStatus.WARN)

    if has_fail:
        result.status = QACheckStatus.FAIL
        result.notes.append("存在真实性失败项,阻断最终输出")
    elif has_warn:
        result.status = QACheckStatus.WARN
        result.notes.append("存在真实性告警项,需人工复核")
    else:
        result.notes.append("全部入选文献元数据完整、来源可溯")

    log.info(
        "accuracy check: inspected=%d issues=%d fail=%d warn=%d status=%s",
        result.metrics["inspected"], result.metrics["issues_count"],
        result.metrics["fail_count"], result.metrics["warn_count"], result.status.value,
    )
    return result
