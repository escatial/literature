# -*- coding: utf-8 -*-
"""Verify Step 4: provider fallback chain actually flips on retryable errors.

We patch _create_with_provider to raise APIConnectionError when provider ==
'minimax' and to return content otherwise. The harness then confirms the
second provider in the chain (deepseek) is invoked successfully, and that
the third provider (gpt) is invoked when both of the first two raise.
"""
import pytest
from openai import APIConnectionError

import llm.client as client


def test_fallback_chain_actually_fires(monkeypatch):
    # Force re-read of env so DEFAULT_FALLBACK_ORDER is honored.
    monkeypatch.setattr(client, "_clients", {})
    # Avoid select_providers()'s env-key check; supply an explicit chain.
    monkeypatch.setattr(client, "get_fallback_order", lambda: ("minimax", "deepseek", "gpt"))

    invocations: list[str] = []

    def fake_create_with_provider(*, resolved, **kwargs):
        invocations.append(resolved.id)
        if resolved.id == "minimax":
            raise APIConnectionError(request=None)
        return f"<{resolved.id}-content>"

    monkeypatch.setattr(client, "_create_with_provider", fake_create_with_provider)
    def fake_resolve(pid):
        return client.ResolvedProvider(
            id=pid, label=pid, api_key="sk-test", base_url="http://stub",
            model="m", mode="chat",
        )
    monkeypatch.setattr(client, "resolve_provider", fake_resolve)
    monkeypatch.setattr(client, "select_providers", lambda req: ("minimax", "deepseek", "gpt"))

    out = client.messages_create(system="x", user="y", max_tokens=10)
    assert out == "<deepseek-content>"
    assert invocations[0] == "minimax"
    assert invocations[-1] == "deepseek"


def test_fallback_chain_walks_through_all_providers(monkeypatch):
    monkeypatch.setattr(client, "_clients", {})
    monkeypatch.setattr(client, "get_fallback_order", lambda: ("minimax", "deepseek", "gpt"))

    invocations: list[str] = []

    def fake_create_with_provider(*, resolved, **kwargs):
        invocations.append(resolved.id)
        if resolved.id in ("minimax", "deepseek"):
            raise APIConnectionError(request=None)
        return f"<{resolved.id}-content>"

    monkeypatch.setattr(client, "_create_with_provider", fake_create_with_provider)
    monkeypatch.setattr(
        client, "resolve_provider",
        lambda pid: client.ResolvedProvider(
            id=pid, label=pid, api_key="sk-test", base_url="http://stub",
            model="m", mode="chat",
        ),
    )
    monkeypatch.setattr(client, "select_providers", lambda req: ("minimax", "deepseek", "gpt"))
    out = client.messages_create(system="x", user="y", max_tokens=10)
    assert out == "<gpt-content>"
    assert invocations == ["minimax", "deepseek", "gpt"]


def test_explicit_provider_does_not_fall_back(monkeypatch):
    """When caller pins a single provider, errors should propagate rather than
    silently flip to a different one."""
    monkeypatch.setattr(client, "_clients", {})

    def fake_create_with_provider(*, resolved, **kwargs):
        raise APIConnectionError(request=None)

    monkeypatch.setattr(client, "_create_with_provider", fake_create_with_provider)
    monkeypatch.setattr(
        client, "resolve_provider",
        lambda pid: client.ResolvedProvider(
            id=pid, label=pid, api_key="sk-test", base_url="http://stub",
            model="m", mode="chat",
        ),
    )
    # explicit minimax -> only minimax in candidates -> no flipping
    with pytest.raises((APIConnectionError, RuntimeError)):
        client.messages_create(
            system="x", user="y", max_tokens=10, provider="minimax",
        )
