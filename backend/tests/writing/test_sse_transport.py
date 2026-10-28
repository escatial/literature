# -*- coding: utf-8 -*-
"""SSE 传输层单测(D1,2026-10-27):直接单测 api/writing._sse_stream_response。

注入假 stream_fn(生成器),不经 HTTP、不起服务器:
- chunk 顺序转发;
- 生成器抛异常 → error 事件 + 正常收尾(不悬挂);
- stop_event 置位 → 后续 chunk 丢弃 + done 收尾 + 注册表键清理;
- 心跳帧格式(monkeypatch _SSE_HEARTBEAT_SECONDS 缩短,不真实等 2s);
- bind 在 worker 线程首行执行、finally reset(含异常路径)。

生产侧唯一改动:心跳间隔抽为模块常量 _SSE_HEARTBEAT_SECONDS 供注入
(任务书允许的最小改动);SSE 事件名与字节序不变。
"""
import asyncio
import json
import secrets
import threading
import time

import pytest

import api.writing as writing_mod
from api.writing import _sse_stream_response


@pytest.fixture(autouse=True)
def _clean_stop_registry():
    """隔离 _WRITING_STOP_EVENTS:防止用例间键泄漏。"""
    with writing_mod._WRITING_STOP_LOCK:
        writing_mod._WRITING_STOP_EVENTS.clear()
    yield
    with writing_mod._WRITING_STOP_LOCK:
        writing_mod._WRITING_STOP_EVENTS.clear()


def _sse_frame(event: str, data: dict) -> str:
    """构造与 orchestrator 同构的 SSE data 帧。"""
    return f"data: {json.dumps({'event': event, 'data': data}, ensure_ascii=False)}\n\n"


def _collect(stream_fn, **kwargs) -> list[str]:
    """同步驱动 StreamingResponse:跑完整个 event_gen,返回帧字符串列表。"""
    resp = _sse_stream_response(stream_fn, **kwargs)

    async def _drain() -> list[str]:
        frames: list[str] = []
        async for item in resp.body_iterator:
            frames.append(item.decode("utf-8") if isinstance(item, bytes) else item)
        return frames

    return asyncio.run(_drain())


def _parse_data_frames(frames: list[str]) -> list[dict]:
    """从帧列表提取 data 行并解析 JSON(注释行/心跳帧的 data 行同样适用)。"""
    out: list[dict] = []
    for frame in frames:
        for line in frame.split("\n"):
            if line.startswith("data: "):
                out.append(json.loads(line[len("data: "):]))
    return out


# ---------- chunk 顺序转发 ----------

def test_chunks_forwarded_in_order():
    """N 个 chunk 按产出顺序原样转发,流正常收尾(不悬挂)。"""
    def stream(stop_event, publish):
        for i in range(5):
            yield _sse_frame("section_token", {"delta": f"chunk-{i}"})

    frames = _collect(stream)
    payloads = _parse_data_frames(frames)
    assert [p["data"]["delta"] for p in payloads] == [f"chunk-{i}" for i in range(5)]
    assert all(p["event"] == "section_token" for p in payloads)


def test_bytes_frames_forwarded_verbatim():
    """chunk 为字节串时按原字节转发(SSE 帧不二次加工)。"""
    raw = _sse_frame("writing_started", {}).encode("utf-8")

    def stream(stop_event, publish):
        yield raw

    frames = _collect(stream)
    assert frames == [raw.decode("utf-8")]


# ---------- 异常 → error 事件 ----------

def test_stream_exception_emits_error_event_and_finishes():
    """生成器中途抛异常:已产出 chunk 照常转发,随后发 error 事件并收尾。"""
    marker = f"boom-{secrets.token_hex(4)}"

    def stream(stop_event, publish):
        yield _sse_frame("writing_started", {})
        raise RuntimeError(marker)

    frames = _collect(stream)
    payloads = _parse_data_frames(frames)
    assert payloads[0]["event"] == "writing_started"
    assert payloads[-1]["event"] == "error"
    assert marker in payloads[-1]["data"]["message"]
    # error 之后立即收尾:不再有后续事件
    assert len(payloads) == 2


def test_bind_reset_called_even_when_stream_raises():
    """bind 的 reset 句柄在异常路径下也必须执行(finally 兜底)。"""
    calls = {"bind": 0, "reset": 0}

    def bind():
        calls["bind"] += 1

        def _reset():
            calls["reset"] += 1

        return _reset

    def stream(stop_event, publish):
        raise RuntimeError("mid-stream failure")

    _collect(stream, bind=bind)
    assert calls["bind"] == 1
    assert calls["reset"] == 1


# ---------- 停止语义:置位后丢弃 chunk ----------

def test_stop_event_discards_pending_chunks_and_finishes():
    """produce 循环检测到 stop_event 置位即断出:后续 chunk 不再转发,
    done 收尾,且注册表中自己的键被清理。"""
    key = (f"usr_{secrets.token_hex(4)}", f"task_{secrets.token_hex(4)}")

    def stream(stop_event, publish):
        yield _sse_frame("section_token", {"delta": "第一段"})
        stop_event.set()
        yield _sse_frame("section_token", {"delta": "停止后不应转发"})

    frames = _collect(stream, stop_key=key)
    assert any("第一段" in f for f in frames)
    assert all("停止后不应转发" not in f for f in frames)
    with writing_mod._WRITING_STOP_LOCK:
        assert key not in writing_mod._WRITING_STOP_EVENTS


def test_publish_drops_chunks_after_stop_set():
    """publish 侧丢弃:置位后经 publish 入队的 chunk 直接丢弃(双保险之一)。"""

    def stream(stop_event, publish):
        stop_event.set()
        publish("chunk", _sse_frame("section_token", {"delta": "置位后发布"}))

    frames = _collect(stream)
    assert all("置位后发布" not in f for f in frames)


# ---------- 心跳帧 ----------

def test_heartbeat_frame_format(monkeypatch):
    """空闲期必须发心跳帧:注释行 + 1024 padding + data 行(event=heartbeat,
    elapsed_seconds 为整数秒);心跳先于正文 chunk。monkeypatch 间隔至 0.05s,
    生成器睡 0.2s,期间至少 1-2 个心跳,不真实等 2s。"""
    monkeypatch.setattr(writing_mod, "_SSE_HEARTBEAT_SECONDS", 0.05)

    def stream(stop_event, publish):
        time.sleep(0.2)  # > 4x 心跳间隔,窗口内必然超时若干次
        yield _sse_frame("complete", {})

    frames = _collect(stream)
    hearts = [f for f in frames if f.startswith(": heartbeat")]
    assert hearts, "空闲窗口内必须出现心跳帧"
    hb = hearts[0]
    # 帧结构:注释行 + padding 行(促使开发代理立即 flush)+ 空行 + data 行
    assert hb.count("\n\n") >= 1
    assert " " * 1024 in hb
    data_line = next(
        line for line in hb.split("\n") if line.startswith("data: ")
    )
    payload = json.loads(data_line[len("data: "):])
    assert payload["event"] == "heartbeat"
    assert isinstance(payload["data"]["elapsed_seconds"], int)
    # 心跳先于正文
    assert frames.index(hb) < len(frames) - 1


# ---------- bind 契约 ----------

def test_bind_runs_in_worker_thread_and_resets_after():
    """bind 在 worker 线程首行执行一次,流结束后 reset 恰好一次(C1)。"""
    calls = {"bind": 0, "reset": 0, "thread": None}

    def bind():
        calls["bind"] += 1
        calls["thread"] = threading.current_thread().name

        def _reset():
            calls["reset"] += 1

        return _reset

    def stream(stop_event, publish):
        yield _sse_frame("start", {})

    _collect(stream, bind=bind)
    assert calls["bind"] == 1
    assert calls["reset"] == 1
    assert calls["thread"] == "writing-stream-worker"


# ---------- 注册表生命周期 ----------

def test_anonymous_stream_cleans_its_own_key():
    """stop_key 缺省时注册匿名键,流结束后注册表不留残余。"""

    def stream(stop_event, publish):
        yield _sse_frame("start", {})

    _collect(stream)
    with writing_mod._WRITING_STOP_LOCK:
        leftovers = [k for k in writing_mod._WRITING_STOP_EVENTS if k[0] == "__anon__"]
    assert leftovers == []
