# -*- coding: utf-8 -*-
"""归组阶段行协议与思考文本抢救专项回归。

线上事故(2026-10-19,90 篇池):MiniMax 在归组阶段无视「只输出 JSON」,
改为逐篇分析(「#1 标题 - 分析…Clearly 品牌建设」),输出预算被思考
吃光,六次尝试解析全部为 0,15 分钟后整场作废。修复 = 三层解析:
JSON(兼容旧行为)→ 行协议(主协议,每篇一行)→ 思考文本零成本抢救;
覆盖 ≥90% 提前收货,余量交给孤儿回收。
"""
import writing.classifier as clf
from retrieval.types import Paper, Source


def _paper(i: int, title: str) -> Paper:
    return Paper(
        lit_id=f"lit_cnki_{i:016x}",
        source=Source.CNKI,
        title=title,
        authors=[f"作者{i}"],
        journal="测试学报",
        year=2023,
        abstract=f"{title}。本文研究相关机制、渠道与消费行为的协同问题,内容充实。",
    )


def _papers(n: int) -> list[Paper]:
    titles = [
        "漳州海产品区域品牌建设研究", "直播电商渠道在水产品销售中的应用",
        "消费者水产品购买意愿影响因素", "渔业区域公用品牌培育路径",
        "社区团购模式下的水产品渠道变革", "水产品消费结构升级研究",
        "县域水产品品牌营销策略", "水产品电商直播带货模式创新",
        "基于计划的购买行为理论应用", "水产品品牌溢价机制研究",
        "生鲜电商冷链渠道优化", "绿色消费理念与水产品购买决策",
    ]
    return [_paper(i + 1, titles[i % len(titles)]) for i in range(n)]


_AXES = [
    clf.Group(name="水产品区域品牌建设研究", lit_ids=[], research_question=None),
    clf.Group(name="水产品电商渠道创新", lit_ids=[], research_question=None),
    clf.Group(name="水产品消费行为分析", lit_ids=[], research_question=None),
]


def test_line_protocol_basic():
    raw = "3. 水产品电商渠道创新\n7. 水产品区域品牌建设研究\n12. 水产品消费行为分析"
    items = clf._parse_assign_line_protocol(raw, _AXES, 20)
    by_name = {i["name"]: i["ids"] for i in items}
    assert by_name == {
        "水产品电商渠道创新": [3],
        "水产品区域品牌建设研究": [7],
        "水产品消费行为分析": [12],
    }


def test_line_protocol_tolerates_hash_and_reasoning_noise():
    """#3 前缀与思考叙述行不得误匹配;行内编号不解析。"""
    raw = (
        "I will assign each paper now.\n"
        "#3. 水产品电商渠道创新\n"
        "1. First, let me analyze the catalog\n"
        "5. 水产品消费行为分析\n"
    )
    items = clf._parse_assign_line_protocol(raw, _AXES, 20)
    by_name = {i["name"]: i["ids"] for i in items}
    assert by_name == {
        "水产品电商渠道创新": [3],
        "水产品消费行为分析": [5],
    }


def test_line_protocol_last_decision_wins():
    """同一编号重复出现(模型回头改判)以最后一次为准。"""
    raw = "3. 水产品电商渠道创新\n3. 水产品消费行为分析"
    items = clf._parse_assign_line_protocol(raw, _AXES, 20)
    by_name = {i["name"]: i["ids"] for i in items}
    assert by_name == {"水产品消费行为分析": [3]}


def test_reasoning_salvage_uses_conclusion_markers():
    """事故同款思考文本:每篇的归属判断在 Clearly/归入 结论里。"""
    raw = (
        "Let me go through each paper:\n"
        "#1 | 漳州海产品区域品牌建设研究 | 2023 - Regional seafood branding. Clearly 区域品牌\n"
        "#2 | 渔业区域公用品牌培育路径 | 2023 - Brand cultivation. "
        "Clearly \"水产品区域品牌建设研究\"\n"
        "#3 | 直播电商渠道在水产品销售中的应用 | 2023 - E-commerce live streaming. "
        "归入 水产品电商渠道创新\n"
        "#4 | 消费者水产品购买意愿影响因素 | 2023 - Consumer willingness. 属于 水产品消费行为分析\n"
    )
    items = clf._salvage_assign_from_reasoning(raw, _AXES, 10)
    by_name = {i["name"]: sorted(i["ids"]) for i in items}
    assert by_name == {
        "水产品区域品牌建设研究": [1, 2],
        "水产品电商渠道创新": [3],
        "水产品消费行为分析": [4],
    }


def test_reasoning_salvage_skips_ambiguous_entries():
    """无结论标记且 4-gram 并列的条目不猜(宁缺勿错)。"""
    raw = "#4 | 某某营销策略研究 | 2020 - generic marketing strategy research text"
    items = clf._salvage_assign_from_reasoning(raw, _AXES, 10)
    assert items == []


def test_assign_recovers_from_reasoning_only_output(monkeypatch):
    """集成:模型只输出思考文本(线上事故形态)→ 抢救 + 孤儿回收,
    照样产出完整分组,不再整场作废。"""
    papers = _papers(12)

    def fake_create(**kwargs):
        lines = []
        for i in range(1, 13):
            p = papers[i - 1]
            if "品牌" in p.title:
                axis = "水产品区域品牌建设研究"
            elif "电商" in p.title or "渠道" in p.title or "团购" in p.title:
                axis = "水产品电商渠道创新"
            else:
                axis = "水产品消费行为分析"
            lines.append(
                f"#{i} | {p.title} | 2023 - Analysis of {p.title[:8]}. "
                f"Clearly {axis[:4]}"
            )
        return "Let me go through each paper:\n" + "\n".join(lines)

    monkeypatch.setattr(clf, "messages_create", fake_create)
    groups = clf._assign_papers_to_axes(
        papers, _AXES, "水产品营销策略", abstract_limit=100,
    )
    covered = {lid for g in groups for lid in g.lit_ids}
    assert covered == {p.lit_id for p in papers}
    assert [g.name for g in groups] == [a.name for a in _AXES]
