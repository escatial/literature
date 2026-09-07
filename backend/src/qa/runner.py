"""核查执行机制:统一调度 + 异常告警 + 阈值判定 + 可追溯。

设计要点:
- 每项核查是纯函数,无副作用,异常被 try/except 收纳为单条 SKIPPED 结果。
- QARunner 集中收集所有检查结果,根据 QARuleSet.required_pass_rate 等阈值
  计算总体状态。
- 提供 run_all(sections, papers, ref_list, grades=None) 同步便捷入口,供
  orchestrator 与 API 层直接调用。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from qa.accuracy import check_citation_accuracy
from qa.binding import check_reference_binding
from qa.citation_format import check_citation_format
from qa.quota import check_literature_quota
from qa.models import QACheckResult, QASummary
from qa.rules import QACheckStatus, QARuleSet, default_rule_set
from retrieval.types import Paper
from writing.orchestrator import SectionResult


log = logging.getLogger(__name__)


@dataclass
class QARunner:
    """统一的核查调度器。"""

    ruleset: QARuleSet

    def run(
        self,
        *,
        papers: list[Paper],
        sections: list[SectionResult],
        reference_list: str,
        grades: dict[str, str] | None = None,
        max_year: int | None = None,
        base_year: int | None = None,
    ) -> QASummary:
        """按规则集顺序触发各项核查。

        所有异常被捕获并归一为 SKIPPED 的检查项,保证流程不会被单一抛错阻断。
        """
        started = time.monotonic()
        results: list[QACheckResult] = []
        executed = 0

        # 1) 准确性
        if self.ruleset.check_accuracy:
            try:
                r = check_citation_accuracy(papers, max_year=max_year)
                results.append(r)
                executed += 1
            except Exception as exc:  # noqa: BLE001
                log.exception("accuracy check raised")
                results.append(_skipped("accuracy_001", "文献引用准确性核查", str(exc)))
            if self.ruleset.fail_fast and results[-1].status == QACheckStatus.FAIL:
                return _summarize(results, started, executed, self.ruleset)

        # 2) 关联一致性
        if self.ruleset.check_binding:
            try:
                r = check_reference_binding(sections, reference_list, papers)
                results.append(r)
                executed += 1
            except Exception as exc:  # noqa: BLE001
                log.exception("binding check raised")
                results.append(_skipped("binding_001", "正文-文献关联一致性核查", str(exc)))
            if self.ruleset.fail_fast and results[-1].status == QACheckStatus.FAIL:
                return _summarize(results, started, executed, self.ruleset)

        # 3) 数量合规性
        if self.ruleset.check_quota:
            try:
                r = check_literature_quota(
                    papers,
                    thresholds=self.ruleset.quota,
                    grades=grades,
                    base_year=base_year,
                )
                results.append(r)
                executed += 1
            except Exception as exc:  # noqa: BLE001
                log.exception("quota check raised")
                results.append(_skipped("quota_001", "文献数量合规性核查", str(exc)))
            if self.ruleset.fail_fast and results[-1].status == QACheckStatus.FAIL:
                return _summarize(results, started, executed, self.ruleset)

        # 4) 格式规范性
        if self.ruleset.check_citation_format:
            try:
                r = check_citation_format(papers, style_id=self.ruleset.citation_style)
                results.append(r)
                executed += 1
            except Exception as exc:  # noqa: BLE001
                log.exception("citation_format check raised")
                results.append(_skipped("citation_format_001", "引用格式规范性核查", str(exc)))

        return _summarize(results, started, executed, self.ruleset)


def _skipped(check_id: str, name: str, reason: str) -> QACheckResult:
    return QACheckResult(
        check_id=check_id,
        name=name,
        status=QACheckStatus.SKIPPED,
        notes=[f"异常: {reason}"],
    )


def _summarize(
    results: list[QACheckResult],
    started: float,
    executed: int,
    ruleset: QARuleSet,
) -> QASummary:
    """汇总并判定总体状态。"""
    # 总通过率 = PASS / 已执行 (SKIPPED 不计入分母,避免影响判定)
    passed = sum(1 for r in results if r.status == QACheckStatus.PASS)
    pass_rate = passed / executed if executed else 1.0
    overall_status = (
        QACheckStatus.PASS
        if pass_rate >= ruleset.required_pass_rate
        and all(r.status != QACheckStatus.FAIL for r in results)
        else QACheckStatus.FAIL
    )
    # 若有 WARN 但未触发 FAIL,且通过率达到阈值,可降级为 WARN(不放行)
    if overall_status == QACheckStatus.PASS and any(
        r.status == QACheckStatus.WARN for r in results
    ):
        overall_status = QACheckStatus.WARN
    elapsed_ms = int((time.monotonic() - started) * 1000)
    return QASummary(
        results=results,
        overall=overall_status,
        pass_rate=pass_rate,
        required_pass_rate=ruleset.required_pass_rate,
        elapsed_ms=elapsed_ms,
    )


def run_all(
    *,
    papers: list[Paper],
    sections: list[SectionResult],
    reference_list: str,
    grades: dict[str, str] | None = None,
    ruleset: QARuleSet | None = None,
    max_year: int | None = None,
    base_year: int | None = None,
) -> QASummary:
    """顶层便捷函数;默认使用 qa.rules.default_rule_set。"""
    if ruleset is None:
        ruleset = default_rule_set
    return QARunner(ruleset).run(
        papers=papers,
        sections=sections,
        reference_list=reference_list,
        grades=grades,
        max_year=max_year,
        base_year=base_year,
    )
