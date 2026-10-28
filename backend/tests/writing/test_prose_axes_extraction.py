# -*- coding: utf-8 -*-
"""散文轴提取(零成本兜底)专项回归。

线上实测 MiniMax 在 json_object 模式下仍可能输出 markdown 编号列表,
主题簇归纳质量很好、只是格式不合规。_parse_prose_axes 用正则直提,
避免动用 LLM 二次转换;_plan_theme_axes 在 JSON 解析失败后先走这条
零成本路径,再降级到 LLM 抢救 —— 这是主题划分 agent 循环中
"解析失败 → 换解析策略再试"的一层。

2026-10-26 事故后 _parse_prose_axes 改为纯解析器:只提取候选 items,
准入决策(名称校验/判重/截断)全部收敛到唯一出口 _normalize_axes。
"""
import writing.classifier as clf
from retrieval.types import Paper, Source


# 线上 MiniMax 真实输出形态:json_object 模式下的 markdown 编号+粗体列表
_PROSE = """根据对文献池的通读分析,建议划分为以下研究主题:

1. **区域品牌建设** - 该主题下的论文主要探讨区域公用品牌的建设路径与主体协同。
2. **电商营销渠道** - 相关论文聚焦直播电商与社群团购等新兴渠道对销售的影响。
3. **消费者购买意愿** - 论文集中研究影响消费者购买行为的因素。

以上就是主题划分方案。
"""


def _paper(i: int) -> Paper:
    return Paper(
        lit_id=f"lit_cnki_{i:016x}",
        source=Source.CNKI,
        title=f"研究问题场景{i % 5}的治理机制{i}",
        authors=[f"作者{i}"],
        journal="测试学报",
        year=2024,
        abstract=f"本文研究场景{i % 5}下的治理机制与协同问题。",
    )


def test_parse_prose_axes_extracts_numbered_bold_themes():
    axes = clf._normalize_axes(clf._parse_prose_axes(_PROSE), 40, "zh")
    assert [a.name for a in axes] == ["区域品牌建设", "电商营销渠道", "消费者购买意愿"]


def test_parse_prose_axes_keeps_rq_from_dash_tail():
    """编号项破折号后的说明文字足够长时,作为该轴的研究问题保留。"""
    axes = clf._normalize_axes(clf._parse_prose_axes(_PROSE), 40, "zh")
    assert axes[0].research_question
    assert "品牌" in axes[0].research_question


def test_parse_prose_axes_dedups_bold_and_numbered_forms():
    """同一主题以编号+粗体重复出现时只保留一条,粗体分支不重复入列。"""
    items = clf._parse_prose_axes(_PROSE)
    names = [str(it.get("name")) for it in items]
    assert len(names) == len(set(names))


def test_parse_prose_axes_bare_bold_without_numbers():
    """MiniMax 也可能输出不带编号的裸粗体标题,同样要能提取。"""
    raw = "建议主题如下:\n\n**冷链物流效率**\n\n**质量安全认证**\n\n以上。"
    axes = clf._normalize_axes(clf._parse_prose_axes(raw), 20, "zh")
    assert [a.name for a in axes] == ["冷链物流效率", "质量安全认证"]


def test_prose_pipeline_filters_narrative_junk_at_single_gate():
    """叙述性引导句在解析层保留为候选,由唯一收口点拒绝。"""
    raw = (
        "首先我们来看第一个主题。\n"
        "1. **近年来** - 相关研究不断涌现。\n"
        "2. **产业链视角下的养殖户组织模式** - 论文探讨组织化程度。\n"
        "3. **养殖户合作行为** - 论文研究合作意愿。\n"
    )
    items = clf._parse_prose_axes(raw)
    # 纯解析器不做准入决策:"近年来"也会出现在候选里
    assert "近年来" in {str(it.get("name")) for it in items}
    axes = clf._normalize_axes(items, 20, "zh")
    axis_names = [a.name for a in axes]
    assert "近年来" not in axis_names
    assert "产业链视角下的养殖户组织模式" in axis_names
    assert "养殖户合作行为" in axis_names


def test_plan_theme_axes_accepts_prose_without_llm_salvage(monkeypatch):
    """轴规划三次都输出散文 → 第 1 次就由正则直提接受,全程只发 1 次
    LLM 调用(不再花钱做格式转换),也不走本地聚类。"""
    papers = [_paper(i) for i in range(1, 41)]
    calls: list[dict] = []

    def fake_create(**kwargs):
        calls.append(kwargs)
        return _PROSE  # 永远输出散文,模拟 MiniMax 格式失灵

    monkeypatch.setattr(clf, "messages_create", fake_create)
    axes = clf._plan_theme_axes(papers, "任意研究主题", "zh", abstract_limit=200)
    assert [a.name for a in axes] == [
        "区域品牌建设", "电商营销渠道", "消费者购买意愿",
    ]
    assert len(calls) == 1, f"散文直提不应触发额外 LLM 调用,实际 {len(calls)} 次"
