"""文献综述 Agent Harness。

统一管理阶段状态、检查结果、修复轮次和最终交付门禁。业务模块只负责
执行一个动作；Harness 负责判断动作是否可以提交，避免“检查失败但仍完成”。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Generic, TypeVar


class AgentStage(str, Enum):
    PLAN = "plan"
    SCREEN = "screen"
    THEME = "theme"
    WAIT_CONFIRM = "wait_confirm"
    WRITE = "write"
    CITATION = "citation"
    QA = "qa"
    COMPLETE = "complete"
    FAILED = "failed"
    BLOCKED = "blocked"


class AgentStatus(str, Enum):
    RUNNING = "running"
    WAITING = "waiting"
    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class HarnessPolicy:
    """每个 Agent 循环的硬边界。"""

    max_attempts: int = 3
    max_elapsed_seconds: float = 600.0
    require_pass: bool = True
    allow_partial_output: bool = False


@dataclass
class AgentCheck:
    name: str
    passed: bool
    issues: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentState:
    task_id: str
    topic: str
    stage: AgentStage = AgentStage.PLAN
    status: AgentStatus = AgentStatus.RUNNING
    attempt: int = 0
    started_at: float = field(default_factory=time.monotonic)
    checks: list[AgentCheck] = field(default_factory=list)
    repairs: list[dict[str, Any]] = field(default_factory=list)
    trace: list[dict[str, Any]] = field(default_factory=list)
    output: Any = None
    error: str | None = None

    @property
    def elapsed_seconds(self) -> float:
        return time.monotonic() - self.started_at

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(check.passed for check in self.checks)

    def record(self, event: str, **data: Any) -> None:
        self.trace.append({
            "at": time.time(),
            "stage": self.stage.value,
            "attempt": self.attempt,
            "event": event,
            **data,
        })


T = TypeVar("T")


class AgentLoopError(RuntimeError):
    """Agent 达到停止条件，不能安全交付结果。"""


def inspect_theme_groups(groups: list[Any], papers: list[Any]) -> list[AgentCheck]:
    """主题方案的通用结构检查，不依赖具体学科词表。"""
    expected = {getattr(paper, "lit_id", "") for paper in papers}
    memberships = [
        getattr(group, "lit_ids", [])
        for group in groups
    ]
    flat = [lit_id for ids in memberships for lit_id in ids]
    duplicates = sorted({lit_id for lit_id in flat if flat.count(lit_id) > 1})
    covered = set(flat)
    names = [str(getattr(group, "name", "")).strip() for group in groups]
    sizes = [len(ids) for ids in memberships]
    total = sum(sizes)
    largest = max(sizes, default=0)
    # 规模失衡:大池子里最大主题过半(如 90 篇里 75 篇挤进一个泛化主题),
    # 既写不出章节也让其余主题失去对照意义,必须打回重新划分。
    balance_ok = total < 30 or 2 * largest <= total
    checks = [
        AgentCheck(
            "theme_count",
            len(papers) < 9 or len(groups) >= 2,
            [f"主题数量为 {len(groups)}，文献规模需要至少 2 个主题"]
            if len(papers) >= 9 and len(groups) < 2 else [],
            {"groups": len(groups), "papers": len(papers)},
        ),
        AgentCheck(
            "theme_balance",
            balance_ok,
            [f"最大主题 {largest} 篇，占文献池 {round(largest / total * 100)}%，规模失衡"]
            if not balance_ok else [],
            {"largest": largest, "total": total},
        ),
        AgentCheck(
            "theme_coverage",
            covered == expected,
            [f"有 {len(expected - covered)} 篇文献未归组"] if covered != expected else [],
            {"covered": len(covered), "expected": len(expected)},
        ),
        AgentCheck(
            "theme_unique_membership",
            not duplicates,
            [f"文献重复归组: {', '.join(duplicates[:5])}"] if duplicates else [],
            {"duplicates": len(duplicates)},
        ),
        AgentCheck(
            "theme_names",
            bool(names) and all(names) and len(set(names)) == len(names),
            ["主题名称为空或重复"] if not names or not all(names) or len(set(names)) != len(names) else [],
            {"names": names},
        ),
    ]
    # 组名质量(碎片/占位/纯英文)走 classifier 同一套校验:此前结构核查
    # 只查非空+唯一,标题碎片与英文章节名能通过 harness 进入确认面板
    # (线上案例:主题 3 = "Sea Table Role Traceability")。
    try:
        from writing.classifier import (
            _group_name_acceptable, _pool_dominant_language, _zh_theme_name_too_short,
        )
        pool_lang = _pool_dominant_language(papers)
        bad_names = [
            name for name in names
            if not _group_name_acceptable(name, pool_language=pool_lang)
            or _zh_theme_name_too_short(name, pool_lang)
        ]
        checks.append(AgentCheck(
            "theme_name_quality",
            not bad_names,
            [f"主题名称不可靠: {', '.join(bad_names[:3])}"] if bad_names else [],
            {"bad_names": bad_names},
        ))
    except Exception:  # noqa: BLE001
        # 校验器不可用时保持原检查集,不让结构核查本身失败
        pass
    return checks


def inspect_section_result(result: Any, papers: list[Any], *, is_comment: bool = False) -> list[AgentCheck]:
    """章节交付前的结构检查。"""
    content = str(getattr(result, "content", "") or "").strip()
    citations = list(getattr(result, "citations", []) or [])
    allowed = {getattr(paper, "lit_id", "") for paper in papers}
    invalid = sorted(set(citations) - allowed)
    issues: list[AgentCheck] = [
        AgentCheck("section_content", bool(content) and len(content) >= 80, ["章节正文为空或过短"] if not content or len(content) < 80 else [], {"chars": len(content)}),
        AgentCheck("section_citations", is_comment or bool(citations), ["章节没有可验证引用"] if not is_comment and not citations else [], {"citations": len(citations)}),
        AgentCheck("section_citation_ids", not invalid, [f"存在无效文献锚点: {', '.join(invalid[:5])}"] if invalid else [], {"invalid": len(invalid)}),
        AgentCheck("section_density", not bool(getattr(result, "density_warning", False)), ["章节引用密度未达到要求"] if getattr(result, "density_warning", False) else [], {}),
    ]
    return issues


def run_repair_loop(
    state: AgentState,
    *,
    stage: AgentStage,
    policy: HarnessPolicy,
    produce: Callable[[AgentState], T],
    inspect: Callable[[T, AgentState], list[AgentCheck]],
    repair: Callable[[T, list[AgentCheck], AgentState], T] | None = None,
) -> T:
    """执行“生成 -> 核查 -> 修复 -> 再核查”循环。

    该函数不理解文献领域内容，只处理 Agent 的控制逻辑，因此适用于主题、
    正文、引用和全文 QA 等不同阶段。
    """
    state.stage = stage
    last_output: T | None = None
    last_checks: list[AgentCheck] = []
    for attempt in range(1, policy.max_attempts + 1):
        state.attempt = attempt
        if state.elapsed_seconds > policy.max_elapsed_seconds:
            break
        state.record("attempt_started")
        try:
            if last_output is None:
                last_output = produce(state)
            checks = inspect(last_output, state)
        except Exception as exc:  # noqa: BLE001
            state.error = str(exc)
            state.record("attempt_error", error=str(exc))
            checks = [AgentCheck("runtime", False, [str(exc)])]
        state.checks = checks
        last_checks = checks
        state.record(
            "inspection_completed",
            passed=all(check.passed for check in checks),
            issues=[issue for check in checks for issue in check.issues],
        )
        if checks and all(check.passed for check in checks):
            state.status = AgentStatus.PASSED
            state.record("stage_passed")
            return last_output
        if repair is None or attempt >= policy.max_attempts:
            break
        try:
            last_output = repair(last_output, checks, state)
            state.repairs.append({
                "stage": stage.value,
                "attempt": attempt,
                "issues": [issue for check in checks for issue in check.issues],
            })
            state.record("repair_applied")
        except Exception as exc:  # noqa: BLE001
            state.error = str(exc)
            state.record("repair_error", error=str(exc))
            break

    state.status = AgentStatus.BLOCKED if last_checks else AgentStatus.FAILED
    state.stage = AgentStage.BLOCKED if state.status == AgentStatus.BLOCKED else AgentStage.FAILED
    state.error = state.error or "Agent 在限定轮次或时间内未通过质量核查"
    state.record("stage_failed", error=state.error)
    if policy.require_pass and not policy.allow_partial_output:
        raise AgentLoopError(state.error)
    return last_output  # type: ignore[return-value]


def completion_payload(state: AgentState) -> dict[str, Any]:
    """生成可持久化、可供 SSE 返回的 Agent 运行报告。"""
    return {
        "task_id": state.task_id,
        "topic": state.topic,
        "stage": state.stage.value,
        "status": state.status.value,
        "attempt": state.attempt,
        "elapsed_seconds": round(state.elapsed_seconds, 2),
        "checks": [check.__dict__ for check in state.checks],
        "repairs": list(state.repairs),
        "trace": list(state.trace),
        "error": state.error,
    }
