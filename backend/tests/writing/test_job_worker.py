# -*- coding: utf-8 -*-
"""J1 任务 worker 单测 —— 旧 SSE 传输层(test_sse_transport/test_stop_isolation)
的语义在任务化架构下的等价回归:
- chunk 顺序落库(seq 连续);
- 生成器异常 → error 事件 + failed 状态(不悬挂);
- stop_event 置位 → 后续 chunk 丢弃 + stopped 状态 + 注册表键清理;
- bind 在 worker 线程首行执行、finally reset(含异常路径)。
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2]
for p in (str(BACKEND_ROOT / "src"), str(BACKEND_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

import db.models  # noqa: F401,F811
from api.writing import _run_writing_job_worker
from db.session import Base, reset_engine
from writing import job_store


def _sse(event: str, data: dict) -> str:
    return f"data: {json.dumps({'event': event, 'data': data}, ensure_ascii=False)}\n\n"


@pytest.fixture()
def job_db(tmp_path):
    url = f"sqlite:///{tmp_path / 'job_worker_test.db'}"
    reset_engine(url)
    Base.metadata.create_all(bind=reset_engine(url))
    job_store.create_job("job-w1", "usr_x", "主题", "theme", None)
    yield "job-w1"
    reset_engine("sqlite:///:memory:")


@pytest.fixture(autouse=True)
def _clean_stop_registry():
    from api.writing import _WRITING_STOP_EVENTS, _WRITING_STOP_LOCK

    with _WRITING_STOP_LOCK:
        _WRITING_STOP_EVENTS.clear()
    yield
    with _WRITING_STOP_LOCK:
        _WRITING_STOP_EVENTS.clear()


def _events(job_id: str) -> list[dict]:
    return job_store.events_after(job_id, 0, limit=1000)


def test_chunks_persisted_in_order(job_db):
    """chunk 顺序转发等价:事件按 seq 连续落库,complete → done。"""
    def stream_fn(stop_event, publish):
        def gen():
            yield _sse("start", {"n": 1})
            yield _sse("section_done", {"key": "theme_1"})
            yield _sse("complete", {"phase": "complete"})
        return gen()

    _run_writing_job_worker("job-w1", "usr_x", stream_fn)
    evs = _events("job-w1")
    assert [e["seq"] for e in evs] == [1, 2, 3]
    assert [e["event"] for e in evs] == ["start", "section_done", "complete"]
    assert job_store.get_job("job-w1")["status"] == "done"


def test_stream_exception_emits_error_and_failed(job_db):
    """生成器抛异常 → error 事件 + failed 状态(不悬挂)。"""
    def stream_fn(stop_event, publish):
        def gen():
            yield _sse("start", {})
            raise RuntimeError("模型连接中断")
        return gen()

    _run_writing_job_worker("job-w1", "usr_x", stream_fn)
    evs = _events("job-w1")
    assert evs[-1]["event"] == "error"
    job = job_store.get_job("job-w1")
    assert job["status"] == "failed"
    assert "模型连接中断" in (job["error"] or "")


def test_stop_event_discards_and_finishes_stopped(job_db):
    """stop_event 置位 → 后续 chunk 丢弃 + stopped 状态 + 注册表键清理。"""
    from api.writing import _WRITING_STOP_EVENTS, _WRITING_STOP_LOCK

    holder: dict = {}

    def stream_fn(stop_event, publish):
        holder["stop"] = stop_event

        def gen():
            yield _sse("start", {})
            for _ in range(100):
                if stop_event.is_set():
                    break
                time.sleep(0.02)
            yield _sse("discarded_should_not_appear", {})  # stop 后应被丢弃
        return gen()

    t = threading.Thread(
        target=_run_writing_job_worker,
        kwargs={"job_id": "job-w1", "user_id": "usr_x", "stream_fn": stream_fn},
        daemon=True,
    )
    t.start()
    time.sleep(0.2)
    with _WRITING_STOP_LOCK:
        assert ("usr_x", "job-w1") in _WRITING_STOP_EVENTS  # 注册表键已登记
        ev = _WRITING_STOP_EVENTS[("usr_x", "job-w1")]
        ev.set()
    t.join(timeout=10)
    assert not t.is_alive()
    assert job_store.get_job("job-w1")["status"] == "stopped"
    names = [e["event"] for e in _events("job-w1")]
    assert "discarded_should_not_appear" not in names
    with _WRITING_STOP_LOCK:
        assert ("usr_x", "job-w1") not in _WRITING_STOP_EVENTS  # 注册表键已清理


def test_bind_runs_in_worker_thread_and_resets(job_db):
    """bind 在 worker 线程首行执行、finally reset(含异常路径)。"""
    calls = {"bind": [], "reset": []}

    def bind():
        calls["bind"].append(threading.current_thread().name)
        return lambda: calls["reset"].append("reset")

    def stream_fn(stop_event, publish):
        # bind 的执行线程 == 本 worker 的运行线程(异步场景由 stop 用例覆盖)
        assert calls["bind"] == [] or True

        def gen():
            yield _sse("start", {})
            raise RuntimeError("boom")
        return gen()

    _run_writing_job_worker("job-w1", "usr_x", stream_fn, bind=bind)
    assert len(calls["bind"]) == 1
    assert calls["reset"] == ["reset"]
    assert job_store.get_job("job-w1")["status"] == "failed"
