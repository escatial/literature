# -*- coding: utf-8 -*-
"""主题最低文献数规则测试(J1 用户规则 2026-11-11:每主题 ≥15 篇)。"""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
for p in (str(BACKEND_ROOT / "src"), str(BACKEND_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from writing.classifier import Group, _merge_underfilled_groups  # noqa: E402


def _g(name: str, n: int, rq: str = "") -> Group:
    return Group(
        name=name,
        lit_ids=[f"lit_{name}_{i}" for i in range(n)],
        research_question=rq,
    )


def test_small_group_merged_into_most_similar():
    """15/15/6:最小的 6 篇组并入名称最相似的组。"""
    groups = [
        _g("品牌建设与传播", 15),
        _g("品牌价值传播策略", 15, rq="品牌如何传播"),
        _g("数字渠道与电商", 6),
    ]
    merged = _merge_underfilled_groups(
        [type("P", (), {"lit_id": ""})] * 36, groups, min_size=15,
    )
    assert len(merged) == 2
    assert all(len(g.lit_ids) >= 15 for g in merged)
    # 6 篇的「数字渠道与电商」被并入首个候选品牌组(小样组与两个候选的
    # bigram 相似度同为 0,按确定性顺序吸收),两个 15 篇品牌组其一变 21
    by_name = {g.name: len(g.lit_ids) for g in merged}
    assert "数字渠道与电商" not in by_name
    assert sorted(by_name.values()) == [15, 21]


def test_merge_respects_min_groups_floor():
    """池子只有 24 篇、2 组(12+12):无法兼得规则与组数下限 → 保持现状。"""
    groups = [_g("甲主题", 12), _g("乙主题", 12)]
    merged = _merge_underfilled_groups(
        [type("P", (), {"lit_id": ""})] * 24, groups, min_size=15,
    )
    assert len(merged) == 2  # 不硬合到 1 组(误杀小池多主题划分)


def test_all_small_pools_left_alone():
    """3 组全不足额:合并到组数下限(2)即停。"""
    groups = [_g("甲", 5), _g("乙", 4), _g("丙", 6)]
    merged = _merge_underfilled_groups(
        [type("P", (), {"lit_id": ""})] * 15, groups, min_size=15,
    )
    assert len(merged) == 2
    total = sum(len(g.lit_ids) for g in merged)
    assert total == 15


def test_merge_dedupes_lit_ids():
    """重复合并不产生重复 lit_id。"""
    g1 = Group(name="甲", lit_ids=["a", "b", "c"], research_question="")
    g2 = Group(name="乙", lit_ids=["a", "d"], research_question="")
    merged = _merge_underfilled_groups(
        [type("P", (), {"lit_id": ""})] * 4, [g1, g2], min_size=15,
    )
    assert len(merged) == 1
    assert sorted(merged[0].lit_ids) == ["a", "b", "c", "d"]
