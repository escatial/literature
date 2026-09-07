"""核查3:文献数量合规性。

执行节点:quotation 选定 (apply_relevance_quota) 之后、apply_citation_numbering 之前。

目的:提前配置目标场景要求的文献数量区间、核心文献占比、近年文献占比等
量化指标,自动统计实际引用的文献总量及分类数量,校验是否完全符合预设
的合规要求。

与 orchestrator._select_reference_quota / _validate_reference_quota 不同:
- 现有实现是"挑选"阶段的硬卡(直接抛异常,流程终止)。
- 本核查是"事后审计"——即便 _validate_reference_quota 没拦住,
  在最终输出前再补一遍可配置阈值的体检,产出可追溯报告。
"""
from __future__ import annotations

import datetime
import logging
from collections import Counter
from typing import Iterable

from qa.models import QACheckResult, QAIssue
from qa.rules import QACheckStatus, QuotaThresholds
from retrieval.types import Paper, Source


log = logging.getLogger(__name__)


_CHINESE_SOURCES: frozenset[Source] = frozenset({Source.CNKI, Source.USER_IMPORTED})


def _current_year() -> int:
    return datetime.datetime.now().year


def _classify_chinese(p: Paper) -> bool:
    return p.source in _CHINESE_SOURCES


def _count_recent(papers: Iterable[Paper], recent_years: int, base_year: int) -> tuple[int, int]:
    """返回 (近年文献数, 有效年份文献总数)。没有年份的不计入分母。"""
    recent = 0
    has_year = 0
    for p in papers:
        if not p.year:
            continue
        has_year += 1
        if base_year - p.year <= recent_years:
            recent += 1
    return recent, has_year


def _count_core(papers: Iterable[Paper], grades: dict[str, str] | None) -> tuple[int, int]:
    """返回 (核心文献数, 入选文献数)。

    grades: orchestrator._grade_map 的输出,键=lit_id,值="high"/"medium"/"low"。
    缺失 grades 时不做硬卡,只汇报现状(避免越权断言)。
    """
    if not grades:
        return 0, len(papers)
    core = sum(1 for p in papers if grades.get(p.lit_id) == "high")
    return core, len(papers)


def check_literature_quota(
    papers: list[Paper],
    *,
    thresholds: QuotaThresholds,
    grades: dict[str, str] | None = None,
    base_year: int | None = None,
) -> QACheckResult:
    """统一入口。

    - papers     : 入选写入的全部文献
    - thresholds : 预设阈值(可在环境变量或调用方注入)
    - grades     : lit_id -> 相关性等级;可不传,代表"无相关性分级数据"
    - base_year  : 比较"近年"的基准年(默认系统当前年)
    """
    if base_year is None:
        base_year = _current_year()

    result = QACheckResult(
        check_id="quota_001",
        name="文献数量合规性核查",
        status=QACheckStatus.PASS,
        metrics={
            "total_count": len(papers),
            "thresholds": {
                "total_min": thresholds.total_min,
                "total_max": thresholds.total_max,
                "chinese_ratio_min": thresholds.chinese_ratio_min,
                "english_ratio_max": thresholds.english_ratio_max,
                "core_ratio_min": thresholds.core_ratio_min,
                "recent_years": thresholds.recent_years,
                "recent_ratio_min": thresholds.recent_ratio_min,
            },
        },
    )

    issues: list[QAIssue] = []

    total = len(papers)
    chinese = sum(1 for p in papers if _classify_chinese(p))
    english = total - chinese

    # 1) 总量区间
    if total < thresholds.total_min:
        issues.append(QAIssue(
            "QUOTA_TOTAL_LOW", QACheckStatus.FAIL,
            "total", "literature_pool",
            f"总文献 {total} 低于阈值 {thresholds.total_min}",
        ))
    elif total > thresholds.total_max:
        # 总量上限作为 WARN(超出区间但通常学术综述可接受略多)
        issues.append(QAIssue(
            "QUOTA_TOTAL_HIGH", QACheckStatus.WARN,
            "total", "literature_pool",
            f"总文献 {total} 高于阈值上限 {thresholds.total_max}",
        ))

    # 2) 中英文占比
    if total > 0:
        cn_ratio = chinese / total
        if cn_ratio + 1e-9 < thresholds.chinese_ratio_min:
            issues.append(QAIssue(
                "QUOTA_CN_LOW", QACheckStatus.FAIL,
                "chinese_ratio", "literature_pool",
                f"中文占比 {cn_ratio:.2%} 低于阈值 {thresholds.chinese_ratio_min:.2%}",
            ))
        if english / total > thresholds.english_ratio_max + 1e-9:
            # 英文超 1/3 是 WARN,而不是 FAIL(与 orchestrator 行为对齐)
            issues.append(QAIssue(
                "QUOTA_EN_HIGH", QACheckStatus.WARN,
                "english_ratio", "literature_pool",
                f"英文占比 {english / total:.2%} 高于指导值 {thresholds.english_ratio_max:.2%}",
            ))

        # 3) 核心(高相关)文献占比
        core, evaluated = _count_core(papers, grades)
        if grades and evaluated:
            core_ratio = core / evaluated
            if core_ratio + 1e-9 < thresholds.core_ratio_min:
                issues.append(QAIssue(
                    "QUOTA_CORE_LOW", QACheckStatus.WARN,
                    "core_ratio", "literature_pool",
                    f"核心文献占比 {core_ratio:.2%} 低于建议值 {thresholds.core_ratio_min:.2%}",
                ))

        # 4) 近年文献占比
        recent_n, has_year_n = _count_recent(papers, thresholds.recent_years, base_year)
        if has_year_n:
            recent_ratio = recent_n / has_year_n
            if recent_ratio + 1e-9 < thresholds.recent_ratio_min:
                issues.append(QAIssue(
                    "QUOTA_RECENT_LOW", QACheckStatus.WARN,
                    "recent_ratio", "literature_pool",
                    f"近 {thresholds.recent_years} 年文献占比 {recent_ratio:.2%} "
                    f"低于建议值 {thresholds.recent_ratio_min:.2%}",
                ))

    result.metrics.update({
        "chinese_count": chinese,
        "english_count": english,
        "chinese_ratio": round(chinese / total, 4) if total else 0.0,
        "english_ratio": round(english / total, 4) if total else 0.0,
        "by_source": dict(Counter(p.source.value for p in papers)),
    })
    if grades:
        core, _ = _count_core(papers, grades)
        result.metrics["core_count"] = core
    result.metrics["issues_count"] = len(issues)
    result.metrics["fail_count"] = sum(1 for i in issues if i.severity == QACheckStatus.FAIL)
    result.metrics["warn_count"] = sum(1 for i in issues if i.severity == QACheckStatus.WARN)
    result.issues = issues

    fail_n = result.metrics["fail_count"]
    if fail_n > 0:
        result.status = QACheckStatus.FAIL
        result.notes.append("总量或中英文硬性阈值未达标,阻断最终输出")
    elif result.metrics["warn_count"] > 0:
        result.status = QACheckStatus.WARN
        result.notes.append("核心/近年阈值偏离建议,需复核")
    else:
        result.notes.append("文献数量与结构满足预设合规阈值")

    log.info(
        "quota check: total=%d cn=%d en=%d issues=%d fail=%d warn=%d status=%s",
        total, chinese, english, result.metrics["issues_count"],
        fail_n, result.metrics["warn_count"], result.status.value,
    )
    return result
