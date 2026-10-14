# -*- coding: utf-8 -*-
"""Verify Step 2: the openai SDK's internal retry is closed (max_retries=0)."""
from openai import OpenAI

import llm.client as client


def test_get_client_constructs_openai_with_max_retries_zero(monkeypatch):
    """get_client() must pass max_retries=0; the SDK should not internally
    retry on transient errors (4xx/5xx/timeout) anymore."""
    captured: dict = {}

    class _FakeOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def close(self):  # mirror real API
            pass

    monkeypatch.setattr(client, "OpenAI", _FakeOpenAI)
    # Avoid hitting the real network for env resolution.
    monkeypatch.setattr(client, "_clients", {})
    monkeypatch.setattr(
        client, "resolve_provider",
        lambda pid=None: client.ResolvedProvider(
            id="minimax", label="MiniMax",
            api_key="sk-test", base_url="http://stub",
            model="m", mode="chat",
        ),
    )
    client._clients.clear()
    client.get_client(provider="minimax", timeout=10.0)
    assert captured.get("max_retries") == 0, (
        f"max_retries must be 0 to disable SDK-internal retries; got {captured!r}"
    )
    assert captured.get("timeout") == 10.0


def test_default_fallback_order_locks_minimax(monkeypatch):
    """默认锁定 minimax 单 provider:minimax 故障时不再静默换模型,
    真实错误直接抛给调用方由前端展示。"""
    assert client.DEFAULT_FALLBACK_ORDER == ("minimax",)


def test_get_fallback_order_honors_env_override(monkeypatch, tmp_path):
    """LLM_FALLBACK_ORDER=minimax 维持单 provider;显式写多值才启用降级;
    清空时回落到默认的 minimax 单 provider。"""
    import os
    # Force re-read by clearing the cached module-level helper output.
    monkeypatch.setattr(os, "environ", {**os.environ, "LLM_FALLBACK_ORDER": "minimax"})
    assert client.get_fallback_order() == ("minimax",)
    monkeypatch.setattr(os, "environ", {**os.environ, "LLM_FALLBACK_ORDER": "minimax,deepseek"})
    assert client.get_fallback_order() == ("minimax", "deepseek")
    monkeypatch.setattr(os, "environ", {**os.environ, "LLM_FALLBACK_ORDER": ""})
    assert client.get_fallback_order() == ("minimax",)


def test_messages_create_records_last_used_provider(monkeypatch):
    """Step 6: when messages_create() resolves to a provider, the
    last_used_provider context var must carry that id forward so the
    orchestrator's theme_attempt event reports the real provider."""
    from src.llm.client import last_used_provider, set_last_used_provider

    # Reset before the test.
    set_last_used_provider(None)
    assert last_used_provider.get() is None

    captured: list[str] = []

    def fake_create_with_provider(*, resolved, **kwargs):
        captured.append(resolved.id)
        last_used_provider.set(resolved.id)
        return '{"groups": []}'

    monkeypatch.setattr(client, "_create_with_provider", fake_create_with_provider)
    monkeypatch.setattr(
        client, "resolve_provider",
        lambda pid=None: client.ResolvedProvider(
            id=pid or "minimax", label=pid or "minimax",
            api_key="sk-test", base_url="http://stub",
            model="m", mode="chat",
        ),
    )
    out = client.messages_create(system="x", user="y", max_tokens=10, provider="gpt")
    assert out == '{"groups": []}'
    assert last_used_provider.get() == "gpt"
    assert captured == ["gpt"]
