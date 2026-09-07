# -*- coding: utf-8 -*-
"""单元测试:任务监控中心(注册表/生命周期/计数器/环形日志/启停/参数热调整/质量记账)。"""
import pytest

from automation.cnki import monitor
from automation.cnki.monitor import ADJUSTABLE_PARAMS, TaskRegistry
from automation.cnki.quality import QualityReport

TASK_DICT_KEYS = {
    "task_id", "query", "status", "stage", "saved", "skipped", "failed",
    "started_at", "ended_at", "params", "error", "counters", "quality", "elapsed",
}

COUNTER_KEYS = {
    "requests_total", "requests_failed", "parse_errors", "saved_total",
    "captcha_hits", "breaker_trips", "proxy_switches", "retries",
}


@pytest.fixture
def reg():
    """每个用例独立的注册表实例(不走全局单例,天然隔离)。"""
    return TaskRegistry()


# ========================== 注册与生命周期 ==========================
def test_register_and_duplicate_resets(reg):
    state = reg.register("t1", query="深度学习", params={"delay_seconds": 1.0})
    assert state.status == "queued"
    assert state.params == {"delay_seconds": 1.0}
    assert state.counters.keys() == COUNTER_KEYS       # 全链路计数键固定
    reg.mark_running("t1", stage="检索中")
    reg.register("t1", query="深度学习")                # 同 id 重复注册 → 状态重置
    fresh = reg.get("t1")
    assert fresh.status == "queued"
    assert fresh.started_at == 0.0


def test_lifecycle_marks(reg):
    reg.register("t1")
    reg.mark_running("t1", stage="抓取详情")
    state = reg.get("t1")
    assert state.status == "running"
    assert state.started_at > 0
    assert state.stage == "抓取详情"
    reg.mark_stage("t1", "解析导出")
    assert reg.get("t1").stage == "解析导出"
    reg.mark_done("t1")
    assert reg.get("t1").ended_at > 0
    reg.register("t2")
    reg.mark_stopped("t2")
    assert reg.get("t2").status == "stopped"
    reg.register("t3")
    reg.mark_failed("t3", error="网络崩了")
    assert reg.get("t3").status == "failed"
    assert reg.get("t3").error == "网络崩了"


def test_to_dict_key_set_and_elapsed(reg):
    reg.register("t1")
    data = reg.get("t1").to_dict()
    assert set(data.keys()) == TASK_DICT_KEYS
    assert "logs" not in data                          # 默认不带日志
    assert data["elapsed"] == 0.0                      # 未启动无耗时
    assert set(data["quality"].keys()) == {"count", "avg_score", "min_score", "flags"}


# ========================== 进度与计数 ==========================
def test_update_progress_partial(reg):
    reg.register("t1")
    reg.update_progress("t1", saved=5)
    state = reg.get("t1")
    assert (state.saved, state.skipped, state.failed) == (5, 0, 0)
    reg.update_progress("t1", failed=2)
    assert reg.get("t1").saved == 5                    # 未传字段不被覆盖
    assert reg.get("t1").failed == 2


def test_incr_known_and_ignore_unknown(reg):
    reg.register("t1")
    reg.incr("t1", "requests_total", 3)
    reg.incr("t1", "requests_total")
    reg.incr("t1", "captcha_hits", 2)
    counters = reg.get("t1").counters
    assert counters["requests_total"] == 4
    assert counters["captcha_hits"] == 2
    reg.incr("t1", "拼错的计数器")                      # 未知键:忽略不报错
    assert reg.get("t1").counters.keys() == COUNTER_KEYS


# ========================== 日志 ==========================
def test_append_log_and_snapshot(reg):
    reg.register("t1")
    reg.append_log("t1", "发起请求 GET /grid")
    snap = reg.snapshot("t1", include_logs=True)
    assert len(snap["logs"]) == 1
    assert "发起请求" in snap["logs"][0]
    assert snap["logs"][0].startswith("[")             # 带时间戳前缀
    assert "logs" not in reg.snapshot("t1", include_logs=False)
    assert reg.snapshot("missing") is None


# ========================== 启停控制 ==========================
def test_stop_task_idempotent_and_missing(reg):
    reg.register("t1")
    assert reg.stop_task("t1") is True
    assert reg.get("t1").is_stopped() is True
    assert reg.stop_task("t1") is True                 # 幂等
    assert reg.stop_task("不存在") is False
    assert reg.cancel_event("不存在") is None


def test_stop_all_only_active_tasks(reg):
    for tid in ("a", "b", "c", "d"):
        reg.register(tid)
    reg.mark_running("a")
    reg.mark_done("c")
    reg.mark_stopped("d")
    assert reg.stop_all() == 2                         # 只停 queued/running
    assert reg.get("a").is_stopped() and reg.get("b").is_stopped()
    assert not reg.get("c").is_stopped() and not reg.get("d").is_stopped()


# ========================== 参数热调整 ==========================
def test_update_params_whitelist(reg):
    assert ADJUSTABLE_PARAMS == {"delay_seconds", "max_workers", "page_size", "max_per_keyword"}
    reg.register("t1", params={"delay_seconds": 1.0, "keyword": "深度学习"})
    with pytest.raises(ValueError):                    # 白名单外拒绝
        reg.update_params("t1", {"evil_key": 1})
    with pytest.raises(KeyError):                      # 任务不存在
        reg.update_params("ghost", {"delay_seconds": 2.0})
    effective = reg.update_params("t1", {"delay_seconds": 2.5, "page_size": 30})
    assert effective["delay_seconds"] == 2.5
    assert effective["page_size"] == 30
    assert effective["keyword"] == "深度学习"           # 原有非白名单参数保留


def test_update_params_hook_called_and_failure_swallowed(reg):
    calls = []

    def _hook(task_id, patch):
        calls.append((task_id, patch))
        if patch.get("delay_seconds") == 9.9:
            raise RuntimeError("钩子内部故障")           # 钩子抛错不影响参数落账

    reg.register_param_hook(_hook)
    reg.register("t1", params={"delay_seconds": 1.0})
    reg.update_params("t1", {"delay_seconds": 2.0})
    assert calls == [("t1", {"delay_seconds": 2.0})]
    assert reg.get("t1").params["delay_seconds"] == 2.0
    reg.update_params("t1", {"delay_seconds": 9.9})    # 钩子抛错仍落账
    assert reg.get("t1").params["delay_seconds"] == 9.9


# ========================== 质量记账 ==========================
def test_add_quality_report_aggregation(reg):
    reg.register("t1")
    reg.add_quality_report("t1", QualityReport(score=90, flags=["missing_doi"]))
    reg.add_quality_report("t1", QualityReport(score=80, flags=["missing_doi", "missing_journal"]))
    quality = reg.get("t1").quality
    assert quality["count"] == 2
    assert quality["avg_score"] == 85.0
    assert quality["min_score"] == 80
    assert quality["flags"] == {"missing_doi": 2, "missing_journal": 1}
    reg.add_quality_report("ghost", QualityReport())   # 未知任务:忽略


# ========================== 列表导出与单例 ==========================
def test_list_tasks_newest_first_and_filter(reg):
    reg.register("a")
    reg.register("b")
    reg.mark_running("b")
    ids = [t["task_id"] for t in reg.list_tasks()]
    assert ids == ["b", "a"]                           # 新→旧
    running = reg.list_tasks(status="running")
    assert [t["task_id"] for t in running] == ["b"]
    assert reg.list_tasks(status="done") == []


def test_global_registry_singleton():
    monitor.reset_registry()
    r1 = monitor.get_registry()
    assert monitor.get_registry() is r1
    monitor.reset_registry()
    assert monitor.get_registry() is not r1
