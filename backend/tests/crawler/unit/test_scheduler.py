# -*- coding: utf-8 -*-
"""单元测试:动态负载并发池(期望并发度计算/批间调整/停机语义/保序输出)。"""
import threading

from automation.cnki import scheduler
from automation.cnki.scheduler import DynamicWorkerPool


class _FakePsutil:
    """假 psutil:固定返回指定 CPU 占用百分比。"""

    def __init__(self, percent):
        self.percent = percent

    def cpu_percent(self, interval=None):
        return self.percent


def _grow_healthy(pool, rounds=3):
    """注入健康窗口样本(全成功、低延迟),驱动池逐轮扩张。"""
    for _ in range(rounds):
        for _ in range(3):
            pool.record_success(0.1)
        pool.desired_workers_fresh()


# ========================== 期望并发度(纯计算) ==========================
def test_desired_workers_insufficient_samples_keeps_current():
    pool = DynamicWorkerPool(min_workers=1, max_workers=6)
    assert pool.desired_workers() == 1                 # 无样本:保持当前(1)
    pool.record_success(0.1)
    pool.record_success(0.1)
    assert pool.desired_workers() == 1                 # 样本 <3:仍保守
    pool.record_success(0.1)
    assert pool.desired_workers() == 2                 # 健康窗口:扩张一格


def test_desired_workers_expand_stepwise_capped(monkeypatch):
    # 隔离本机 CPU 信号:psutil 存在且跑测试瞬间 CPU>85% 时有效上限被压到
    # (max+1)//2,扩张会被封在 2 —— 该用例只验证"逐轮+1、封顶 max"的逻辑
    monkeypatch.setattr(scheduler, "psutil", None)
    pool = DynamicWorkerPool(min_workers=1, max_workers=3)
    _grow_healthy(pool, rounds=5)
    assert pool.desired_workers() == 3                 # 逐轮 +1,封顶 max_workers


def test_desired_workers_risk_signal_shrinks_and_consumes():
    pool = DynamicWorkerPool(min_workers=1, max_workers=6)
    _grow_healthy(pool, rounds=1)                      # 一轮健康:workers=2
    assert pool._workers == 2
    pool.record_risk_signal(2)                         # 验证码/限流空壳
    assert pool.desired_workers() == 1                 # 无条件收缩到 min
    assert pool.snapshot()["risk_pending"] == 0        # 信号已被消费
    assert pool.desired_workers() == 3                 # 窗口仍健康:扩张一格(2+1)


def test_desired_workers_high_fail_rate_shrinks():
    pool = DynamicWorkerPool(min_workers=1, max_workers=6)
    _grow_healthy(pool, rounds=1)                      # workers=2, 窗口3成功
    for _ in range(8):
        pool.record_failure()                          # 失败率 8/11 ≈ 73% > 30%
    assert pool.desired_workers() == 1                 # 收缩一格(2-1)


def test_desired_workers_high_latency_shrinks():
    pool = DynamicWorkerPool(min_workers=1, max_workers=6)
    _grow_healthy(pool, rounds=1)                      # workers=2, 窗口3×0.1s
    for _ in range(5):
        pool.record_latency(5.0)                       # 均延迟(0.3+25)/8 ≈ 3.16s > 3
    assert pool.desired_workers() == 1                 # 收缩一格(2-1)


def test_desired_workers_cpu_overload_caps_effective_max(monkeypatch):
    monkeypatch.setattr(scheduler, "psutil", _FakePsutil(90))   # CPU 90% > 85%
    pool = DynamicWorkerPool(min_workers=1, max_workers=6)
    for _ in range(10):
        _grow_healthy(pool, rounds=1)
        fresh = pool.desired_workers_fresh()
        assert fresh <= 3                              # 有效上限 = (6+1)//2 = 3
    assert pool._workers == 3


# ========================== 批间调整节流 ==========================
def test_maybe_adjust_throttled_by_interval():
    class Clock:
        now = 1000.0

        def __call__(self):
            return Clock.now

    clock = Clock()
    pool = DynamicWorkerPool(min_workers=1, max_workers=6, adjust_interval=10.0, clock=clock)
    for _ in range(3):
        pool.record_success(0.1)
    pool._maybe_adjust()                               # 首次:立即采纳
    assert pool._workers == 2
    for _ in range(6):
        pool.record_success(0.1)
    pool._maybe_adjust()                               # 间隔不足:跳过
    assert pool._workers == 2
    Clock.now += 11.0
    pool._maybe_adjust()                               # 间隔已过:采纳新值
    assert pool._workers == 3


# ========================== run_items 执行语义 ==========================
def test_run_items_preserves_order_and_results():
    pool = DynamicWorkerPool(min_workers=1, max_workers=2)
    items = list(range(7))
    results = pool.run_items(lambda x: x * 10, items)
    assert results == [(i, i * 10, None) for i in items]   # 与输入同序


def test_run_items_exception_isolated_per_item():
    pool = DynamicWorkerPool(min_workers=2, max_workers=2)

    def _fn(x):
        if x == 3:
            raise ValueError(f"炸了: {x}")
        return x + 1

    seen = []
    results = pool.run_items(_fn, [1, 2, 3, 4], on_result=lambda i, r, e: seen.append(i))
    assert results[0] == (1, 2, None)
    assert results[2][0] == 3 and results[2][1] is None
    assert isinstance(results[2][2], ValueError)       # 单篇异常不炸池
    assert results[3] == (4, 5, None)
    assert seen == [1, 2, 3, 4]                        # on_result 全量回调


def test_run_items_stop_before_start_executes_nothing():
    pool = DynamicWorkerPool(min_workers=1, max_workers=4)
    stop = threading.Event()
    stop.set()
    called = []

    results = pool.run_items(lambda x: called.append(x), [1, 2, 3], stop_event=stop)
    assert called == []                                # 已置位:一个都不提交
    assert results == [None, None, None]


def test_run_items_stop_midway_keeps_finished_part():
    pool = DynamicWorkerPool(min_workers=1, max_workers=1)
    stop = threading.Event()

    def _fn(x):
        if x == 0:
            stop.set()                                 # 第一个任务完成后置停机
        return x * 10

    results = pool.run_items(_fn, [0, 1, 2, 3], stop_event=stop)
    assert results[0] == (0, 0, None)                  # 已执行:正常结果
    assert results[1:] == [None, None, None]           # 未执行:尾部 None


# ========================== 模块级单例与快照 ==========================
def test_configure_get_reset_pool():
    scheduler.reset_pool()
    assert scheduler.get_pool() is None
    pool = scheduler.configure_pool({"min_workers": 2, "max_workers": 5})
    assert scheduler.get_pool() is pool
    assert pool.min_workers == 2 and pool.max_workers == 5
    scheduler.reset_pool()
    assert scheduler.get_pool() is None


def test_snapshot_fields():
    pool = DynamicWorkerPool(min_workers=1, max_workers=6)
    pool.record_success(0.5)
    pool.record_failure()
    snap = pool.snapshot()
    assert set(snap.keys()) == {
        "workers", "min_workers", "max_workers", "avg_latency",
        "fail_rate", "risk_pending", "cpu_load", "psutil_available",
    }
    assert snap["fail_rate"] == 0.5
    assert snap["avg_latency"] == 0.5
