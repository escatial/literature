# -*- coding: utf-8 -*-
"""分级路由守护:tier=fast 走快模型覆盖,未配置/失败回落主档。"""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
for p in (str(BACKEND_ROOT / "src"), str(BACKEND_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

import llm.client as C
from llm.client import (
    clear_runtime_fast_llm,
    get_runtime_fast_llm,
    messages_create,
    set_runtime_fast_llm,
)

import pytest


class TestFastTierContext:
    def test_set_get_clear(self):
        token = set_runtime_fast_llm(
            kind="openai", base_url="https://open.bigmodel.cn/api/paas/v4",
            api_key="k", model="glm-4.5-flash",
        )
        rt = get_runtime_fast_llm()
        assert rt and rt["model"] == "glm-4.5-flash"
        C.reset_runtime_fast_llm(token)
        assert get_runtime_fast_llm() is None

    def test_minimax_kind_normalized(self):
        token = set_runtime_fast_llm(
            kind="minimax", base_url="https://api.minimax.cn/v1",
            api_key="k", model="m",
        )
        assert get_runtime_fast_llm()["kind"] == "openai"
        C.reset_runtime_fast_llm(token)
        clear_runtime_fast_llm()


class TestResolveFastProvider:
    def test_none_when_unconfigured(self):
        clear_runtime_fast_llm()
        assert C._resolve_fast_provider() is None

    def test_resolved_when_configured(self):
        token = set_runtime_fast_llm(
            kind="openai", base_url="https://open.bigmodel.cn/api/paas/v4",
            api_key="k", model="glm-4.5-flash",
        )
        try:
            r = C._resolve_fast_provider()
            assert r is not None
            assert r.model == "glm-4.5-flash"
            assert r.id.startswith("fast:")
        finally:
            C.reset_runtime_fast_llm(token)

    def test_ssrf_rejected(self):
        set_runtime_fast_llm(
            kind="openai", base_url="http://127.0.0.1:9000",
            api_key="k", model="m",
        )
        try:
            with pytest.raises(RuntimeError):
                C._resolve_fast_provider()
        finally:
            clear_runtime_fast_llm()


class TestTierRouting:
    def test_messages_create_tier_fast_hits_fast_provider(self, monkeypatch):
        """配置快档后 tier=fast 的调用必须落在快档,且不再走常规 fallback。"""
        token = set_runtime_fast_llm(
            kind="openai", base_url="https://fast.example.com",
            api_key="k", model="fast-model",
        )
        clear = token
        try:
            called = {}

            def fake_create_with_provider(*, resolved, system, user, **kw):
                called["id"] = resolved.id
                called["model"] = resolved.model
                return "正文"

            monkeypatch.setattr(C, "_create_with_provider", fake_create_with_provider)
            monkeypatch.setattr(
                C, "_create_fallback",
                lambda **kw: (_ for _ in ()).throw(AssertionError("不应走常规链路")),
            )
            out = messages_create("s", "u", tier="fast")
            assert out == "正文"
            assert called["id"].startswith("fast:")
            assert called["model"] == "fast-model"
        finally:
            C.reset_runtime_fast_llm(clear)

    def test_messages_create_falls_back_without_fast(self, monkeypatch):
        """未配置快档 → tier=fast 走常规链路(行为与未分级一致)。"""
        clear_runtime_fast_llm()
        monkeypatch.setattr(
            C, "select_providers", lambda provider=None: ("minimax",),
        )
        monkeypatch.setattr(
            C, "_create_fallback",
            lambda **kw: "常规链路正文",
        )
        assert messages_create("s", "u", tier="fast") == "常规链路正文"

    def test_messages_create_falls_back_on_fast_failure(self, monkeypatch):
        """快档调用失败 → 回落主档,不抛错。"""
        token = set_runtime_fast_llm(
            kind="openai", base_url="https://fast.example.com",
            api_key="k", model="fast-model",
        )
        try:
            def boom(*, resolved, **kw):
                raise RuntimeError("快档挂了")

            monkeypatch.setattr(C, "_create_with_provider", boom)
            monkeypatch.setattr(
                C, "select_providers", lambda provider=None: ("minimax",),
            )
            monkeypatch.setattr(C, "_create_fallback", lambda **kw: "主档正文")
            assert messages_create("s", "u", tier="fast") == "主档正文"
        finally:
            C.reset_runtime_fast_llm(token)
