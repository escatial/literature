# -*- coding: utf-8 -*-
"""占位主题名回归:模型照抄 prompt 示例占位词("主题名"/"<组名>")的防线。

线上案例:格式修复/归并示例里写了 {"name":"主题名"},MiniMax 照抄后
一个 28 篇的主题以"主题名"出现在确认面板。契约:
1. _group_name_acceptable 必须整词拒绝示例占位名(含可选引号/尖括号);
2. 正常主题名不受影响;
3. _finalize_theme_groups 对占位名组做证据重命名,文献不丢。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from retrieval.types import Paper, Source
import writing.classifier as clf


@pytest.mark.parametrize("name", [
    "主题名", "<主题名>", "组名", "<组名>", "主题名称",
    "示例主题", "主题", "分组", '"主题名"',
])
def test_prompt_example_placeholders_are_rejected(name):
    assert not clf._group_name_acceptable(name), f"占位名应被拒绝: {name}"


@pytest.mark.parametrize("name", [
    "水产品品牌建设",
    "特定品种水产品市场拓展",
    "突发公共卫生事件对水产行业的影响",
])
def test_real_theme_names_still_accepted(name):
    assert clf._group_name_acceptable(name), f"正常主题名应保留: {name}"


def test_finalize_renames_placeholder_group_from_evidence():
    titles = [
        "直播带货中的水产品营销策略",
        "社区团购渠道的水产品流通模式",
        "水产品品牌联名合作路径",
        "短视频平台海鲜营销案例分析",
        "水产品直播电商用户复购研究",
        "生鲜电商冷链配送优化",
        "预制菜水产品市场拓展策略",
        "水产品溯源体系与消费者信任",
    ]
    papers = [
        Paper(
            lit_id=f"lit_cnki_{i:016x}", source=Source.CNKI, title=t,
            authors=[f"作者{i}"], journal="水产经济", year=2024,
            abstract=f"研究{t}的策略与路径。",
        )
        for i, t in enumerate(titles, start=1)
    ]
    groups = [clf.Group(name="主题名", lit_ids=[p.lit_id for p in papers])]
    out = clf._finalize_theme_groups(papers, groups, "水产品营销策略")
    assert out, "占位名组经证据重命名后不应被丢弃"
    assert all(g.name != "主题名" for g in out)
    assert all(clf._group_name_acceptable(g.name) for g in out)
    covered = sorted(lid for g in out for lid in g.lit_ids)
    assert covered == sorted(p.lit_id for p in papers), "重命名不允许丢文献"
