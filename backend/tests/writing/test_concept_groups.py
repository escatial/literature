# -*- coding: utf-8 -*-
"""相关性概念组截断抢救回归测试。

线上实测(2026-09-08):MiniMax 思考内容与正文共享 max_tokens 预算,
1500 上限下概念组 JSON 在 concepts 数组中途截断("Unterterminated string")
→ 两次解析全败 → 语义匹配降级为字面匹配 → 英文池高相关 0 篇。
"""
import json

from writing.relevance import _build_concept_groups_once, _salvage_truncated_concepts


def test_salvage_recovers_complete_concept_objects():
    raw = (
        '{"concepts": [{"cn": ["水产品", "渔业"], "en": ["aquatic products"]}, '
        '{"cn": ["营销", "营销策略"], "en": ["marketing strategy"]}, '
        '{"cn": ["电子商务"], "en": ["e-com'  # 第 3 组写到一半截断
    )
    data = _salvage_truncated_concepts(raw)
    assert isinstance(data, dict)
    assert len(data["concepts"]) == 2
    assert data["concepts"][0]["cn"] == ["水产品", "渔业"]


def test_salvage_returns_none_when_nothing_complete():
    assert _salvage_truncated_concepts('{"concepts": [{"cn": ["水产') is None
    assert _salvage_truncated_concepts("完全不是 JSON") is None
    assert _salvage_truncated_concepts("") is None


def test_build_concept_groups_uses_salvage_on_truncation(monkeypatch):
    truncated = (
        '前言文字 {"concepts": [{"cn": ["水产品"], "en": ["seafood"]}, '
        '{"cn": ["营销策略"], "en": ["marketing"]}, {"cn": ["截'
    )
    monkeypatch.setattr(
        "llm.client.messages_create", lambda *a, **k: truncated,
    )
    groups = _build_concept_groups_once("水产品营销策略")
    assert groups is not None
    assert groups[0] == {"cn": ["水产品"], "en": ["seafood"]}
    assert len(groups) == 2


def test_build_concept_groups_complete_json_untouched(monkeypatch):
    ok = '{"concepts": [{"cn": ["水产品"], "en": ["seafood"]}]}'
    monkeypatch.setattr(
        "llm.client.messages_create", lambda *a, **k: ok,
    )
    groups = _build_concept_groups_once("水产品营销策略")
    assert groups == [{"cn": ["水产品"], "en": ["seafood"]}]


def test_concept_budget_raised_for_reasoning_models(monkeypatch):
    """思考型模型推理吃预算,概念组预算必须足够大(≥4000)。"""
    seen: dict = {}
    ok = '{"concepts": [{"cn": ["a"], "en": ["b"]}]}'
    monkeypatch.setattr(
        "llm.client.messages_create",
        lambda *a, **k: (seen.update(k), ok)[1],
    )
    _build_concept_groups_once("主题")
    assert seen["max_tokens"] >= 4000
