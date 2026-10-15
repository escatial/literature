# -*- coding: utf-8 -*-
"""引用支持性裁判(support judge)测试:环境开关/解析/判 0 告警。"""
import json

import pytest

import qa.support as sp
from retrieval.types import Paper, Source
from writing.orchestrator import SectionResult


def _paper(lit_id: str, abstract: str) -> Paper:
    return Paper(
        lit_id=lit_id, source=Source.CNKI, title=f"研究{lit_id[-2:]}",
        authors=["张三"], journal="测试学报", year=2023, abstract=abstract,
    )


def _section(content: str, citations: list[str], key: str = "theme_1") -> SectionResult:
    return SectionResult(key=key, title="章节", content=content, citations=citations)


def test_support_judge_toggle(monkeypatch):
    monkeypatch.delenv("QA_SUPPORT_JUDGE", raising=False)
    assert sp.support_judge_enabled() is False        # 默认关闭
    monkeypatch.setenv("QA_SUPPORT_JUDGE", "1")
    assert sp.support_judge_enabled() is True


def test_parse_judgements_robust():
    raw = json.dumps({"judgements": [
        {"index": 0, "verdict": 2},
        {"index": "1", "verdict": 0, "reason": "对象不符"},
        {"index": 2, "verdict": 5},          # 非法档位 → 丢弃
        {"index": "x", "verdict": 1},        # 非法序号 → 丢弃
        "garbage",
    ]})
    out = sp._parse_judgements(raw, expected=3)
    assert [j["index"] for j in out] == [0, 1]
    assert out[1]["verdict"] == 0


def test_parse_judgements_invalid_json_raises():
    with pytest.raises(ValueError):
        sp._parse_judgements("not json", 1)


def test_check_citation_support_flags_unsupported(monkeypatch):
    papers = [
        _paper("lit_a", "摘要:X 正向影响 Y。"),
        _paper("lit_b", "摘要:本篇只研究了 Z 的测量。"),
    ]
    section = _section(
        "张三(2023)发现X提升Y[1]。张三(2023)证明X导致Z上升[2]。",
        ["lit_a", "lit_b"],
    )
    def fake_llm(system, user, **kwargs):
        payload = json.loads(user)
        assert payload["papers"][0]["abstract"].startswith("摘要:")
        return json.dumps({"judgements": [
            {"index": 0, "verdict": 2, "reason": ""},
            {"index": 1, "verdict": 0, "reason": "lit_b 未研究 X 与 Z"},
        ]}, ensure_ascii=False)
    monkeypatch.setattr("llm.client.messages_create", fake_llm)
    result = sp.check_citation_support([section], papers)
    codes = [i.code for i in result.issues]
    assert "SUPPORT_UNVERIFIED" in codes
    assert result.metrics["unsupported_claims"] == 1
    assert result.metrics["judge_coverage"] == 1.0
    assert result.status.value == "warn"


def test_check_citation_support_all_supported(monkeypatch):
    papers = [_paper("lit_a", "摘要:X 正向影响 Y。")]
    section = _section("张三(2023)发现X提升Y[1]。", ["lit_a"])
    def fake_llm(system, user, **kwargs):
        return json.dumps({"judgements": [{"index": 0, "verdict": 2, "reason": ""}]})
    monkeypatch.setattr("llm.client.messages_create", fake_llm)
    result = sp.check_citation_support([section], papers)
    assert result.issues == []
    assert result.status.value == "pass"


def test_comment_section_not_judged(monkeypatch):
    called = []
    def fake_llm(system, user, **kwargs):
        called.append(user)
        return json.dumps({"judgements": []})
    monkeypatch.setattr("llm.client.messages_create", fake_llm)
    section = _section("综合而言,现有研究结论不一。", [], key="comment")
    result = sp.check_citation_support([section], [_paper("lit_a", "摘要")])
    assert called == []
    assert result.metrics["cited_claims"] == 0
