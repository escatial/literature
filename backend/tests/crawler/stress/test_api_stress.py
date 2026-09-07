#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""爬虫系统性能压测（五大维度之性能稳定性）。

策略：真实长稳压测不进 CI；此处以「规模阶梯 + 延迟预算 + 守恒断言」
固化性能验收线，任何退化（P-1/P-2 类内存泄漏、延迟劣化）直接红灯。

覆盖面：
- dashboard 满载读吞吐：registry 容量打满(200)时的首屏聚合延迟；
- registry 满容量导出延迟：200 任务全量 JSON 序列化；
- P-2 守恒：超容量 5 倍注册(1000)后 dict/deque 长度守恒、新旧任务进出正确；
- P-2 内存回收：被挤出任务的 TaskState 经 gc 后真正释放（弱引用断言）；
- P-1 关联生命周期回收：批量同步任务跑完后 _CANCEL_EVENTS 必须清零；
- 混合并发流量：16 线程 200 请求（dashboard/列表/详情/SSE/参数热调）无 5xx；
- 参数热调高频：500 次连续 PUT 全部成功且落账正确；
- 风暴冒烟：批量未知任务查询/停止 → 全 404 无 500、进程不崩。

路径约定与 test_admin_api 一致：独立 app 挂 router（无 /api 前缀）。
"""
import gc
import statistics
import time
import weakref
from concurrent.futures import ThreadPoolExecutor

import fastapi
import pytest
from fastapi.testclient import TestClient

from automation.cnki import monitor as m_monitor
from automation.cnki import proxy_pool as m_proxy_pool
from automation.cnki import resilience as m_resilience
from automation.cnki import scheduler as m_scheduler
from api import cnki as cnki_api
from api import crawler_admin


@pytest.fixture(autouse=True)
def _isolate_singletons(monkeypatch):
    """全局单例隔离（与安全测试同款）：四单例 + cnki 三字典 + 断路器表。"""
    monkeypatch.delenv("CNKI_PROXY_MODE", raising=False)
    monkeypatch.delenv("CNKI_PROXY_LIST", raising=False)
    monkeypatch.setattr(m_resilience, "_breakers", {})
    cnki_api._task_results.clear()
    cnki_api._task_queues.clear()
    cnki_api._CANCEL_EVENTS.clear()
    m_monitor.reset_registry()
    m_resilience.reset_alert_manager()
    m_scheduler.reset_pool()
    m_proxy_pool.reset_proxy_pool()
    yield
    cnki_api._task_results.clear()
    cnki_api._task_queues.clear()
    cnki_api._CANCEL_EVENTS.clear()
    m_monitor.reset_registry()
    m_resilience.reset_alert_manager()
    m_scheduler.reset_pool()
    m_proxy_pool.reset_proxy_pool()


@pytest.fixture()
def client():
    app = fastapi.FastAPI()
    app.include_router(crawler_admin.router)
    app.include_router(cnki_api.router)
    return TestClient(app)


def _fill_registry(n: int, prefix: str = "load") -> list[str]:
    """向全局 registry 注册 n 个任务，返回 id 列表（注册顺序）。"""
    reg = m_monitor.get_registry()
    ids = [f"{prefix}-{i:05d}" for i in range(n)]
    for tid in ids:
        reg.register(tid, query=f"topic {tid}", params={"delay_seconds": 1.0})
    return ids


def _fake_runner_once(events: list[dict], result: dict):
    """构造 run_cnki_full_auto 替身：把 events 依次推进 SSE 队列后返回 result。"""

    async def fake(*args, queue=None, stop_event=None, pool_task_id=None, **kw):
        for ev in events:
            if queue is not None:
                await queue.put(ev)
        if queue is not None:
            await queue.put({"stage": "done", "payload": {"total": 1}})
        return result

    return fake


def _start_sync_done_task(client: TestClient, tid: str) -> str:
    """以同步模式(X-Test-Sync=1)跑一个即时完成的爬取任务（打满全生命周期）。

    :returns: 服务端生成的真实 task_id（SSE 流地址用它，X-Task-Id 仅为
      文献池隔离 ID，不是 cnki 任务标识）。
    """
    monkey_done = _fake_runner_once(
        [{"stage": "page", "payload": {"page": 1}}],
        {"papers": [{"title": "t", "url": f"https://x/{tid}"}]},
    )
    current = cnki_api.run_cnki_full_auto
    cnki_api.run_cnki_full_auto = monkey_done  # 直接替换(from-import 命名空间)
    try:
        resp = client.post(
            "/cnki/start",
            json={
                "topic": "压测主题",
                "expert_query": "SU='压测'*'检索'",
                "expert_queries": [
                    "SU='压测'*'检索'",
                    "SU='文献'*'综述'",
                    "SU='压测'*'文献'",
                    "SU='综述'*'检索'",
                ],
                "target_count": 1,
                "max_pages": 1,
                "db_type": "cnki",
                "run_id": f"stress-{tid}",
            },
            headers={"X-Test-Sync": "1", "X-Task-Id": tid},
        )
        assert resp.status_code == 200, resp.text
        return resp.json()["task_id"]
    finally:
        cnki_api.run_cnki_full_auto = current


def _p95(values: list[float]) -> float:
    return sorted(values)[int(0.95 * len(values)) - 1]


# ========================== 读路径吞吐 ==========================
def test_dashboard_read_throughput_at_full_capacity(client):
    """registry 打满(200)时 dashboard 首屏聚合：60 次全 200，
    平均延迟 < 50ms、p95 < 100ms（TestClient 无网络栈的进程内预算）。"""
    _fill_registry(200)
    latencies: list[float] = []
    for _ in range(60):
        t0 = time.perf_counter()
        resp = client.get("/crawler/dashboard")
        latencies.append((time.perf_counter() - t0) * 1000)
        assert resp.status_code == 200
    assert resp.json()["tasks"]["total"] == 200
    assert statistics.mean(latencies) < 50, f"dashboard 平均延迟 {statistics.mean(latencies):.1f}ms"
    assert _p95(latencies) < 100, f"dashboard p95 延迟 {_p95(latencies):.1f}ms"


def test_registry_full_capacity_list_export(client):
    """满容量任务列表导出：200 条全量 JSON，单次 < 100ms 且条数完整。"""
    _fill_registry(200)
    t0 = time.perf_counter()
    resp = client.get("/crawler/tasks")
    elapsed = (time.perf_counter() - t0) * 1000
    assert resp.status_code == 200
    assert len(resp.json()) == 200
    assert elapsed < 100, f"任务列表导出 {elapsed:.1f}ms 超预算"


# ========================== P-2 守恒与内存回收 ==========================
def test_registry_conservation_beyond_capacity():
    """P-2 回归：超容量 5 倍注册(1000→容量200)后 dict/deque 长度守恒，
    最新任务在场、最老任务被挤出，绝无 dict 无界增长。"""
    reg = m_monitor.get_registry()
    all_ids = _fill_registry(1000)
    assert len(reg._order) == 200
    assert len(reg._tasks) == len(reg._order), "P-2 守恒破坏：_tasks 与 _order 失同步"
    survivors = set(reg._tasks)
    assert all_ids[-1] in survivors and all_ids[-50] in survivors, "最新任务丢失"
    assert not any(t in survivors for t in all_ids[:50]), "最老任务未被挤出"


def test_evicted_task_state_reclaimed_by_gc():
    """P-2 内存回收：被挤出的 TaskState 在 gc 后真实释放（弱引用失效）。"""
    reg = m_monitor.get_registry()
    # 不保留局部强引用——否则测试自身栈引用会让 gc 断言失真
    ref = weakref.ref(reg.register("victim-000", query="first"))
    for i in range(1, 300):  # 挤满并越过容量，victim 必然被挤出
        reg.register(f"victim-{i:03d}", query="filler")
    gc.collect()
    assert ref() is None, "被挤出的 TaskState 未被回收（疑似引用滞留）"


# ========================== P-1 关联：生命周期回收 ==========================
def test_sync_task_storm_lifecycle_state_clean(client):
    """批量同步任务(30)风暴后：_CANCEL_EVENTS 必须全部清理（_runner finally），
    结果/队列在 TTL 内保留（兜底回收前可被 SSE 消费）。"""
    for i in range(30):
        _start_sync_done_task(client, f"storm-{i:03d}")
    assert len(cnki_api._task_results) == 30
    assert len(cnki_api._task_queues) == 30
    assert len(cnki_api._CANCEL_EVENTS) == 0, "P-1 关联回归：取消事件未清理"
    # TTL 兜底回收函数幂等可用
    tid = next(iter(cnki_api._task_results))
    cnki_api._reap_task(tid)
    assert tid not in cnki_api._task_results
    cnki_api._reap_task(tid)  # 重复调用不抛错


def test_done_task_sse_stream_replay(client):
    """已完成任务(30 个中的抽样)SSE 流可立即回放终止事件，不挂起。"""
    for i in range(5):
        real_tid = _start_sync_done_task(client, f"sse-{i}")
        resp = client.get(f"/cnki/stream/{real_tid}")
        assert resp.status_code == 200
        assert "done" in resp.text


# ========================== 混合并发流量 ==========================
def test_concurrent_mixed_traffic_no_5xx(client):
    """16 线程 200 混合请求（dashboard/列表/详情/SSE 回放/404/参数热调）：
    零 5xx、总耗时 < 15s、服务不崩。"""
    _fill_registry(100)
    mix_tid = _start_sync_done_task(client, "sse-mix")
    workloads: list[tuple[str, str]] = (
        [("GET", "/crawler/dashboard")] * 40
        + [("GET", "/crawler/tasks")] * 40
        + [("GET", f"/crawler/tasks/load-{i:05d}") for i in range(40)]
        + [("GET", f"/cnki/stream/{mix_tid}")] * 20
        + [("GET", "/crawler/tasks/ghost-404")] * 30
        + [("PUT", "/crawler/tasks/load-00000/params")] * 30
    )

    def _hit(req: tuple[str, str]):
        method, url = req
        if method == "PUT":
            return client.put(url, json={"max_workers": 4}).status_code
        return client.get(url).status_code

    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=16) as pool:
        codes = list(pool.map(_hit, workloads))
    elapsed = time.perf_counter() - t0
    assert len(codes) == 200
    bad = [c for c in codes if c >= 500]
    assert not bad, f"并发流量出现 5xx: {bad[:5]}"
    assert all(c in (200, 404) for c in codes), f"异常状态码: {sorted(set(codes))}"
    assert elapsed < 15, f"200 并发请求耗时 {elapsed:.1f}s"


def test_update_params_high_frequency(client):
    """参数热调 500 连发：全 200、无数量级性能退化、最终落账值正确。

    历史阈值 3s 为绝对墙钟断言，对开发机实时负载高度敏感
    （git stash 二分证实：撤掉全部源码改动后同样跑到 5.4s），
    故改为与并发用例一致的退化检测阈值：本用例意图是捕获
    「热调路径引入锁竞争/同步 IO」这类数量级退化（正常约
    6ms/次，退化会到数百 ms/次），而非校准机器性能。
    """
    m_monitor.get_registry().register("hot", params={"delay_seconds": 1.0})
    t0 = time.perf_counter()
    for i in range(500):
        resp = client.put(
            "/crawler/tasks/hot/params",
            json={"delay_seconds": 1.0 + (i % 5) * 0.5},
        )
        assert resp.status_code == 200
    elapsed = time.perf_counter() - t0
    assert elapsed < 15, (
        f"500 次参数热调耗时 {elapsed:.1f}s"
        f"（均值 {elapsed / 500 * 1000:.1f}ms/次），热调路径疑似性能退化"
    )
    assert client.get("/crawler/tasks/hot").json()["params"]["delay_seconds"] == 3.0


# ========================== 风暴冒烟（异常处理能力） ==========================
def test_not_found_storm_no_crash(client):
    """400 连发未知任务查询/停止：全 404 无 500，服务持续可用。"""
    for i in range(200):
        assert client.get(f"/crawler/tasks/ghost-{i}").status_code == 404
    for i in range(200):
        assert client.post(f"/crawler/tasks/ghost-{i}/stop").status_code == 404
    # 服务仍健康：正常读路径可响应
    assert client.get("/crawler/dashboard").status_code == 200
