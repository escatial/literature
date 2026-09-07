#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""crawler_admin API 集成测试：端点行为 + 双单例回归守卫。

关键设计：
- _isolate_singletons：前后重置 4 个子系统全局单例（monitor/resilience/
  scheduler/proxy_pool），并清掉代理相关环境变量，用例间零污染；
- client：独立 FastAPI 实例挂载 crawler_admin.router，不起真实服务；
- 双单例守卫：断言 API 模块导入的 _monitor 与测试环境的
  automation.cnki.monitor 是同一模块对象——生产 `src.*` 与测试回退
  `automation.*` 必须汇聚到同一棵模块树，否则面板与爬虫实例各说各话。
"""
import fastapi
import pytest
from fastapi.testclient import TestClient

from automation.cnki import monitor as m_monitor
from automation.cnki import proxy_pool as m_proxy_pool
from automation.cnki import resilience as m_resilience
from automation.cnki import scheduler as m_scheduler
from api import crawler_admin


@pytest.fixture(autouse=True)
def _isolate_singletons(monkeypatch):
    """全局单例与环境变量隔离（前后各重置一次）。"""
    monkeypatch.delenv("CNKI_PROXY_MODE", raising=False)
    monkeypatch.delenv("CNKI_PROXY_LIST", raising=False)
    m_monitor.reset_registry()
    m_resilience.reset_alert_manager()
    m_scheduler.reset_pool()
    m_proxy_pool.reset_proxy_pool()
    yield
    m_monitor.reset_registry()
    m_resilience.reset_alert_manager()
    m_scheduler.reset_pool()
    m_proxy_pool.reset_proxy_pool()


@pytest.fixture()
def client():
    app = fastapi.FastAPI()
    app.include_router(crawler_admin.router)
    return TestClient(app)


# ========================== 双单例回归守卫 ==========================
def test_dual_singleton_guard():
    """API 模块引用的 monitor 必须与测试环境的 automation.cnki.monitor 同一模块。"""
    assert crawler_admin._monitor is m_monitor
    assert crawler_admin._resilience is m_resilience
    assert crawler_admin._scheduler is m_scheduler
    assert crawler_admin._proxy_pool is m_proxy_pool


def test_business_chain_same_tree_as_admin():
    """业务链路（retrieval.sources → adapter）必须与面板同树（2026-09 生产事故守卫）。

    事故回顾：main.py 同时注入 backend 根与 src，两棵模块树均可导入——
    业务链路以 src.automation.cnki_adapter 加载 adapter（其相对导入把
    monitor 解析到 src 树），任务注册到 src 树 registry；而面板读
    automation 树 → 爬虫在跑、面板全 0。
    守卫手段：retrieval.sources 引用的爬虫入口函数必须定义在 automation
    树的 adapter 上（from-import 后函数的 __module__ 即定义模块名），
    且 adapter 内部注册监控用的 _monitor 与面板导入的 monitor 同一模块。
    """
    import retrieval.sources.cnki as rsc
    from automation import cnki_adapter

    assert rsc.run_cnki_full_auto.__module__ == cnki_adapter.__name__
    assert cnki_adapter._monitor is m_monitor


# ========================== dashboard ==========================
def test_dashboard_empty(client):
    """空注册表：各分片给默认壳，前端首屏不判空。"""
    body = client.get("/crawler/dashboard").json()
    assert body["tasks"]["total"] == 0
    assert body["tasks"]["saved_total"] == 0
    assert body["tasks"]["recent"] == []
    assert body["pool"] == {"initialized": False}
    assert body["proxy"]["mode"] == "off"
    assert body["proxy"]["proxies"] == []
    assert body["alerts"] == []
    assert body["breakers"] == []


def test_dashboard_with_tasks(client):
    reg = m_monitor.get_registry()
    reg.register("t-running", "kw-a")
    reg.mark_running("t-running", stage="检索中")
    reg.register("t-done", "kw-b")
    reg.mark_running("t-done")
    reg.update_progress("t-done", saved=7)
    reg.mark_done("t-done")

    t = client.get("/crawler/dashboard").json()["tasks"]
    assert t["total"] == 2
    assert t["running"] == 1
    assert t["done"] == 1
    assert t["saved_total"] == 7
    assert len(t["recent"]) == 2


# ========================== 任务列表 / 详情 / 停止 ==========================
def test_list_tasks_order_and_status_filter(client):
    reg = m_monitor.get_registry()
    reg.register("t1")
    reg.register("t2")
    reg.mark_running("t2")                                  # t2 → running
    reg.mark_running("t1")
    reg.mark_failed("t1", error="boom")                     # t1 → failed

    lst = client.get("/crawler/tasks").json()
    assert [x["task_id"] for x in lst] == ["t2", "t1"]      # 新→旧
    running = client.get("/crawler/tasks", params={"status": "running"}).json()
    assert [x["task_id"] for x in running] == ["t2"]


def test_task_detail_with_and_without_logs(client):
    reg = m_monitor.get_registry()
    reg.register("t9", "kw")
    reg.mark_running("t9")
    reg.append_log("t9", "发起检索")

    body = client.get("/crawler/tasks/t9").json()
    assert body["task_id"] == "t9"
    assert any("发起检索" in line for line in body["logs"])

    no_logs = client.get("/crawler/tasks/t9", params={"logs": "false"}).json()
    assert "logs" not in no_logs


def test_task_detail_404(client):
    assert client.get("/crawler/tasks/ghost").status_code == 404


def test_stop_task_and_missing_404(client):
    reg = m_monitor.get_registry()
    reg.register("t1")
    reg.mark_running("t1")
    assert client.post("/crawler/tasks/t1/stop").json() == {"task_id": "t1", "stopped": True}
    # stop 仅置取消事件:状态翻转由业务协程感知后调 mark_stopped 完成
    assert reg.snapshot("t1")["status"] == "running"
    reg.mark_stopped("t1")                                  # 模拟业务侧收尾
    stopped = client.get("/crawler/tasks", params={"status": "stopped"}).json()
    assert [x["task_id"] for x in stopped] == ["t1"]
    assert client.post("/crawler/tasks/ghost/stop").status_code == 404


def test_stop_all_counts_active_only(client):
    """stop-all 只停 queued/running，done 任务不受影响。"""
    reg = m_monitor.get_registry()
    reg.register("r1")
    reg.mark_running("r1")
    reg.register("r2")                                       # 保持 queued
    reg.register("d1")
    reg.mark_running("d1")
    reg.mark_done("d1")
    assert client.post("/crawler/tasks/stop-all").json() == {"stopped": 2}


# ========================== 参数热调整 ==========================
def test_update_params_merges_and_keeps_existing(client):
    """热调整合并生效：未传键与白名单外既有参数都保留。"""
    reg = m_monitor.get_registry()
    reg.register("t1", "kw", params={"delay_seconds": 2.0, "custom_flag": True})
    body = client.put("/crawler/tasks/t1/params", json={"max_workers": 4}).json()
    assert body["params"]["max_workers"] == 4
    assert body["params"]["delay_seconds"] == 2.0
    assert body["params"]["custom_flag"] is True


def test_update_params_empty_patch_422(client):
    reg = m_monitor.get_registry()
    reg.register("t1")
    assert client.put("/crawler/tasks/t1/params", json={}).status_code == 422


def test_update_params_unknown_task_404(client):
    assert client.put("/crawler/tasks/ghost/params", json={"max_workers": 2}).status_code == 404


def test_update_params_invalid_value_rejected_by_pydantic(client):
    """请求体校验（ge=1）在进端点前拦截，非法值 422。"""
    reg = m_monitor.get_registry()
    reg.register("t1")
    assert client.put("/crawler/tasks/t1/params", json={"max_workers": 0}).status_code == 422


# ========================== 告警 / 断路器 ==========================
def test_alerts_endpoint_lists_buffer(client):
    m_resilience.get_alert_manager().alert("ut-告警", push=False)
    body = client.get("/crawler/alerts").json()
    assert len(body["alerts"]) == 1


def test_breakers_snapshot_and_reset(client):
    """连败到阈值 → open；运维 reset 接口 → closed。"""
    br = m_resilience.get_breaker("ut-api-br", failure_threshold=2, recovery_timeout=60.0)
    br.record_failure()          # 注意:record_failure 无参,异常上下文由调用方日志承载
    br.record_failure()

    me = next(
        b for b in client.get("/crawler/breakers").json()["breakers"]
        if b["name"] == "ut-api-br"
    )
    assert me["state"] == "open"
    assert me["consecutive_failures"] == 2

    assert client.post("/crawler/breakers/ut-api-br/reset").json() == {"name": "ut-api-br", "reset": True}
    me2 = next(
        b for b in client.get("/crawler/breakers").json()["breakers"]
        if b["name"] == "ut-api-br"
    )
    assert me2["state"] == "closed"


# ========================== 代理池 ==========================
def test_proxies_off_mode(client):
    body = client.get("/crawler/proxies").json()
    assert body["mode"] == "off"
    assert body["proxies"] == []


def test_proxies_snapshot_masks_credentials(monkeypatch):
    """mode=on（环境变量指定）：池内代理可见但凭据必须打码。"""
    monkeypatch.setenv("CNKI_PROXY_MODE", "on")
    # 替换健康巡检循环：测试不启动线程
    monkeypatch.setattr(
        m_proxy_pool.ProxyPool, "start_health_loop",
        lambda self, interval: None,
    )
    m_proxy_pool.init_proxy_pool({"mode": "on", "pool": ["http://user:pass@1.2.3.4:8080"]})

    app = fastapi.FastAPI()
    app.include_router(crawler_admin.router)
    body = TestClient(app).get("/crawler/proxies").json()
    assert body["mode"] == "on"
    assert len(body["proxies"]) == 1
    assert body["proxies"][0]["url"] == "http://***@1.2.3.4:8080"


# ========================== 动态并发池 ==========================
def test_pool_endpoint_uninitialized(client):
    assert client.get("/crawler/pool").json() == {"initialized": False}


def test_pool_endpoint_initialized(client):
    m_scheduler.configure_pool({"min_workers": 1, "max_workers": 3})
    body = client.get("/crawler/pool").json()
    assert body["initialized"] is True
    assert body["max_workers"] == 3
