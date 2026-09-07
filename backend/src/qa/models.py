"""统一的核查项与汇总结果数据结构。"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any

from qa.rules import QACheckStatus


@dataclass
class QAIssue:
    """一条具体问题(可溯源:定位到 section_key / lit_id / 文本片段)。

    - code   : 机器可读的错误码,例如 CITATION_FORMAT_LANG_MIX / BINDING_DUP
    - field  : 受影响字段(citation / content / authors / year / pages …)
    - path   : 受影响的物理定位(便于前端跳转),"section:theme_2 / [lit_oa_3]"
    - message: 人可读的说明
    """

    code: str
    severity: QACheckStatus
    field: str
    path: str
    message: str
    lit_id: str | None = None
    section_key: str | None = None
    snippet: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "severity": self.severity.value,
        }


@dataclass
class QACheckResult:
    """单条核查项的产出。

    字段:
    - check_id  : 类目编号(accuracy_001 / binding_001 …)
    - name      : 中文名
    - status    : PASS / WARN / FAIL / SKIPPED
    - metrics   : 量化指标(用于渲染报告)
    - issues    : 问题列表
    - notes     : 备注(给前端/运维看)
    """

    check_id: str
    name: str
    status: QACheckStatus
    metrics: dict[str, Any] = field(default_factory=dict)
    issues: list[QAIssue] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def add_issue(
        self,
        code: str,
        field: str,
        path: str,
        message: str,
        lit_id: str | None = None,
        section_key: str | None = None,
        snippet: str | None = None,
        severity: QACheckStatus | None = None,
    ) -> None:
        """统一加问题,warn 默认走 WARN,其它情况按调用方指定。"""
        self.issues.append(
            QAIssue(
                code=code,
                severity=severity or self.status,
                field=field,
                path=path,
                message=message,
                lit_id=lit_id,
                section_key=section_key,
                snippet=snippet,
            )
        )

    @property
    def is_pass(self) -> bool:
        return self.status == QACheckStatus.PASS

    def to_dict(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "name": self.name,
            "status": self.status.value,
            "metrics": dict(self.metrics),
            "issues": [issue.to_dict() for issue in self.issues],
            "notes": list(self.notes),
        }


@dataclass
class QASummary:
    """整次核查的汇总。

    - results     : 每类核查的结果
    - overall     : 总体状态(PASS / WARN / FAIL)
    - pass_rate   : PASS 数 / 已执行数
    - required_pass_rate: 规则要求的通过率下界
    - elapsed_ms  : 总耗时
    """

    results: list[QACheckResult] = field(default_factory=list)
    overall: QACheckStatus = QACheckStatus.PASS
    pass_rate: float = 1.0
    required_pass_rate: float = 1.0
    elapsed_ms: int = 0

    @property
    def issues(self) -> list[QAIssue]:
        return [issue for r in self.results for issue in r.issues]

    @property
    def failed_checks(self) -> list[str]:
        return [r.check_id for r in self.results if r.status == QACheckStatus.FAIL]

    def to_dict(self) -> dict[str, Any]:
        return {
            "overall": self.overall.value,
            "pass_rate": round(self.pass_rate, 4),
            "required_pass_rate": self.required_pass_rate,
            "elapsed_ms": self.elapsed_ms,
            "results": [r.to_dict() for r in self.results],
            "failed_checks": list(self.failed_checks),
            "issues": [i.to_dict() for i in self.issues],
        }
