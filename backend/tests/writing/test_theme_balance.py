# -*- coding: utf-8 -*-
"""主题规模均衡回归:90 篇里 75 篇挤进一个泛化主题的事故防线。

线上案例:主题划分确认页出现 2 个主题(15/75),最大主题既写不出章节,
也让其余主题失去对照意义。契约:
1. 真主题允许长到池子一半(证据密集的问题簇就该是大章节,过强拆会肢解
   完整主题);超过一半的巨型组由 _balance_oversize_groups 确定性拆分成
   均衡子组,一篇不丢、组名不重复 —— 上限与 harness 的过半失衡检查对齐;
2. inspect_theme_groups 的 theme_balance 检查必须把"最大主题过半"的
   方案判为不通过,驱动策略升级。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from agent.harness import inspect_theme_groups
from retrieval.types import Paper, Source
import writing.classifier as clf


def _paper(i: int, title: str) -> Paper:
    return Paper(
        lit_id=f"lit_cnki_{i:016x}",
        source=Source.CNKI,
        title=title,
        authors=[f"作者{i}"],
        journal="水产经济",
        year=2024,
        abstract=f"本文研究{title}相关问题的第{i}个维度,采用调研与案例分析。",
    )


def _pool_15_75() -> tuple[list[Paper], list[clf.Group]]:
    """复现线上事故:75 篇泛化营销文献 + 15 篇品牌文献。"""
    papers = [
        _paper(i, f"水产品市场营销策略分析{i%20}:渠道与消费行为研究")
        for i in range(1, 76)
    ]
    papers += [
        _paper(100 + i, f"水产品品牌建设案例研究{i%5}:区域公用品牌与企业品牌")
        for i in range(1, 16)
    ]
    groups = [
        clf.Group(name="水产品品牌建设", lit_ids=[p.lit_id for p in papers[75:]]),
        clf.Group(name="一般市场营销策略", lit_ids=[p.lit_id for p in papers[:75]]),
    ]
    return papers, groups


def test_balance_splits_oversize_group_without_losing_papers():
    papers, groups = _pool_15_75()
    balanced = clf._balance_oversize_groups(papers, groups, "水产品营销策略")

    assert len(balanced) > 2, "75 篇的巨型组应被拆分成多个子主题"
    covered = [lid for g in balanced for lid in g.lit_ids]
    assert sorted(covered) == sorted(p.lit_id for p in papers), "拆分不允许丢文献"
    names = [g.name for g in balanced]
    assert len(set(names)) == len(names), "拆分后组名不得重复"
    cap = max(12, -(-len(papers) * 50 // 100))
    assert all(len(g.lit_ids) <= cap for g in balanced), (
        f"拆分后仍有超限组: {[(g.name, len(g.lit_ids)) for g in balanced]}"
    )


def test_balance_keeps_majority_theme_up_to_half():
    """证据密集的真主题(45%)不再被强拆 —— 35% 时代会肢解完整主题。"""
    papers = [
        _paper(i, f"水产品市场营销策略分析{i % 20}:渠道与消费行为研究")
        for i in range(1, 46)
    ]
    papers += [
        _paper(100 + i, f"水产品流通渠道案例研究{i % 5}")
        for i in range(1, 31)
    ]
    papers += [
        _paper(200 + i, f"水产品消费行为调查研究{i % 5}")
        for i in range(1, 26)
    ]
    groups = [
        clf.Group(name="市场营销策略研究", lit_ids=[p.lit_id for p in papers[:45]]),
        clf.Group(name="流通渠道研究", lit_ids=[p.lit_id for p in papers[45:75]]),
        clf.Group(name="消费行为研究", lit_ids=[p.lit_id for p in papers[75:]]),
    ]
    balanced = clf._balance_oversize_groups(papers, groups, "水产品营销策略")
    assert [g.name for g in balanced] == [g.name for g in groups], (
        "未过半的主题不应被拆分"
    )


def test_balance_keeps_reasonable_groups_untouched():
    papers = [
        _paper(i, f"水产品营销研究维度{i % 4}视角分析") for i in range(1, 41)
    ]
    groups = [
        clf.Group(name="渠道与流通研究", lit_ids=[p.lit_id for p in papers[:12]]),
        clf.Group(name="消费行为研究", lit_ids=[p.lit_id for p in papers[12:26]]),
        clf.Group(name="电商营销模式研究", lit_ids=[p.lit_id for p in papers[26:]]),
    ]
    balanced = clf._balance_oversize_groups(papers, groups, "水产品营销策略")
    assert [g.name for g in balanced] == [g.name for g in groups], (
        "规模合理的主题不应被拆分"
    )


def test_inspect_theme_groups_rejects_dominant_theme():
    papers, groups = _pool_15_75()
    checks = {c.name: c for c in inspect_theme_groups(groups, papers)}
    assert "theme_balance" in checks
    assert not checks["theme_balance"].passed, "75/90 的巨型组必须被判失衡"
    # 拆分后必须通过
    balanced = clf._balance_oversize_groups(papers, groups, "水产品营销策略")
    checks = {c.name: c for c in inspect_theme_groups(balanced, papers)}
    assert checks["theme_balance"].passed


def test_semantic_cluster_end_to_end_stays_balanced(monkeypatch):
    """语义聚类对同质大池子的输出也不允许出现过半巨型组。"""
    monkeypatch.setattr(
        clf, "messages_create",
        lambda **kwargs: '{"names": ["主题甲", "主题乙", "主题丙", "主题丁"]}',
    )
    papers = [
        _paper(i, f"水产品营销策略与渠道模式研究{i % 10}") for i in range(1, 91)
    ]
    groups = clf._semantic_cluster_groups(papers, "水产品营销策略")
    assert groups
    largest = max(len(g.lit_ids) for g in groups)
    assert largest * 2 <= len(papers), (
        f"语义聚类产出巨型组 {largest}/{len(papers)},规模失衡未被修正"
    )
