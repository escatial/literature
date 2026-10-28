# -*- coding: utf-8 -*-
"""主题轴单收口点回归(2026-10-26「13 个重复英文主题」事故)。

事故链:混合文献池被误判英文池 → 轴规划跌入散文旁路
(_parse_prose_axes 自带准入、无判重无上限)直接产出 13 个英文轴
(含 3 对大小写/措辞变体的同义主题)→ 组级判重因未 lower 化全部漏放。

本文件锁定三层防线:
1. 散文轴候选必须经唯一收口点 _normalize_axes(bigram≥0.5 判重 +
   _AXIS_MAX 截断),旁路不得自带准入;
2. free fallback 自主分组受 _enforce_group_budget 预算闸约束,
   被截组成员经既有孤儿回收挂回保留组,文献一篇不丢;
3. lower 化后英文近似主题对(大小写变体)必须被两级判重拦住。
"""
import json

import writing.classifier as clf
from retrieval.types import Paper, Source


# 线上事故真实形态:13 个英文轴,前 6 个构成 3 对同义主题
# (大小写变体 / 措辞变体,bigram ≥ 0.5 必须互斥)
_FLOODED_EN_AXES = [
    {"name": "Digital and omnichannel marketing transformation",
     "research_question": "How does digitalization reshape marketing?"},
    {"name": "Digital and Omnichannel Marketing",
     "research_question": "How do omnichannel strategies work?"},
    {"name": "Consumer preferences and market segmentation",
     "research_question": "How do preferences segment markets?"},
    {"name": "Consumer Choice and Market Segmentation",
     "research_question": "How does choice shape segmentation?"},
    {"name": "Traceability, certification, and safety signaling",
     "research_question": "How does certification signal safety?"},
    {"name": "Traceability and Quality Signaling",
     "research_question": "How does traceability signal quality?"},
    {"name": "Sustainable supply chain governance",
     "research_question": ""},
    {"name": "Green supply chain management practices",
     "research_question": ""},
    {"name": "E-commerce platform adoption",
     "research_question": ""},
    {"name": "Online retail channel integration",
     "research_question": ""},
    {"name": "Consumer behavior in digital markets",
     "research_question": ""},
    {"name": "Pricing strategies and competition",
     "research_question": ""},
    {"name": "Brand equity and loyalty programs",
     "research_question": ""},
]


def _assert_mutually_exclusive(axes) -> None:
    """输出主题两两互斥:bigram < 0.5 且无包含等价关系。"""
    for i, a in enumerate(axes):
        for b in axes[i + 1:]:
            sim = clf._title_similarity(a.name, b.name)
            assert sim < 0.5, f"近似主题对泄漏:{a.name!r} <-> {b.name!r} sim={sim:.3f}"
            assert not clf._group_name_conflicts(a.name, {b.name}), (
                f"包含等价主题对泄漏:{a.name!r} <-> {b.name!r}"
            )


def _en_paper(i: int) -> Paper:
    return Paper(
        lit_id=f"lit_openalex_{i:016x}",
        source=Source.OPENALEX,
        title=f"Seafood market study on channel strategy number {i}",
        authors=[f"Author {i}"],
        journal="Aquaculture Economics",
        year=2024,
        abstract=f"This paper studies seafood marketing question {i} with empirical evidence.",
    )


def _zh_paper(i: int) -> Paper:
    return Paper(
        lit_id=f"lit_cnki_{i:016x}",
        source=Source.CNKI,
        title=f"水产品营销渠道与消费者行为研究{i}",
        authors=[f"作者{i}"],
        journal="测试学报",
        year=2024,
        abstract=f"本文研究水产品营销问题{i}的机制与对策。",
    )


def test_normalize_axes_caps_thirteen_axis_flood_at_eight():
    """13 轴洪流(含 3 对近似对)经收口点收敛到 ≤8 且无近似对。

    冒烟自验:近似对必须被丢弃,剩余轴两两互斥,数量不超过 _AXIS_MAX。
    """
    axes = clf._normalize_axes([dict(x) for x in _FLOODED_EN_AXES], 90, "en")
    assert axes, "en 池下合格英文轴不应被全部拒绝"
    assert len(axes) <= clf._AXIS_MAX, f"轴数超上限:{len(axes)}"
    _assert_mutually_exclusive(axes)
    # 三对近似对的第一条(信息量更全的措辞)应被保留
    kept = {a.name for a in axes}
    assert "Digital and omnichannel marketing transformation" in kept
    assert "Digital and Omnichannel Marketing" not in kept


def test_prose_axes_is_pure_parser_without_admission():
    """纯解析器只提取候选,不做准入:洪流散文原样提取全部 13 个候选。"""
    prose = "\n".join(
        f"{i + 1}. **{x['name']}** - related papers discuss the topic in depth."
        for i, x in enumerate(_FLOODED_EN_AXES)
    )
    items = clf._parse_prose_axes(prose)
    assert len(items) == len(_FLOODED_EN_AXES), (
        "解析器必须无准入地提取全部候选,判重交由收口点"
    )


def test_plan_theme_axes_routes_prose_through_single_gate(monkeypatch):
    """旁路堵住:散文直提结果必须经 _normalize_axes 收口。

    模拟 MiniMax 对大文档放弃 JSON、恒输出 13 轴散文:轴规划仍只发
    1 次调用(零成本直提),但产出必须 ≤8 且无近似对 —— 旧旁路在此
    场景会原样泄漏 13 个含同义对的英文轴。
    """
    papers = [_en_paper(i) for i in range(1, 91)]
    prose = "\n".join(
        f"{i + 1}. **{x['name']}** - related papers discuss the topic in depth."
        for i, x in enumerate(_FLOODED_EN_AXES)
    )
    calls: list[dict] = []

    def fake_create(**kwargs):
        calls.append(kwargs)
        return prose

    monkeypatch.setattr(clf, "messages_create", fake_create)
    monkeypatch.delenv("WRITING_POOL_LANGUAGE", raising=False)
    axes = clf._plan_theme_axes(papers, "seafood marketing", "en", abstract_limit=200)
    assert len(calls) == 1, "散文直提路径不应触发 LLM 抢救"
    assert len(axes) <= clf._AXIS_MAX
    _assert_mutually_exclusive(axes)


def test_enforce_group_budget_keeps_largest_groups_in_order():
    """组数超上限时保留规模最大的前 _AXIS_MAX 组并维持原组序。"""
    groups = [
        clf.Group(name=f"自主主题组{i}", lit_ids=[f"lit_{i}_{j}" for j in range(12 - i)])
        for i in range(10)
    ]
    kept = clf._enforce_group_budget(groups)
    assert len(kept) == clf._AXIS_MAX
    kept_names = {g.name for g in kept}
    assert "自主主题组0" in kept_names, "规模最大的组必须保留"
    assert "自主主题组8" not in kept_names and "自主主题组9" not in kept_names
    assert [g.name for g in kept] == [
        g.name for g in groups if g.name in kept_names
    ], "保留组必须维持原组序"


def test_enforce_group_budget_noop_under_limit():
    groups = [clf.Group(name="数字营销渠道", lit_ids=["lit_a", "lit_b"])]
    kept = clf._enforce_group_budget(groups)
    assert len(kept) == 1 and kept[0].name == "数字营销渠道"


def test_assign_free_fallback_caps_groups_and_recycles_orphans(monkeypatch):
    """free fallback 自主分组 10 组 → 预算闸截到 8,被截组成员经
    既有孤儿回收挂回保留组,全池文献一篇不丢。"""
    papers = [_zh_paper(i) for i in range(1, 91)]
    axes = [
        clf.Group(name="主题轴甲", lit_ids=[]),
        clf.Group(name="主题轴乙", lit_ids=[]),
    ]
    state = {"n": 0}
    free_names = [
        "数字营销渠道", "消费者行为研究", "品牌价值管理", "供应链协同治理",
        "质量安全认证", "电商平台运营", "价格竞争策略", "跨境贸易壁垒",
        "绿色可持续发展", "区域产业集聚",
    ]

    def fake_create(**kwargs):
        state["n"] += 1
        if state["n"] <= 3:
            raise RuntimeError("模拟按轴归组三次失败")
        groups = [
            {"name": name, "ids": list(range(k * 9 + 1, k * 9 + 10))}
            for k, name in enumerate(free_names)
        ]
        return json.dumps({"groups": groups})

    monkeypatch.setattr(clf, "messages_create", fake_create)
    monkeypatch.delenv("WRITING_POOL_LANGUAGE", raising=False)
    out = clf._assign_papers_to_axes(papers, axes, "水产营销", abstract_limit=200)
    assert len(out) <= clf._AXIS_MAX, f"free fallback 组数超上限:{len(out)}"
    covered = sorted(lid for g in out for lid in g.lit_ids)
    assert covered == sorted(p.lit_id for p in papers), "被截组成员必须经孤儿回收挂回,一篇不丢"
