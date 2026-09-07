# -*- coding: utf-8 -*-
"""压力测试:万条翻页 / 内存水位 / 解析性能 / 限速器高并发。

定位:验证爬虫在大数据量与高并发压力下的稳定性与资源边界。
所有阈值均为宽松上界(只捕获"数量级劣化"与泄漏,不卡精确性能数字),
零真实睡眠(sleep_jitter 已被 crawler_env 替换为记录器)。

换机器标定:阈值支持环境变量覆盖。慢机器/CI 先跑一次实测,
再按实测值×1.5 设置环境变量(如 STRESS_FETCH_10K_MAX_SECONDS=270),
避免本机基线误报;快机器可收紧提高灵敏度。
"""
import gc
import os
import threading
import time
import tracemalloc

from automation.cnki import crawler
from helpers import make_list_html, make_list_rows


def _env_float(name: str, default: float) -> float:
    """从环境变量读取阈值;未设置/非法值时回退默认。"""
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


# 宽松上界:CI/低配机器也能稳定通过;环境变量可按机器标定覆盖
PARSE_1000_ROWS_MAX_SECONDS = _env_float("STRESS_PARSE_1000_MAX_SECONDS", 5.0)
FETCH_10K_MAX_SECONDS = _env_float("STRESS_FETCH_10K_MAX_SECONDS", 180.0)
FETCH_10K_MAX_PEAK_MB = _env_float("STRESS_FETCH_10K_PEAK_MB", 80.0)
LEAK_MAX_GROWTH_MB = _env_float("STRESS_LEAK_MAX_GROWTH_MB", 10.0)


# ========================== 解析性能 ==========================
def test_parse_list_1000_rows_performance(crawler_env):
    """千行列表页单次解析:正确性 + 耗时有界。"""
    html = make_list_html(make_list_rows("S", 1000))
    t0 = time.perf_counter()
    items = crawler.parse_list(html)
    elapsed = time.perf_counter() - t0

    assert len(items) == 1000
    assert items[0]["title"] == "S研究0001" and items[-1]["title"] == "S研究1000"
    assert elapsed < PARSE_1000_ROWS_MAX_SECONDS, \
        f"千行解析耗时 {elapsed:.2f}s,超过宽松上界 {PARSE_1000_ROWS_MAX_SECONDS}s"


# ========================== 万条全链路翻页(内存/超时/数据完整性) ==========================
def test_fetch_all_list_10k_pages_stress(crawler_env):
    """500 页 × 20 = 1 万条连续翻页:
    - 数据完整(一条不少、无重复)→ 排查"数据丢失/翻页游标错位"
    - 耗时有界 → 排查"运行超时"
    - tracemalloc 峰值有界 → 排查"内存泄漏"
    """
    total, per_page = 10_000, 20

    def make_page(body, raw):
        page = int(body["pageNum"][0])
        start = (page - 1) * per_page + 1
        return make_list_html(
            make_list_rows(f"P{page}", per_page, start_no=start), total=total)

    crawler_env.kns.grid_callable = make_page

    tracemalloc.start()
    t0 = time.perf_counter()
    items = crawler.fetch_all_list("Q", max_count=total)
    elapsed = time.perf_counter() - t0
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    # 数据完整性:恰好 1 万条且标题全局唯一(页间不错位/不丢页)
    assert len(items) == total, f"应抓满 {total} 条,实得 {len(items)} 条"
    titles = [it["title"] for it in items]
    assert len(set(titles)) == total, "存在重复条目,翻页游标可能错位"
    assert items[0]["title"] == "P1研究0001"
    assert items[-1]["title"] == "P500研究10000"

    # 请求形态:恰 500 页,无多余重试请求
    assert len(crawler_env.kns.grid_requests()) == total // per_page

    # 性能与内存上界
    assert elapsed < FETCH_10K_MAX_SECONDS, \
        f"万条抓取耗时 {elapsed:.1f}s,超过宽松上界 {FETCH_10K_MAX_SECONDS}s"
    assert peak < FETCH_10K_MAX_PEAK_MB * 1024 * 1024, \
        f"内存峰值 {peak / 1024 / 1024:.1f}MB,超过 {FETCH_10K_MAX_PEAK_MB}MB"


# ========================== 重复解析无内存泄漏 ==========================
def test_parse_list_repeated_no_memory_leak(crawler_env):
    """同一千行页面重复解析 50 轮:常驻内存增量有界 → 无累积性泄漏。"""
    html = make_list_html(make_list_rows("L", 1000))

    # 预热:解析库懒加载/内部缓存稳定后再测基线
    for _ in range(5):
        crawler.parse_list(html)
    gc.collect()

    tracemalloc.start()
    base = tracemalloc.get_traced_memory()[0]
    for i in range(50):
        crawler.parse_list(html)
        if i % 10 == 0:
            gc.collect()
    gc.collect()
    growth = tracemalloc.get_traced_memory()[0] - base
    tracemalloc.stop()

    assert growth < LEAK_MAX_GROWTH_MB * 1024 * 1024, \
        f"50 轮千行解析常驻内存增长 {growth / 1024 / 1024:.1f}MB,疑似累积性泄漏"


# ========================== 限速器高负载并发 ==========================
def test_throttle_high_load_concurrency(crawler_env):
    """8 线程 × 1000 次混合 hit/ok:无死锁、无异常、终值始终在 [0.5, 30] 界内。"""
    crawler.throttle_init(1.0)
    threads_n, loops = 8, 1000
    barrier = threading.Barrier(threads_n)
    errors = []

    def worker(seed):
        try:
            barrier.wait(timeout=10)
            for i in range(loops):
                if i % 10 == 0:  # 10% 触发风控信号,90% 成功回落
                    crawler.throttle_hit(f"压测线程{seed}")
                else:
                    crawler.throttle_ok()
        except Exception as e:  # noqa: BLE001  (压测收集一切异常)
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(threads_n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    alive = [t for t in threads if t.is_alive()]
    assert not alive, "限速器并发死锁:线程 30s 未结束"
    assert not errors, f"并发中出现异常: {errors[:3]}"
    delay = crawler.effective_delay()
    assert 0.5 <= delay <= 30.0, f"限速终值 {delay} 越界,高负载下状态被破坏"
