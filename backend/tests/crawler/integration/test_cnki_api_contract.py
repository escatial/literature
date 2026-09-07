#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""cnki API 契约测试：/cnki/start + /cnki/stream(SSE) 全量校验。

覆盖（联合测试阶段1 接口测试 + P-1 修复回归）：
- A 组：入参校验矩阵（422 边界值 / 非法入参 / 缺 run_id）
- B 组：SSE 契约（fake 爬虫推流、error 终态保证不挂、停止联动、
  X-Task-Id 透传、queue 清理、aggregator 故障隔离）
- C 组：P-1 修复回归（_reap_task 清理 / TTL 定时回收 / 不再无界增长）

设计：
- 独立 FastAPI 实例挂载 cnki.router，不触发 main.lifespan（不碰 DB/冷导入）
- monkeypatch api.cnki.run_cnki_full_auto（from-import 后名字在 api.cnki 命名空间）
- X-Test-Sync=1 同步执行防 TestClient hang
- aggregator 与文献池读取默认 stub，任何用例都不触碰真实数据库
"""
import asyncio
import json
import threading
import time

import fastapi
import pytest
from fastapi.testclient import TestClient

from api import cnki as cnki_api


# ========================== 夹具 ==========================
@pytest.fixture(autouse=True)
def _isolate_task_state(monkeypatch):
    """任务状态字典隔离 + 外部副作用 stub（前后各清一次，用例零污染）。"""
    cnki_api._task_queues.clear()
    cnki_api._task_results.clear()
    cnki_api._CANCEL_EVENTS.clear()

    # 文献池读取不碰 DB；聚合写入只记录调用
    monkeypatch.setattr(cnki_api, "_cnki_papers_from_pool", lambda pool_task_id=None: [])
    agg_calls: list[dict] = []

    def _fake_agg(**kw):
        agg_calls.append(kw)

    monkeypatch.setattr(cnki_api, "aggregator_add_cnki", _fake_agg)

    yield

    cnki_api._task_queues.clear()
    cnki_api._task_results.clear()
    cnki_api._CANCEL_EVENTS.clear()


@pytest.fixture()
def client():
    app = fastapi.FastAPI()
    app.include_router(cnki_api.router)
    return TestClient(app)


# ========================== 工具 ==========================
def _payload(**over) -> dict:
    """合法启动请求体（字段过 pydantic 校验 + 检索式过启动同步预检:
    必须 SU= 开头且含 * 交叉,与生产 LLM 生成的式子同构）。"""
    p = {
        "topic": "大语言模型",
        "expert_query": "SU='大语言模型'*'教育应用'",
        "expert_queries": [
            "SU='大语言模型'*'教育'",
            "SU='LLM'*'生成式'",
            "SU='生成式AI'*'教学改革'",
            "SU='语言模型'*'应用'",
        ],
        "target_count": 300,
        "max_pages": 10,
        "db_type": "cnki",
        "run_id": "run-test-001",
    }
    p.update(over)
    return p


def _fake_runner(events=None, result=None, capture=None):
    """构造 run_cnki_full_auto 替身：向 SSE 队列推事件后返回结果。"""

    async def fake(*args, queue=None, stop_event=None, pool_task_id=None, **kw):
        if capture is not None:
            capture["pool_task_id"] = pool_task_id
        for evt in (events or []):
            queue.put_nowait(evt)
        return result if result is not None else {"status": "succeeded", "saved": len(events or [])}

    return fake


def _parse_sse(body: str) -> list[tuple[str, dict]]:
    """把 SSE 文本解析为 [(event, data_dict)]；data 必须是合法 JSON。"""
    out = []
    for block in body.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        ev = data = None
        for line in block.splitlines():
            if line.startswith("event: "):
                ev = line.removeprefix("event: ")
            elif line.startswith("data: "):
                data = json.loads(line.removeprefix("data: "))
        out.append((ev, data))
    return out


def _wait_result(task_id: str, timeout: float = 3.0) -> dict | None:
    """轮询等待后台任务结果落盘（异步模式用）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        res = cnki_api._task_results.get(task_id)
        if res is not None:
            return res
        time.sleep(0.02)
    return None


# ========================== A 组：入参校验矩阵 ==========================
def test_a1_start_missing_run_id_rejected(client):
    """缺 run_id → 422，提示必须携带（三库统一检索约束）。"""
    r = client.post("/cnki/start", json=_payload(run_id=None))
    assert r.status_code == 422
    assert "三库统一检索" in r.json()["detail"]


def test_a2_start_blank_topic_rejected(client):
    """topic 空串 → 422（min_length=1）。"""
    assert client.post("/cnki/start", json=_payload(topic="")).status_code == 422


def test_a3_start_too_few_expert_queries_rejected(client):
    """expert_queries 少于 4 条 → 422。"""
    assert client.post(
        "/cnki/start", json=_payload(expert_queries=["a", "b", "c"])
    ).status_code == 422


def test_a4_start_too_many_expert_queries_rejected(client):
    """expert_queries 多于 8 条 → 422。"""
    assert client.post(
        "/cnki/start", json=_payload(expert_queries=[f"q{i}" for i in range(9)])
    ).status_code == 422


@pytest.mark.parametrize("bad", [0, 501])
def test_a5_start_target_count_bounds_rejected(client, bad):
    """target_count 越界（0 / 501）→ 422（合法域 1-500）。"""
    assert client.post("/cnki/start", json=_payload(target_count=bad)).status_code == 422


@pytest.mark.parametrize("bad", [0, 51])
def test_a6_start_max_pages_bounds_rejected(client, bad):
    """max_pages 越界（0 / 51）→ 422（合法域 1-50）。"""
    assert client.post("/cnki/start", json=_payload(max_pages=bad)).status_code == 422


def test_a7_start_invalid_db_type_rejected(client):
    """db_type 非 cnki → 422（pattern ^cnki$，三库只放开知网）。"""
    assert client.post("/cnki/start", json=_payload(db_type="wanfang")).status_code == 422


def test_a8_start_valid_returns_running_contract(client, monkeypatch):
    """合法请求 → 200；契约字段 task_id(32hex)/status=running/db_type=cnki。"""
    monkeypatch.setattr(
        cnki_api, "run_cnki_full_auto",
        _fake_runner(events=[], result={"status": "stopped"}),
    )
    r = client.post("/cnki/start", json=_payload())
    assert r.status_code == 200
    body = r.json()
    assert len(body["task_id"]) == 32
    int(body["task_id"], 16)  # 必须是 hex
    assert body["status"] == "running"
    assert body["db_type"] == "cnki"
    # 注:fake 即时跑完,finally 已清理取消事件——反向证明生命周期清理正确


def test_a9_invalid_expert_query_precheck_rejected(client):
    """Q-1 修复①:非法检索式在 start 前同步 422 并携带原因,零僵尸任务。

    场景:其余 3 条合法、仅 1 条坏式(生产中 LLM 偶发单组式/字段前缀错),
    旧行为返回 running 但任务在 adapter 注册前秒败、面板永不可见;
    新契约:启动请求直接 422,queue/取消标志均不登记。
    """
    r = client.post("/cnki/start", json=_payload(
        expert_queries=[
            "SU='大语言模型'*'教育'",
            "SU='LLM'*'生成式'",
            "SU='生成式AI'*'教学'",
            "TI='语言模型'",  # 非 SU= 前缀 → 非法
        ],
    ))
    assert r.status_code == 422
    assert "检索式" in r.json()["detail"]
    # 零僵尸任务:queue 与取消标志均未创建
    assert not cnki_api._task_queues
    assert not cnki_api._CANCEL_EVENTS


def test_a10_single_group_query_precheck_rejected(client):
    """Q-1 修复①补充:单组式(无 * 交叉,v8.4 判非法)同样 422 拒启。"""
    r = client.post("/cnki/start", json=_payload(
        expert_queries=[
            "SU='只有一组'", "SU='大语言模型'", "SU='LLM'", "SU='教育'",
        ],
    ))
    assert r.status_code == 422
    assert "* " in r.json()["detail"] or "2 组概念交叉" in r.json()["detail"]
    assert not cnki_api._task_queues


# ========================== B 组：SSE 契约 ==========================
def test_b9_sync_run_and_sse_stream_contract(client, monkeypatch):
    """同步执行：响应 200 → SSE 按序收到全部事件 → done 终态收流不挂。"""
    events = [
        {"stage": "log", "msg": "开始检索"},
        {"stage": "log", "msg": "解析第 1 页"},
        {"stage": "done", "saved": 2},
    ]
    monkeypatch.setattr(
        cnki_api, "run_cnki_full_auto",
        _fake_runner(events=events, result={"status": "succeeded", "saved": 2}),
    )
    r = client.post("/cnki/start", json=_payload(), headers={"X-Test-Sync": "1"})
    assert r.status_code == 200
    tid = r.json()["task_id"]
    # 结果已落盘（前端断线重连可查终态）
    assert cnki_api._task_results[tid]["status"] == "succeeded"

    with client.stream("GET", f"/cnki/stream/{tid}") as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        parsed = _parse_sse(resp.read().decode("utf-8"))
    assert [p[1] for p in parsed if p[0] == "cnki_progress"] == events


def test_b11_sse_error_terminal_state_no_hang(client, monkeypatch):
    """爬虫失败路径：error 事件后流必须正常关闭（前端不永久转圈）。"""
    monkeypatch.setattr(
        cnki_api, "run_cnki_full_auto",
        _fake_runner(
            events=[{"stage": "log", "msg": "抓取中"}, {"stage": "error", "msg": "滑块验证失败(本次已入库 0 篇)"}],
            result={"status": "failed", "saved": 0, "skipped": 0, "reason": "滑块验证失败"},
        ),
    )
    r = client.post("/cnki/start", json=_payload(), headers={"X-Test-Sync": "1"})
    tid = r.json()["task_id"]
    with client.stream("GET", f"/cnki/stream/{tid}") as resp:
        parsed = _parse_sse(resp.read().decode("utf-8"))
    assert parsed[-1][0] == "cnki_progress"
    assert parsed[-1][1]["stage"] == "error"
    assert cnki_api._task_results[tid]["status"] == "failed"


def test_b12_x_task_id_passthrough_to_runner(client, monkeypatch):
    """X-Task-Id 头 → 透传为 pool_task_id（文献池隔离标签）。"""
    capture: dict = {}
    monkeypatch.setattr(
        cnki_api, "run_cnki_full_auto",
        _fake_runner(events=[{"stage": "done", "saved": 0}], result={"status": "stopped"}, capture=capture),
    )
    client.post(
        "/cnki/start", json=_payload(),
        headers={"X-Test-Sync": "1", "X-Task-Id": "pool-abc-123"},
    )
    assert capture["pool_task_id"] == "pool-abc-123"


def test_b13_x_task_id_blank_means_no_pool_tag(client, monkeypatch):
    """X-Task-Id 为空白串 → 归一化为 None（不产生脏标签）。"""
    capture: dict = {}
    monkeypatch.setattr(
        cnki_api, "run_cnki_full_auto",
        _fake_runner(events=[], result={"status": "stopped"}, capture=capture),
    )
    client.post(
        "/cnki/start", json=_payload(),
        headers={"X-Test-Sync": "1", "X-Task-Id": "   "},
    )
    assert capture["pool_task_id"] is None


def test_b14_stop_cnki_tasks_by_ids_and_all():
    """停止联动：按 id 停 → 仅目标置位；None → 停所有已知任务（含已停，幂等）。

    注:置位不移除 _CANCEL_EVENTS 条目(由 _runner finally 清理),
    因此 stop(None) 返回的清单包含此前已置位的 t1——语义正确。
    """
    e1, e2 = threading.Event(), threading.Event()
    cnki_api._CANCEL_EVENTS["t1"] = e1
    cnki_api._CANCEL_EVENTS["t2"] = e2
    assert cnki_api.stop_cnki_tasks(["t1", "not-exist"]) == ["t1"]
    assert e1.is_set() and not e2.is_set()
    stopped = cnki_api.stop_cnki_tasks(None)
    assert set(stopped) == {"t1", "t2"}
    assert e2.is_set()


def test_b15_async_task_stop_flow(monkeypatch):
    """真实停止联动：异步任务运行中停止 → 爬虫退出 → stopped 终态落盘。"""

    async def fake(*args, queue=None, stop_event=None, **kw):
        queue.put_nowait({"stage": "log", "msg": "运行中"})
        while not stop_event.is_set():
            await asyncio.sleep(0.01)
        queue.put_nowait({"stage": "error", "msg": "用户停止(本次已入库 0 篇)"})
        return {"status": "stopped", "saved": 0}

    monkeypatch.setattr(cnki_api, "run_cnki_full_auto", fake)
    # 常驻 portal loop:后台任务在请求之间持续存活,停止信号才能被消费
    app = fastapi.FastAPI()
    app.include_router(cnki_api.router)
    with TestClient(app) as client:
        r = client.post("/cnki/start", json=_payload())
        tid = r.json()["task_id"]
        assert cnki_api.stop_cnki_tasks([tid]) == [tid]
        res = _wait_result(tid)
        assert res is not None and res["status"] == "stopped"


def test_b16_stream_queue_cleaned_after_consumption(client, monkeypatch):
    """SSE 消费完毕 → _task_queues 移除该任务（finally 清理）。"""
    monkeypatch.setattr(
        cnki_api, "run_cnki_full_auto",
        _fake_runner(events=[{"stage": "done", "saved": 0}], result={"status": "succeeded"}),
    )
    tid = client.post("/cnki/start", json=_payload(), headers={"X-Test-Sync": "1"}).json()["task_id"]
    assert tid in cnki_api._task_queues
    with client.stream("GET", f"/cnki/stream/{tid}") as resp:
        resp.read()
    assert tid not in cnki_api._task_queues


def test_b17_aggregator_failure_isolated(client, monkeypatch):
    """聚合写入抛异常 → 只记日志，响应与结果落盘不受影响（异常隔离）。"""

    def _boom(**kw):
        raise RuntimeError("db write failed")

    monkeypatch.setattr(cnki_api, "aggregator_add_cnki", _boom)
    monkeypatch.setattr(
        cnki_api, "run_cnki_full_auto",
        _fake_runner(events=[{"stage": "done", "saved": 3}], result={"status": "succeeded", "saved": 3}),
    )
    r = client.post("/cnki/start", json=_payload(), headers={"X-Test-Sync": "1"})
    assert r.status_code == 200
    tid = r.json()["task_id"]
    assert cnki_api._task_results[tid]["status"] == "succeeded"


def test_b18_stream_unknown_task_404(client):
    """不存在的 task_id 查流 → 404（含路径穿越样例，不 500）。"""
    assert client.get("/cnki/stream/no-such-task").status_code == 404
    assert client.get("/cnki/stream/..%2F..%2Fetc").status_code in (404, 422)


def test_b19_async_start_returns_immediately_and_completes(client, monkeypatch):
    """异步模式：立即返回 running；后台任务真实执行 → 终态落盘 → SSE 可收尾。"""
    monkeypatch.setattr(
        cnki_api, "run_cnki_full_auto",
        _fake_runner(
            events=[{"stage": "log", "msg": "n1"}, {"stage": "done", "saved": 1}],
            result={"status": "succeeded", "saved": 1},
        ),
    )
    r = client.post("/cnki/start", json=_payload())
    tid = r.json()["task_id"]
    assert r.json()["status"] == "running"
    res = _wait_result(tid)
    assert res is not None and res["status"] == "succeeded"
    with client.stream("GET", f"/cnki/stream/{tid}") as resp:
        parsed = _parse_sse(resp.read().decode("utf-8"))
    assert parsed[-1][1]["stage"] == "done"


# ========================== C 组：P-1 修复回归 ==========================
def test_c1_reap_task_clears_state():
    """_reap_task：结果与队列一并清理，且对不存在的 id 幂等。"""
    cnki_api._task_results["t"] = {"status": "done"}
    cnki_api._task_queues["t"] = asyncio.Queue()
    cnki_api._reap_task("t")
    assert "t" not in cnki_api._task_results
    assert "t" not in cnki_api._task_queues
    cnki_api._reap_task("t")  # 幂等不抛


def test_c2_reap_after_ttl_delay(monkeypatch):
    """call_later 定时回收机制：TTL 到期后 _reap_task 被真实触发。"""
    monkeypatch.setattr(cnki_api, "_RESULT_TTL_SECONDS", 0.05)
    cnki_api._task_results["t"] = {"status": "done"}
    loop = asyncio.new_event_loop()
    try:
        loop.call_later(cnki_api._RESULT_TTL_SECONDS, cnki_api._reap_task, "t")
        loop.run_until_complete(asyncio.sleep(0.2))
        assert "t" not in cnki_api._task_results
    finally:
        loop.close()
        cnki_api._task_results.pop("t", None)


def test_c3_ttl_reap_integration(monkeypatch):
    """端到端：任务结束后 TTL 到期，_task_results/_task_queues 自动清空。"""
    monkeypatch.setattr(cnki_api, "_RESULT_TTL_SECONDS", 0.05)
    app = fastapi.FastAPI()
    app.include_router(cnki_api.router)
    monkeypatch.setattr(
        cnki_api, "run_cnki_full_auto",
        _fake_runner(events=[{"stage": "done", "saved": 1}], result={"status": "succeeded", "saved": 1}),
    )
    with TestClient(app) as client:  # 常驻 portal loop，call_later 定时器可触发
        tid = client.post("/cnki/start", json=_payload(), headers={"X-Test-Sync": "1"}).json()["task_id"]
        assert tid in cnki_api._task_results
        # 轻请求驱动 portal loop 转动，等待 TTL 定时器执行回收
        deadline = time.time() + 3
        while time.time() < deadline and tid in cnki_api._task_results:
            client.get("/cnki/stream/none")
            time.sleep(0.05)
        assert tid not in cnki_api._task_results
        assert tid not in cnki_api._task_queues
