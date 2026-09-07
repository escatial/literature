"""合规性报告输出:JSON(机读)与 Markdown(人读/审计/归档)双格式。"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from qa.models import QASummary
from qa.rules import QACheckStatus


_STATUS_LABEL = {
    QACheckStatus.PASS: "通过",
    QACheckStatus.WARN: "告警",
    QACheckStatus.FAIL: "不通过",
    QACheckStatus.SKIPPED: "跳过",
}


def render_json_report(summary: QASummary) -> str:
    """生成机读 JSON 报告,可被前端 dashboard / ELK 收集。"""
    payload: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        **summary.to_dict(),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _metric_lines(metrics: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for key, value in metrics.items():
        if isinstance(value, (dict, list)):
            lines.append(f"- {key}: {json.dumps(value, ensure_ascii=False)}")
        else:
            lines.append(f"- {key}: {value}")
    return lines


def render_markdown_report(summary: QASummary) -> str:
    """生成可粘贴到周报/审计文档的 Markdown 报告。"""
    lines: list[str] = []
    lines.append("# 文献综述全流程核查报告")
    lines.append("")
    lines.append(f"- 总体状态:**{_STATUS_LABEL[summary.overall]}**")
    lines.append(f"- 通过率:`{summary.pass_rate:.2%}` "
                 f"(要求 ≥ {summary.required_pass_rate:.0%})")
    lines.append(f"- 耗时:{summary.elapsed_ms} ms")
    lines.append(f"- 生成时间(UTC):{datetime.now(timezone.utc).isoformat()}")
    lines.append("")

    # 汇总表
    lines.append("## 核查项汇总")
    lines.append("")
    lines.append("| 编号 | 名称 | 状态 | 失败 | 告警 |")
    lines.append("| --- | --- | --- | --- | --- |")
    for r in summary.results:
        fails = sum(1 for i in r.issues if i.severity == QACheckStatus.FAIL)
        warns = sum(1 for i in r.issues if i.severity == QACheckStatus.WARN)
        lines.append(
            f"| {r.check_id} | {r.name} | {_STATUS_LABEL[r.status]} "
            f"| {fails} | {warns} |"
        )
    lines.append("")

    # 详细产出
    for r in summary.results:
        lines.append(f"## {r.check_id} {r.name}")
        lines.append("")
        lines.append(f"状态:**{_STATUS_LABEL[r.status]}**")
        lines.append("")
        if r.metrics:
            lines.append("### 关键指标")
            lines.extend(_metric_lines(r.metrics))
            lines.append("")
        if r.notes:
            lines.append("### 备注")
            for n in r.notes:
                lines.append(f"- {n}")
            lines.append("")
        if r.issues:
            lines.append("### 问题清单")
            lines.append("")
            lines.append("| 错误码 | 字段 | 定位 | 严重度 | 说明 |")
            lines.append("| --- | --- | --- | --- | --- |")
            for issue in r.issues:
                snippet = (issue.snippet or "")[:50].replace("|", "/")
                lines.append(
                    f"| `{issue.code}` | {issue.field} | {issue.path} | "
                    f"{_STATUS_LABEL[issue.severity]} | "
                    f"{issue.message} {'`' + snippet + '`' if snippet else ''} |"
                )
            lines.append("")
    return "\n".join(lines)
