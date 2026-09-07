# -*- coding: utf-8 -*-
"""单元测试:自适应限速器(倍增/回落/封顶/并发线程安全)。"""
import threading

import pytest

from automation.cnki import crawler


def test_throttle_init_baseline():
    crawler.throttle_init(2.0)
    assert crawler.effective_delay() == 2.0
    assert crawler.throttle_base() == 2.0


def test_throttle_init_floor_half_second():
    crawler.throttle_init(0.01)
    assert crawler.effective_delay() == 0.5  # 基准下限 0.5s


def test_throttle_hit_multiplies_and_resets_streak():
    crawler.throttle_init(2.0)
    for _ in range(5):
        crawler.throttle_ok()  # 攒连胜
    crawler.throttle_hit("测试风控")
    assert crawler.effective_delay() == pytest.approx(3.6)
    # 连胜清零:一次 ok 不回落
    crawler.throttle_ok()
    assert crawler.effective_delay() == pytest.approx(3.6)


def test_throttle_hit_heavy_multiplies_harder():
    """v9.4:分钟级限流空壳用 heavy 档 ×2.5,抬升幅度追得上限流窗口成型速度。"""
    crawler.throttle_init(2.0)
    crawler.throttle_hit("限流空壳", heavy=True)
    assert crawler.effective_delay() == pytest.approx(5.0)   # 2.0 × 2.5
    crawler.throttle_hit("限流空壳", heavy=True)
    assert crawler.effective_delay() == pytest.approx(12.5)  # 5.0 × 2.5


def test_throttle_hit_caps_at_30s():
    crawler.throttle_init(2.0)
    for _ in range(20):
        crawler.throttle_hit("连续风控")
    assert crawler.effective_delay() == 30.0  # 封顶


def test_throttle_ok_falls_back_after_8_streak():
    """实现语义(v9.4):连胜 <8 不回落;达 8 之后每次 ok 都 ×0.9(缓慢回落到基准)。"""
    crawler.throttle_init(2.0)
    crawler.throttle_hit("抬升")               # 3.6
    for _ in range(7):
        crawler.throttle_ok()                  # 前 7 次:攒连胜,不回落
    assert crawler.effective_delay() == pytest.approx(3.6)
    crawler.throttle_ok()                      # 第 8 次:首次回落
    assert crawler.effective_delay() == pytest.approx(3.6 * 0.9)   # 3.24
    crawler.throttle_ok()                      # 第 9 次:连胜已达标,继续回落
    assert crawler.effective_delay() == pytest.approx(3.24 * 0.9)  # 2.916


def test_throttle_ok_never_below_base():
    crawler.throttle_init(2.0)
    for _ in range(50):
        crawler.throttle_ok()
    assert crawler.effective_delay() == 2.0  # 回落到基准即止


def test_throttle_concurrent_no_deadlock_and_consistent():
    """8 线程 × 200 次混合 hit/ok:无死锁、终值有界、无竞态异常。"""
    crawler.throttle_init(2.0)
    barrier = threading.Barrier(8)

    def worker(n):
        barrier.wait()
        for i in range(200):
            if i % 3 == 0:
                crawler.throttle_hit(f"w{n}")
            else:
                crawler.throttle_ok()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)  # 死锁则超时失败
        assert not t.is_alive()

    delay = crawler.effective_delay()
    assert crawler.throttle_base() <= delay <= 30.0


def test_throttle_state_independent_between_inits():
    crawler.throttle_init(2.0)
    crawler.throttle_hit("x")
    crawler.throttle_init(1.0)  # 重新初始化应完全重置
    assert crawler.effective_delay() == 1.0
    # 重置后连胜为 0:单次 ok 不回落(初始即基准,无变化可断)
    crawler.throttle_ok()
    assert crawler.effective_delay() == 1.0
