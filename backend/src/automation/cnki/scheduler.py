#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""动态负载并发池：按实时负载信号自适应调节并发度的任务调度器。

取代 adapter 里固定 ``ThreadPoolExecutor(max_workers=3)`` 的"盲并发"：
负载信号（滚动窗口统计）→ 期望并发度 → 分批提交，批间重算，实现
"顺水加速、迎风减速"——目标站顺畅时提速到 max_workers，出现风控信号
立即收缩到 1 线程，兼顾爬取效率与对本地/目标站点的资源保护。

负载信号（均可注入，单测零等待零网络）：
- 响应延迟   record_latency：窗口均值 >3s 视为站点变慢 → 收缩
- 失败率     record_failure/record_success：>30% 收缩，<5% 且延迟健康 → 扩张
- 风控信号   record_risk_signal（验证码/限流空壳）：立即降为 min_workers
- 本机 CPU   可选：psutil 存在且 CPU>85% 时压缩上限（无 psutil 则跳过）

调节节流：每次重算至少间隔 ``adjust_interval`` 秒，避免指标抖动引发震荡。
线程安全（adapter 的调用线程与 worker 线程并发记账）。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Iterable

# psutil 可选：无则 CPU 负载代理缺失，仅凭站点侧信号调节
try:
    import psutil  # type: ignore
except ImportError:
    psutil = None


class DynamicWorkerPool:
    """自适应并发池：``run_items`` 消费任务列表，批间动态调整 worker 数。"""

    def __init__(
        self,
        min_workers: int = 1,
        max_workers: int = 6,
        window: int = 20,                # 滚动统计窗口（最近 N 次请求）
        latency_high: float = 3.0,       # 窗口均延迟超过该值 → 收缩(秒)
        latency_healthy: float = 1.5,    # 均延迟低于该值且失败率低 → 扩张
        fail_high: float = 0.30,         # 失败率超过该值 → 收缩
        fail_low: float = 0.05,          # 失败率低于该值 → 具备扩张条件
        cpu_high: float = 0.85,          # 本机 CPU 占用超过该比例 → 压缩上限
        adjust_interval: float = 2.0,    # 重算最小间隔(秒)
        clock: Callable[[], float] = time.monotonic,
    ):
        self.min_workers = max(int(min_workers), 1)
        self.max_workers = max(int(max_workers), self.min_workers)
        self.window = int(window)
        self.latency_high = float(latency_high)
        self.latency_healthy = float(latency_healthy)
        self.fail_high = float(fail_high)
        self.fail_low = float(fail_low)
        self.cpu_high = float(cpu_high)
        self.adjust_interval = float(adjust_interval)
        self._clock = clock
        self._lock = threading.Lock()
        self._latencies: deque[float] = deque(maxlen=self.window)
        self._outcomes: deque[bool] = deque(maxlen=self.window)  # True=成功
        self._risk_pending = 0          # 未消化的风控信号计数
        self._workers = self.min_workers
        self._last_adjust = 0.0
        self._stop = threading.Event()

    # ---- 记账接口（爬虫各请求点调用）----
    def record_latency(self, seconds: float) -> None:
        """记录一次请求的响应延迟（含成功请求；失败请求走 record_failure）。"""
        with self._lock:
            self._latencies.append(max(float(seconds), 0.0))

    def record_success(self, latency: float | None = None) -> None:
        """成功记账：可选顺带记延迟。"""
        with self._lock:
            self._outcomes.append(True)
            if latency is not None:
                self._latencies.append(max(float(latency), 0.0))

    def record_failure(self) -> None:
        """失败记账（网络错误/解析失败等；重试成功后可再 record_success 对冲）。"""
        with self._lock:
            self._outcomes.append(False)

    def record_risk_signal(self, count: int = 1) -> None:
        """风控信号记账（验证码/限流空壳/安全拦截）：触发立即收缩。"""
        with self._lock:
            self._risk_pending += max(int(count), 0)

    # ---- 负载评估 ----
    def _cpu_load(self) -> float | None:
        """本机 CPU 负载（0-1）；psutil 缺失或采样失败返回 None。"""
        if psutil is None:
            return None
        try:
            return psutil.cpu_percent(interval=None) / 100.0
        except Exception:
            return None

    def desired_workers(self) -> int:
        """依据窗口指标计算期望并发度（纯计算，不改状态；单测友好）。"""
        with self._lock:
            latencies = list(self._latencies)
            outcomes = list(self._outcomes)
            risk = self._risk_pending
        cpu = self._cpu_load()

        # 1) 刚发生风控信号：无条件收缩到最小（消费掉信号）
        if risk > 0:
            with self._lock:
                self._risk_pending = 0
            return self.min_workers

        # 2) 本机过载：压缩有效上限（保护本地资源）
        effective_max = self.max_workers
        if cpu is not None and cpu > self.cpu_high:
            effective_max = max(self.min_workers, (self.max_workers + 1) // 2)

        # 3) 窗口样本不足：保守用当前值（夹在有效上限内）
        if len(outcomes) < 3:
            return min(max(self._workers, self.min_workers), effective_max)

        fail_rate = 1.0 - (sum(outcomes) / len(outcomes))
        avg_latency = (sum(latencies) / len(latencies)) if latencies else 0.0

        # 4) 站点变差（高失败率 / 高延迟）→ 收缩一格
        if fail_rate > self.fail_high or avg_latency > self.latency_high:
            return max(self.min_workers, self._workers - 1)
        # 5) 站点健康 → 扩张一格（不越过有效上限）
        if fail_rate < self.fail_low and avg_latency < self.latency_healthy:
            return min(effective_max, self._workers + 1)
        # 6) 中间态：维持现状
        return min(max(self._workers, self.min_workers), effective_max)

    def _maybe_adjust(self) -> None:
        """节流地重算期望并发度（批间调用）。"""
        now = self._clock()
        with self._lock:
            if now - self._last_adjust < self.adjust_interval:
                return
            self._last_adjust = now
        self._workers = self.desired_workers()

    # ---- 执行入口 ----
    def run_items(
        self,
        fn: Callable,
        items: Iterable,
        *,
        on_result: "Callable[[object, object | None, BaseException | None], None] | None" = None,
        stop_event: threading.Event | None = None,
    ) -> list:
        """并发消费 items：按当前期望并发度分批提交，批间重算并发度。

        :param fn: 处理单个 item 的函数
        :param on_result: 回调 (item, result, exception)——统计/记账钩子
        :param stop_event: 置位后批间中断（未开始的任务不再提交），
            已提交在跑的任务自然收尾；返回已完成部分的结果
        :return: [(item, result, exception), ...] 与输入同序
        """
        stop_event = stop_event or self._stop
        item_list = list(items)
        results: list = [None] * len(item_list)
        idx = 0
        while idx < len(item_list) and not stop_event.is_set():
            batch_size = min(self.desired_workers_fresh(), len(item_list) - idx)
            batch = item_list[idx: idx + batch_size]
            with ThreadPoolExecutor(max_workers=max(batch_size, 1)) as pool:
                futures = {
                    pool.submit(fn, item): (idx + j, item)
                    for j, item in enumerate(batch)
                }
                for fut, (pos, item) in futures.items():
                    exc = None
                    result = None
                    try:
                        result = fut.result()
                    except Exception as e:  # noqa: BLE001——逐任务隔离，不让单篇失败炸池
                        exc = e
                    results[pos] = (item, result, exc)
                    if on_result is not None:
                        on_result(item, result, exc)
            idx += batch_size
            self._maybe_adjust()
        return results

    def desired_workers_fresh(self) -> int:
        """立即重算并采纳期望并发度（批次开头的强刷新，绕过节流）。"""
        self._workers = self.desired_workers()
        return self._workers

    # ---- 面板导出 ----
    def snapshot(self) -> dict:
        """当前池状态（监控 API 直接序列化）。"""
        with self._lock:
            latencies = list(self._latencies)
            outcomes = list(self._outcomes)
            risk = self._risk_pending
        fail_rate = (
            round(1.0 - sum(outcomes) / len(outcomes), 3) if outcomes else 0.0
        )
        avg_latency = round(sum(latencies) / len(latencies), 3) if latencies else 0.0
        return {
            "workers": self._workers,
            "min_workers": self.min_workers,
            "max_workers": self.max_workers,
            "avg_latency": avg_latency,
            "fail_rate": fail_rate,
            "risk_pending": risk,
            "cpu_load": self._cpu_load(),
            "psutil_available": psutil is not None,
        }


# ========================== 模块级单例 ==========================
_pool: DynamicWorkerPool | None = None
_POOL_LOCK = threading.Lock()


def configure_pool(runtime_cfg: dict) -> DynamicWorkerPool:
    """按 runtime 配置段构建全局动态池（crawler.init() 调用）。"""
    global _pool
    with _POOL_LOCK:
        _pool = DynamicWorkerPool(
            min_workers=int(runtime_cfg.get("min_workers", 1)),
            max_workers=int(runtime_cfg.get("max_workers", 6)),
        )
        return _pool


def get_pool() -> DynamicWorkerPool | None:
    """全局池（未初始化返回 None）。"""
    with _POOL_LOCK:
        return _pool


def reset_pool() -> None:
    """重置全局池（仅测试用）。"""
    global _pool
    with _POOL_LOCK:
        _pool = None
