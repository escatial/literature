"""核查流水线 hook:把 qa.runner 的检查嵌入到 writing.orchestrator。

设计原则:
- 不阻塞单元测试(测试可在 mocks 之后单独断言)。
- 默认在 writing 完成 apply_citation_numbering 之后再统一执行四项核查。
- 提供可注入的"硬失败回调",供上层决定是否要 raise / 仅记录。
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable

from qa.report import render_json_report, render_markdown_report
from qa.runner import QARunner, run_all
from qa.rules import QACheckStatus, QARuleSet, default_rule_set
from retrieval.types import Paper
from writing.orchestrator import SectionResult


# 前向引用:OnFailHandler 在执行时才解析,避免循环 import。
if TYPE_CHECKING:  # pragma: no cover
    from qa.models import QASummary


log = logging.getLogger(__name__)


# 默认"硬失败"回调:抛 ValueError 让 orchestrator 抛错并被 API 捕获。
OnFailHandler = Callable[["QASummary"], None]


def _raise_on_fail(summary: "QASummary") -> None:
    fail_names = ", ".join(r.name for r in summary.results if r.status == QACheckStatus.FAIL)
    raise ValueError(
        f"文献综述全流程核查未通过({fail_names or '未知项'});"
        "所有核查项的通过率必须达到 100% 才能输出最终结果"
    )


def run_post_write_qa(
    *,
    runner: QARunner,
    papers: list[Paper],
    sections: list[SectionResult],
    reference_list: str,
    grades: dict[str, str] | None = None,
    on_fail: OnFailHandler | None = None,
) -> dict[str, Any]:
    """在 apply_citation_numbering 之后调用,完成四项核查 + 报告渲染。

    返回值包含 summary.to_dict() 与 markdown_report,供上层选用。
    若 overall == FAIL 且提供 on_fail 则回调(默认抛错)。
    """
    summary = runner.run(
        papers=papers,
        sections=sections,
        reference_list=reference_list,
        grades=grades,
    )
    markdown_report = render_markdown_report(summary)
    payload = {
        "summary": summary.to_dict(),
        "markdown_report": markdown_report,
        "json_report": render_json_report(summary),
    }
    log.info(
        "post-write QA done: overall=%s pass_rate=%.2f%% checks=%d",
        summary.overall.value, summary.pass_rate * 100, len(summary.results),
    )
    if summary.overall == QACheckStatus.FAIL:
        if on_fail is None:
            _raise_on_fail(summary)
        else:
            on_fail(summary)
    return payload


def attach_post_write_qa(
    *,
    ruleset: QARuleSet | None = None,
    on_fail: DefaultFailHandler | None = None,
) -> dict[str, Any]:
    """为 orchestrator.generate_review 等高层调用提供的便捷工厂。

    用法::
        output = attach_post_write_qa()
        # output 内部是 closure,在调用时收集 papers/sections/ref
    """
    rs = ruleset or default_rule_set
    state: dict[str, Any] = {"papers": [], "sections": [], "ref_list": "", "grades": None}

    def _set(papers: list[Paper], sections: list[SectionResult], reference_list: str,
             grades: dict[str, str] | None = None) -> None:
        state["papers"] = papers
        state["sections"] = sections
        state["ref_list"] = reference_list
        state["grades"] = grades

    def _run() -> dict[str, Any]:
        return run_post_write_qa(
            runner=QARunner(rs),
            papers=state["papers"],
            sections=state["sections"],
            reference_list=state["ref_list"],
            grades=state["grades"],
            on_fail=on_fail,
        )

    return {"set": _set, "run": _run}
