"""任务级 LLM 耗时归因(P0-6,2026-11-18)。

目标:回答「一次任务的总耗时里,LLM 调用占多少、其余(本地计算/空转)
占多少」——这是决定要不要做分章并行/多 agent 的归因数据。

机制:
- llm.client.messages_create 在每次调用(含 fallback 链)结束时累计
  add_llm_call(seconds, ok);
- 任务侧(写作 worker / 检索后台线程)用 bind_task_stats() 绑定本任务的
  累计器(与 set_runtime_llm 同一 ContextVar 模式,线程隔离),终态时
  snapshot() 读出并写进轨迹/质量单。

注意:ContextVar 按线程上下文隔离;未绑定时 add_llm_call 是 no-op,
不影响无任务语境的 LLM 调用(如 API 层的同步测试调用)。
"""
from __future__ import annotations

import threading
from contextvars import ContextVar
from typing import Any

_stats: ContextVar[dict[str, Any] | None] = ContextVar("task_phase_stats", default=None)
_lock = threading.Lock()


def bind_task_stats(task_id: str) -> object:
    """为当前线程上下文绑定任务级累计器,返回 reset 用的 token。"""
    return _stats.set({
        "task_id": task_id,
        "llm_calls": 0,
        "llm_failures": 0,
        "llm_seconds": 0.0,
    })


def reset_task_stats(token: object) -> None:
    """恢复绑定前的上下文(与 set_runtime_llm/reset 配对使用)。"""
    _stats.reset(token)  # type: ignore[arg-type]


def add_llm_call(seconds: float, ok: bool) -> None:
    """llm.client 每次调用结束时上报。未绑定任务时 no-op。"""
    stats = _stats.get()
    if stats is None:
        return
    with _lock:
        stats["llm_calls"] += 1
        stats["llm_seconds"] += max(0.0, float(seconds))
        if not ok:
            stats["llm_failures"] += 1


def snapshot() -> dict[str, Any] | None:
    """读取当前任务的累计快照;未绑定时返回 None。"""
    stats = _stats.get()
    if stats is None:
        return None
    with _lock:
        return dict(stats)
