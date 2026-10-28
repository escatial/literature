"""LLM prompt/响应落盘(治日志盲区)。

线上排障长期痛点:prompt 组装与模型原始输出只在内存中穿行,出了问题
只能靠猜。本模块把关键 stage 的 prompt/响应原文同步落盘:

    backend/outputs/llm_traces/<task_id>/<stage>_<n>.txt

- task_id 经 contextvar(仿 llm.client.last_used_provider 先例),由
  orchestrator 两个 stream 入口绑定;缺省落 "adhoc"(直调/单测);
- 同步 append 写入;任何异常吞掉仅 warning,绝不阻塞主流程;
- stage ∈ readthrough / axis_plan / section_notes(可 additive 扩展);
- 根目录可用 LLM_TRACE_DIR 覆盖(测试隔离)。

注意:本模块被 classifier 与 orchestrator 共同消费,导入一律写死
``src.writing.llm_trace`` 单实例路径,保证 contextvar 只有一份
(双模块树下 writing.llm_trace 与 src.writing.llm_trace 会是两个
模块实例,各持一个 ContextVar,绝不能混用)。
"""
from __future__ import annotations

import logging
import os
import re
import threading
from contextvars import ContextVar
from pathlib import Path

logger = logging.getLogger(__name__)

_task_id_ctx: ContextVar[str | None] = ContextVar("llm_trace_task_id", default=None)

# (task, stage) -> 已写序号;同进程内单调递增,防同名覆盖
_counters: dict[tuple[str, str], int] = {}
_write_lock = threading.Lock()

_SAFE_RE = re.compile(r"[^0-9A-Za-z_.\-]+")


def set_task_id(task_id: str | None):
    """绑定当前任务的 trace 目录名,返回 token 供 reset_task_id 恢复。"""
    return _task_id_ctx.set(str(task_id) if task_id else None)


def reset_task_id(token) -> None:
    """恢复 set_task_id 之前的 contextvar 状态(worker 线程复用防泄漏)。"""
    try:
        _task_id_ctx.reset(token)
    except Exception as exc:  # noqa: BLE001
        logger.debug("llm_trace task_id reset 失败(忽略): %s", exc)


def current_task_id() -> str | None:
    return _task_id_ctx.get()


def _trace_root() -> Path:
    override = (os.environ.get("LLM_TRACE_DIR") or "").strip()
    if override:
        return Path(override)
    # llm_trace.py 位于 backend/src/writing/ → parents[2] = backend
    return Path(__file__).resolve().parents[2] / "outputs" / "llm_traces"


def record(stage: str, label: str, prompt: str, response: str) -> None:
    """同步落盘一次 LLM 交互(prompt + 响应原文)。绝不抛异常。

    stage: 业务阶段名(readthrough/axis_plan/section_notes...);
    label: 同 stage 内的子标识(如 "chunk3"/"attempt1"/章节 key)。
    """
    try:
        task_id = _task_id_ctx.get() or "adhoc"
        safe_task = _SAFE_RE.sub("_", task_id)[:80] or "adhoc"
        safe_stage = _SAFE_RE.sub("_", stage)[:40] or "stage"
        with _write_lock:
            key = (safe_task, safe_stage)
            _counters[key] = _counters.get(key, 0) + 1
            n = _counters[key]
        target = _trace_root() / safe_task
        target.mkdir(parents=True, exist_ok=True)
        path = target / f"{safe_stage}_{n}.txt"
        body = (
            f"=== stage: {stage} | label: {label} | task: {task_id} ===\n"
            f"=== PROMPT ===\n{prompt or ''}\n\n=== RESPONSE ===\n{response or ''}\n"
        )
        with open(path, "a", encoding="utf-8", errors="replace") as fh:
            fh.write(body)
    except Exception as exc:  # noqa: BLE001
        logger.warning("LLM trace 落盘失败(stage=%s): %s", stage, exc)


__all__ = ["set_task_id", "reset_task_id", "current_task_id", "record"]
