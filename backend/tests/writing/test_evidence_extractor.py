# -*- coding: utf-8 -*-
"""证据抽取模块测试:解析/归一化/截断抢救/降级/横截面判定。"""
import pytest

import evidence.extractor as ex
from retrieval.types import Paper, Source


def _paper(lit_id: str, abstract: str = "本研究采用问卷调查了386名员工。") -> Paper:
    return Paper(
        lit_id=lit_id, source=Source.CNKI, title=f"研究{lit_id[-2:]}",
        authors=["张三"], journal="测试学报", year=2023, abstract=abstract,
    )


def _fake_llm(raw: str):
    def fake(system, user, **kwargs):
        return raw
    return fake


# ---------- 解析与归一化 ----------

def test_parse_items_normalizes_fields():
    raw = (
        '{"items": [{"lit_id": "lit_a", "research_question": "X对Y的影响", '
        '"theory": null, "method_design": "survey", "sample": "386名员工", '
        '"findings": ["X正向影响Y"], "limitations": ["横截面"], '
        '"key_variables": ["X", "Y"], "polarity": "POSITIVE"}]}'
    )
    items = ex._parse_items(raw)
    pair = ex._normalize_item(items[0], {"lit_a"})
    assert pair is not None
    lit_id, ev = pair
    assert lit_id == "lit_a"
    assert ev["polarity"] == "positive"          # 大写枚举归一化
    assert ev["theory"] is None                   # null 保持 null
    assert ev["findings"] == ["X正向影响Y"]


def test_parse_items_salvages_truncated_json():
    raw = ('{"items": [{"lit_id": "lit_a", "findings": ["完整对象"], "polarity": "negative"}, '
           '{"lit_id": "lit_b", "research_qu')
    items = ex._parse_items(raw)
    assert len(items) == 1
    assert items[0]["lit_id"] == "lit_a"


def test_normalize_drops_unknown_lit_id_and_bad_polarity():
    item = {"lit_id": "lit_ghost", "polarity": "positive"}
    assert ex._normalize_item(item, {"lit_a"}) is None
    item2 = {"lit_id": "lit_a", "polarity": "非常正向", "method_design": "问卷"}
    _, ev = ex._normalize_item(item2, {"lit_a"})
    assert ev["polarity"] == "unclear"
    assert ev["method_design"] == "unclear"


# ---------- 全池抽取与降级 ----------

def test_extract_pool_evidence_success(monkeypatch):
    raw = ('{"items": ['
           '{"lit_id": "lit_a", "method_design": "survey", "polarity": "positive", "findings": ["f"]}, '
           '{"lit_id": "lit_b", "method_design": "experiment", "polarity": "negative"}]}')
    monkeypatch.setattr("llm.client.messages_create", _fake_llm(raw))
    result = ex.extract_pool_evidence([_paper("lit_a"), _paper("lit_b")], "主题")
    assert result["lit_a"]["extracted"] is True
    assert result["lit_a"]["polarity"] == "positive"
    assert result["lit_b"]["polarity"] == "negative"
    assert all(v["extractor_version"] == ex.EXTRACTOR_VERSION for v in result.values())


def test_extract_pool_evidence_degrades_to_fallback_on_llm_failure(monkeypatch):
    def boom(system, user, **kwargs):
        raise RuntimeError("provider down")
    monkeypatch.setattr("llm.client.messages_create", boom)
    result = ex.extract_pool_evidence([_paper("lit_a"), _paper("lit_b")], "主题")
    assert result["lit_a"]["extracted"] is False
    assert result["lit_a"]["polarity"] == "unclear"
    assert result["lit_a"]["findings"] == []


def test_partial_coverage_fills_fallback(monkeypatch):
    # LLM 只答了 1/2 篇:达到过半线,缺失方降级占位而不是报错
    raw = '{"items": [{"lit_id": "lit_a", "polarity": "positive"}]}'
    monkeypatch.setattr("llm.client.messages_create", _fake_llm(raw))
    result = ex.extract_pool_evidence([_paper("lit_a"), _paper("lit_b")], "主题")
    assert result["lit_a"]["extracted"] is True
    assert result["lit_b"]["extracted"] is False


# ---------- 横截面判定 ----------

def test_cross_sectional_dominant():
    papers = [_paper(f"lit_{i:02d}") for i in range(5)]
    evidence = {
        p.lit_id: {"extracted": True, "method_design": "survey"}
        for p in papers
    }
    assert ex.is_cross_sectional_dominant(papers, evidence) is True
    assert ex.is_cross_sectional_dominant(papers, None) is False
    experiment = {k: {"extracted": True, "method_design": "experiment"} for k in evidence}
    assert ex.is_cross_sectional_dominant(papers, experiment) is False
