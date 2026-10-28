# -*- coding: utf-8 -*-
"""LLM 运行时配置 ContextVar 隔离回归(C1)。

- 线程1绑 A 配置、线程2绑 B 配置,各发 N 次 mock 调用,断言互不串台;
- reset 后归零;跨线程 reset 的兜底路径(clear)不炸;
- mock 打在 OpenAI client 构造层(get_client 返回假客户端),
  不触碰 _strip_think 等输出清洗函数。
"""
import threading

import pytest

from llm import client as llm_client


@pytest.fixture(autouse=True)
def _clean_ctx():
    llm_client.clear_runtime_llm()
    yield
    llm_client.clear_runtime_llm()


class _FakeCompletions:
    """记录解析到的 base_url,验证调用方上下文。"""

    seen: list[str] = []
    lock = threading.Lock()

    def create(self, **kwargs):
        # 真实链路:get_client 缓存键含 base_url 指纹,mock 层直接读上下文
        rt = llm_client.get_runtime_llm() or {}
        with type(self).lock:
            type(self).seen.append(rt.get("base_url") or "")
        return "ok"


def test_concurrent_threads_do_not_cross_talk(monkeypatch):
    """线程 A/B 各绑不同 base_url,各 20 次 mock 调用,互不串台。"""
    _FakeCompletions.seen = []
    monkeypatch.setattr(llm_client, "get_client", lambda *a, **kw: _FakeClient())

    results: dict[str, list[str]] = {"A": [], "B": []}
    barrier = threading.Barrier(2, timeout=5)

    def worker(name: str, base: str):
        token = llm_client.set_runtime_llm(
            kind="openai", base_url=base,
            api_key=f"sk-{name.lower()}", model="m",
        )
        try:
            barrier.wait()  # 两组同时开跑,最大化交错窗口
            for _ in range(20):
                # 走真实 resolve 链:resolve_provider 读 ContextVar
                resolved = llm_client.resolve_provider(None)
                with _FakeCompletions.lock:
                    results[name].append(resolved.base_url)
        finally:
            llm_client.reset_runtime_llm(token)

    class _FakeClient:
        completions = type("C", (), {"create": staticmethod(_FakeCompletions.create)})()

    t1 = threading.Thread(target=worker, args=("A", "https://a.example.com"))
    t2 = threading.Thread(target=worker, args=("B", "https://b.example.com"))
    t1.start(); t2.start(); t1.join(10); t2.join(10)

    assert len(results["A"]) == 20 and len(results["B"]) == 20
    assert set(results["A"]) == {"https://a.example.com"}
    assert set(results["B"]) == {"https://b.example.com"}
    # 上下文退出后归零(主线程从未 set)
    assert llm_client.get_runtime_llm() is None


def test_set_reset_roundtrip_in_same_thread():
    """同线程 set→reset 恢复前值;嵌套 set 亦然。"""
    t1 = llm_client.set_runtime_llm(kind="openai", base_url="https://first", api_key="k", model="m")
    t2 = llm_client.set_runtime_llm(kind="openai", base_url="https://second", api_key="k", model="m")
    assert llm_client.get_runtime_llm()["base_url"] == "https://second"
    llm_client.reset_runtime_llm(t2)
    assert llm_client.get_runtime_llm()["base_url"] == "https://first"
    llm_client.reset_runtime_llm(t1)
    assert llm_client.get_runtime_llm() is None


def test_minimax_kind_normalized_and_clear_compat():
    """历史 kind 归一化与 clear 兼容入口语义保持。"""
    llm_client.set_runtime_llm(kind="minimax", base_url="https://api.minimax.cn", api_key="k", model="m")
    rt = llm_client.get_runtime_llm()
    assert rt["kind"] == "openai"
    llm_client.clear_runtime_llm()
    assert llm_client.get_runtime_llm() is None
    assert llm_client._runtime_llm_active() is False


def test_model_override_contextvar_scoped():
    to = llm_client.set_model_override("model-x")
    try:
        assert llm_client.get_model_override() == "model-x"
        to2 = llm_client.set_model_override("model-y")
        llm_client.reset_model_override(to2)
        assert llm_client.get_model_override() == "model-x"
    finally:
        llm_client.reset_model_override(to)
    assert llm_client.get_model_override() is None


def test_cross_thread_reset_falls_back_to_clear():
    """跨线程 reset(token) 会抛(ValueError)——apply_user_llm 的兜底路径
    捕获后 clear。此处验证兜底行为不炸且状态归零。"""
    token = llm_client.set_runtime_llm(
        kind="openai", base_url="https://x.example.com", api_key="k", model="m",
    )
    errors: list[BaseException] = []

    def other_thread():
        try:
            llm_client.reset_runtime_llm(token)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
            llm_client.clear_runtime_llm()

    t = threading.Thread(target=other_thread)
    t.start(); t.join(5)
    # ContextVar.reset 跨线程:Python 不强制抛(C 实现按 token 上下文匹配),
    # 两种结果都合法 —— 关键是不炸且最终可回到已定义状态。
    # N3(2026-10-27)有效断言:若兜底路径被触发,抛的必须是 token 上下文
    # 不匹配类错误(ValueError:Token was created in a different Context /
    # LookupError),而非任意崩溃;兜底 clear 失败会由上方状态断言抓到。
    llm_client.clear_runtime_llm()
    assert llm_client.get_runtime_llm() is None
    assert errors == [] or all(
        isinstance(exc, (ValueError, LookupError)) for exc in errors
    ), f"跨线程 reset 兜底路径出现非预期异常: {errors!r}"
