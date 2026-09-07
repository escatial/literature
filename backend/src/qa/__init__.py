"""文献综述全流程核查 Agent(本包对外暴露)。

四类核查:
1. 文献引用准确性        qa.accuracy
2. 正文-文献关联一致性    qa.binding
3. 文献数量合规性        qa.quota
4. 引用格式规范性        qa.citation_format

执行机制:
- qa.rules / qa.runner    统一调度 + 阈值告警
- qa.report               合规性报告输出(JSON / Markdown)
- qa.hooks                与 writing 流水线集成的触发点

约定:所有复核函数纯函数化,只读取入参,返回结构化结果,不修改原数据。
"""
from __future__ import annotations

from qa.rules import (
    QuotaThresholds,
    QACheckStatus,
    QARuleSet,
    default_rule_set,
)
from qa.runner import QARunner, QASummary, run_all
from qa.report import render_json_report, render_markdown_report


__all__ = [
    "QuotaThresholds",
    "QACheckStatus",
    "QARuleSet",
    "default_rule_set",
    "QARunner",
    "QASummary",
    "run_all",
    "render_json_report",
    "render_markdown_report",
]
