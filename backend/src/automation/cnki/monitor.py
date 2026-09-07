#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""任务监控中心：任务注册表 + 运行时参数热调整 + 每任务环形日志 + 全链路计数。

可视化监控面板（前端 CrawlerMonitorPage）与运维 API（api/crawler_admin.py）的
唯一数据源；同时是任务启停的权威状态机：
- api/cnki.py 的取消机制委托本模块（``cancel_event``），
  任务线程通过轮询 ``is_stopped`` 实现协作式停止；
- 运行中参数动态调整：``update_params`` 修改白名单参数并触发注册的回调
  （crawler 集成时把 delay_seconds 热调整接到 throttle_init）。

全链路计数（需求：覆盖请求发起→响应解析→数据落库）：
- requests_total / requests_failed      请求发起层
- parse_errors                          响应解析层
- saved_total / quality_reports         数据落库层（质量评分随落库记账）
- captcha_hits / breaker_trips          反爬应对层
本模块不 import crawler，零网络；线程安全。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field

# 运行中允许热调整的参数白名单（越权键直接拒绝，防面板误操作污染配置）
ADJUSTABLE_PARAMS = {"delay_seconds", "max_workers", "page_size", "max_per_keyword"}

_TASK_STATUSES = ("queued", "running", "done", "stopped", "failed")


@dataclass
class TaskState:
    """单个爬取任务的全生命周期状态。"""

    task_id: str
    query: str = ""
    status: str = "queued"
    stage: str = ""                       # 当前阶段描述（面板实时展示）
    saved: int = 0
    skipped: int = 0
    failed: int = 0
    started_at: float = 0.0
    ended_at: float = 0.0
    params: dict = field(default_factory=dict)     # 运行时参数（可热调整）
    error: str = ""
    counters: dict = field(default_factory=lambda: {
        "requests_total": 0,
        "requests_failed": 0,
        "parse_errors": 0,
        "saved_total": 0,
        "captcha_hits": 0,
        "breaker_trips": 0,
        "proxy_switches": 0,
        "retries": 0,
    })
    quality: dict = field(default_factory=lambda: {
        "count": 0, "avg_score": 0.0, "min_score": 0, "flags": {},
    })
    cancel_event: threading.Event = field(default_factory=threading.Event)
    logs: deque = field(default_factory=lambda: deque(maxlen=500))
    _quality_reports: list = field(default_factory=list)   # 聚合用原始报告

    def is_stopped(self) -> bool:
        """任务线程在关键循环点轮询本方法实现协作式停止。"""
        return self.cancel_event.is_set()

    def to_dict(self, include_logs: bool = False) -> dict:
        """导出为 JSON 可序列化 dict（API 响应体）。"""
        data = {
            "task_id": self.task_id,
            "query": self.query,
            "status": self.status,
            "stage": self.stage,
            "saved": self.saved,
            "skipped": self.skipped,
            "failed": self.failed,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "params": dict(self.params),
            "error": self.error,
            "counters": dict(self.counters),
            "quality": dict(self.quality),
            "elapsed": round(
                ((self.ended_at or time.time()) - self.started_at), 1
            ) if self.started_at else 0.0,
        }
        if include_logs:
            data["logs"] = list(self.logs)
        return data


class TaskRegistry:
    """进程内任务注册表（单例，见 :func:`get_registry`）。"""

    def __init__(self, max_tasks: int = 200):
        self._tasks: dict[str, TaskState] = {}
        self._order: deque[str] = deque(maxlen=int(max_tasks))
        self._lock = threading.Lock()
        self._param_hooks: list = []   # 参数变更回调 [(task_id, patch) -> None]

    # ---- 注册与生命周期 ----
    def register(self, task_id: str, query: str = "", params: dict | None = None) -> TaskState:
        """登记新任务（同 id 重复注册则重置状态——API 层保证 id 唯一，防御性）。

        内存修复(P-2):_order 是 maxlen 有界 deque,满员自动挤出最老 id;
        _tasks 必须同步删除被挤出的条目,否则 dict 无界增长(内存泄漏)。
        """
        with self._lock:
            state = TaskState(task_id=task_id, query=query, params=dict(params or {}))
            self._tasks[task_id] = state
            if task_id not in self._order:
                evicted: str | None = None
                if len(self._order) == self._order.maxlen:
                    evicted = self._order[0]
                self._order.append(task_id)
                if evicted is not None and evicted != task_id:
                    self._tasks.pop(evicted, None)
            return state

    def get(self, task_id: str) -> TaskState | None:
        with self._lock:
            return self._tasks.get(task_id)

    def mark_running(self, task_id: str, stage: str = "") -> None:
        with self._lock:
            t = self._tasks.get(task_id)
            if t:
                t.status = "running"
                t.started_at = t.started_at or time.time()
                if stage:
                    t.stage = stage

    def mark_stage(self, task_id: str, stage: str) -> None:
        """更新阶段描述（请求发起→解析→落库各环节推送，全链路可视）。"""
        with self._lock:
            t = self._tasks.get(task_id)
            if t:
                t.stage = stage

    def _finish(self, task_id: str, status: str, error: str = "") -> None:
        with self._lock:
            t = self._tasks.get(task_id)
            if t:
                t.status = status
                t.ended_at = time.time()
                if error:
                    t.error = error

    def mark_done(self, task_id: str) -> None:
        self._finish(task_id, "done")

    def mark_stopped(self, task_id: str) -> None:
        self._finish(task_id, "stopped")

    def mark_failed(self, task_id: str, error: str = "") -> None:
        self._finish(task_id, "failed", error=error)

    # ---- 进度与计数 ----
    def update_progress(self, task_id: str, saved: int | None = None,
                        skipped: int | None = None, failed: int | None = None) -> None:
        """覆盖式更新进度（调用方传真实值，而非增量——与 adapter 的闭包共享一致）。"""
        with self._lock:
            t = self._tasks.get(task_id)
            if not t:
                return
            if saved is not None:
                t.saved = int(saved)
            if skipped is not None:
                t.skipped = int(skipped)
            if failed is not None:
                t.failed = int(failed)

    def incr(self, task_id: str, counter: str, n: int = 1) -> None:
        """全链路计数器自增（未知计数器名忽略，防止拼错键污染）。"""
        with self._lock:
            t = self._tasks.get(task_id)
            if t and counter in t.counters:
                t.counters[counter] += int(n)

    # ---- 日志 ----
    def append_log(self, task_id: str, msg: str) -> None:
        """任务环形日志（每任务独立，上限 500 条，新日志在尾部）。"""
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        with self._lock:
            t = self._tasks.get(task_id)
            if t:
                t.logs.append(line)

    # ---- 启停控制 ----
    def cancel_event(self, task_id: str) -> threading.Event | None:
        """任务的取消事件（api/cnki.py 的 stop 接口委托到这里）。"""
        with self._lock:
            t = self._tasks.get(task_id)
            return t.cancel_event if t else None

    def stop_task(self, task_id: str) -> bool:
        """请求停止任务：置取消事件。返回任务是否存在。幂等。"""
        with self._lock:
            t = self._tasks.get(task_id)
            if not t:
                return False
            t.cancel_event.set()
            return True

    def stop_all(self) -> int:
        """停止全部运行中任务（运维接口），返回停止数量。"""
        with self._lock:
            running = [t for t in self._tasks.values() if t.status in ("queued", "running")]
            for t in running:
                t.cancel_event.set()
            return len(running)

    # ---- 参数动态调整 ----
    def register_param_hook(self, hook) -> None:
        """注册参数变更回调 hook(task_id, patch)（crawler 热重载限速用）。"""
        with self._lock:
            if hook not in self._param_hooks:
                self._param_hooks.append(hook)

    def update_params(self, task_id: str, patch: dict) -> dict:
        """运行中参数热调整（白名单校验），返回生效后的完整参数。

        :raises KeyError: 任务不存在
        :raises ValueError: patch 含白名单外的键
        """
        illegal = set(patch or {}) - ADJUSTABLE_PARAMS
        if illegal:
            raise ValueError(f"不允许调整的参数: {sorted(illegal)}，白名单: {sorted(ADJUSTABLE_PARAMS)}")
        with self._lock:
            t = self._tasks.get(task_id)
            if not t:
                raise KeyError(f"任务不存在: {task_id}")
            t.params.update(patch)
            hooks = list(self._param_hooks)
        for hook in hooks:
            try:
                hook(task_id, dict(patch))
            except Exception:
                pass  # 钩子失败不影响参数落账（如限速器热重载异常）
        return dict(t.params)

    # ---- 数据质量记账 ----
    def add_quality_report(self, task_id: str, report) -> None:
        """挂一条 QualityReport（quality.assess 的产物），增量聚合任务级质量。"""
        with self._lock:
            t = self._tasks.get(task_id)
            if not t:
                return
            t._quality_reports.append(report)
            t.quality["count"] = len(t._quality_reports)
            scores = [r.score for r in t._quality_reports]
            t.quality["avg_score"] = round(sum(scores) / len(scores), 1)
            t.quality["min_score"] = min(scores)
            flags: dict[str, int] = {}
            for r in t._quality_reports:
                for f in r.flags:
                    flags[f] = flags.get(f, 0) + 1
            t.quality["flags"] = flags

    # ---- 导出 ----
    def list_tasks(self, status: str | None = None) -> list[dict]:
        """任务列表（新→旧；status 过滤）。"""
        with self._lock:
            ids = list(self._order)
            states = [self._tasks[i] for i in reversed(ids) if i in self._tasks]
        if status:
            states = [t for t in states if t.status == status]
        return [t.to_dict() for t in states]

    def snapshot(self, task_id: str, include_logs: bool = True) -> dict | None:
        """单任务全量快照（含日志/计数/质量）。"""
        with self._lock:
            t = self._tasks.get(task_id)
            return t.to_dict(include_logs=include_logs) if t else None


# ========================== 模块级单例 ==========================
_registry: TaskRegistry | None = None
_REG_LOCK = threading.Lock()


def get_registry() -> TaskRegistry:
    """全局注册表（进程内单例；api 层与 crawler 集成层共享）。"""
    global _registry
    with _REG_LOCK:
        if _registry is None:
            _registry = TaskRegistry()
        return _registry


def reset_registry() -> None:
    """重置注册表（仅测试用）。"""
    global _registry
    with _REG_LOCK:
        _registry = None
