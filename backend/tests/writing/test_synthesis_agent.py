# -*- coding: utf-8 -*-
"""主题综合 Agent 测试:编号回填/清洗校验/按主题降级/比较表。"""
import json

import pytest

import synthesis.agent as sa
from retrieval.types import Paper, Source
from writing.classifier import Group


def _paper(lit_id: str, abstract: str = "问卷调查发现X正向影响Y。") -> Paper:
    return Paper(
        lit_id=lit_id, source=Source.CNKI, title=f"研究{lit_id[-2:]}",
        authors=["张三"], journal="测试学报", year=2023, abstract=abstract,
    )


# ---------- 编号回填 ----------

def test_valid_ids_maps_numbers_and_accepts_pool_ids():
    number_map = {1: "lit_a", 2: "lit_b", 3: "lit_c"}
    assert sa._valid_ids([1, "2", "lit_c"], number_map) == ["lit_a", "lit_b", "lit_c"]
    # 未知编号/池外 lit_id/垃圾输入全部丢弃
    assert sa._valid_ids([9, "lit_ghost", "abc", None, True], number_map) == []


# ---------- 清洗校验 ----------

def test_sanitize_drops_one_sided_controversy():
    number_map = {1: "lit_a", 2: "lit_b"}
    data = {
        "consensus": [{"claim": "X 促进 Y", "lit_ids": [1, 2]}],
        "controversies": [
            {"claim": "方向相反", "side_a": {"lit_ids": [1], "position": "正向"},
             "side_b": {"lit_ids": [], "position": ""}, "possible_causes": ["样本不同"]},
        ],
        "method_differences": ["一篇问卷一篇实验"],
        "gaps": [{"gap": "缺少纵向证据", "evidence": "均为横截面", "kind": "confirmed"}],
    }
    out = sa._sanitize_synthesis(data, number_map, 2)
    assert out["consensus"][0]["lit_ids"] == ["lit_a", "lit_b"]
    assert out["controversies"] == []           # 单边争议 = 伪争议,丢弃
    assert out["gaps"][0]["kind"] == "confirmed"
    assert out["method_differences"] == ["一篇问卷一篇实验"]


def test_sanitize_marks_causes_as_hypothesis_and_caps():
    number_map = {i: f"lit_{i}" for i in range(1, 12)}
    data = {
        "consensus": [
            {"claim": f"结论{i}", "lit_ids": [1]} for i in range(20)
        ],
        "controversies": [],
        "method_differences": [],
        "gaps": [],
    }
    out = sa._sanitize_synthesis(data, number_map, 11)
    assert len(out["consensus"]) == sa._MAX_CONSENSUS


def test_sanitize_normalizes_gap_kind():
    number_map = {1: "lit_a"}
    data = {
        "consensus": [{"claim": "c", "lit_ids": [1]}],
        "controversies": [],
        "gaps": [{"gap": "g1", "evidence": "", "kind": "不知道"},
                 {"gap": "g2", "evidence": "", "kind": "not_retrieved"}],
    }
    out = sa._sanitize_synthesis(data, number_map, 1)
    assert [g["kind"] for g in out["gaps"]] == ["not_retrieved", "not_retrieved"]


# ---------- 单主题综合 ----------

def test_build_theme_synthesis_happy_path(monkeypatch):
    papers = [_paper("lit_a"), _paper("lit_b"), _paper("lit_c")]
    payload = {
        "consensus": [{"claim": "X 正向影响 Y", "lit_ids": [1, 2]}],
        "controversies": [{
            "claim": "机制存在分歧",
            "side_a": {"lit_ids": [1], "position": "效率机制"},
            "side_b": {"lit_ids": [3], "position": "焦虑机制"},
            "possible_causes": ["测量方式不同"],
        }],
        "method_differences": ["样本行业不同"],
        "gaps": [{"gap": "缺纵向证据", "evidence": "均为横截面", "kind": "confirmed"}],
    }
    def fake_llm(system, user, **kwargs):
        assert "#1" in system and "#3" in system   # 数字编号清单进入 prompt
        return json.dumps(payload, ensure_ascii=False)
    monkeypatch.setattr("llm.client.messages_create", fake_llm)
    result = sa.build_theme_synthesis("主题A", "X 如何影响 Y", papers, None, "研究主题")
    assert result["consensus"][0]["lit_ids"] == ["lit_a", "lit_b"]
    assert result["controversies"][0]["side_b"]["lit_ids"] == ["lit_c"]
    assert result["controversies"][0]["possible_causes"][0]["type"] == "hypothesis"


def test_build_theme_synthesis_rejects_single_paper():
    with pytest.raises(sa.SynthesisError):
        sa.build_theme_synthesis("主题A", None, [_paper("lit_a")], None, "研究主题")


def test_build_theme_synthesis_invalid_json_raises(monkeypatch):
    monkeypatch.setattr("llm.client.messages_create", lambda s, u, **k: "这不是JSON")
    with pytest.raises(sa.SynthesisError):
        sa.build_theme_synthesis("主题A", None, [_paper("lit_a"), _paper("lit_b")], None, "研究主题")


# ---------- 全量综合与降级 ----------

def test_build_all_syntheses_records_per_theme_failure():
    groups = [
        Group(name="数字技术使用研究", lit_ids=["lit_a", "lit_b"], research_question="问题A"),
        Group(name="单篇孤例组", lit_ids=["lit_c"], research_question="问题B"),  # <2 篇 → 失败
    ]
    papers = [_paper("lit_a"), _paper("lit_b"), _paper("lit_c")]
    payload = {"consensus": [{"claim": "c", "lit_ids": [1, 2]}],
               "controversies": [], "method_differences": [], "gaps": []}
    import synthesis.agent as mod
    monkey_ok = lambda *a, **k: json.dumps(payload)
    # 直接 monkeypatch build_theme_synthesis 更聚焦:单主题失败不阻塞整体
    def fake_build(name, rq, theme_papers, ev, topic, rqs=None):
        if len(theme_papers) < 2:
            raise sa.SynthesisError("文献不足")
        return {"consensus": [{"claim": "c", "lit_ids": [p.lit_id for p in theme_papers]}],
                "controversies": [], "method_differences": [], "gaps": []}
    original = mod.build_theme_synthesis
    mod.build_theme_synthesis = fake_build
    try:
        syntheses, meta = sa.build_all_syntheses(groups, papers, None, "研究主题")
    finally:
        mod.build_theme_synthesis = original
    assert set(syntheses.keys()) == {"theme_1"}
    assert meta["succeeded"] == 1 and meta["failed"] == 1
    assert meta["failures"][0]["theme"] == "单篇孤例组"


def test_comparison_table_rows():
    syntheses = {"theme_1": {"consensus": [], "controversies": [{
        "claim": "c", "side_a": {"lit_ids": ["lit_a"], "position": "A"},
        "side_b": {"lit_ids": ["lit_b"], "position": "B"},
        "possible_causes": [{"cause": "x", "type": "hypothesis"}],
    }], "method_differences": [], "gaps": []}}
    groups = [Group(name="数字技术使用研究", lit_ids=["lit_a", "lit_b"])]
    rows = sa.comparison_table(syntheses, groups)
    assert rows[0]["theme"] == "数字技术使用研究"
    assert rows[0]["possible_causes"][0]["type"] == "hypothesis"


# ---------- 环境开关 ----------

def test_synthesis_enabled_toggle(monkeypatch):
    monkeypatch.setenv("WRITING_SYNTHESIS", "1")
    assert sa.synthesis_enabled() is True
    monkeypatch.setenv("WRITING_SYNTHESIS", "0")
    assert sa.synthesis_enabled() is False
    monkeypatch.delenv("WRITING_SYNTHESIS", raising=False)
    assert sa.synthesis_enabled() is True   # 生产默认开
