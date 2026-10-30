# -*- coding: utf-8 -*-
"""归组阶段行协议与思考文本抢救专项回归。

线上事故(2026-10-19,90 篇池):MiniMax 在归组阶段无视「只输出 JSON」,
改为逐篇分析(「#1 标题 - 分析…Clearly 品牌建设」),输出预算被思考
吃光,六次尝试解析全部为 0,15 分钟后整场作废。修复 = 三层解析:
JSON(兼容旧行为)→ 行协议(主协议,每篇一行)→ 思考文本零成本抢救;
覆盖 ≥90% 提前收货,余量交给孤儿回收。

线上事故(2026-10-30,90 篇中文为主池):归组三次整段输出英文思考散文,
「≥2 个 4-gram 才认」的收紧门槛把抢救网饿死(仅捞回 6 组 10 篇);
同时 `not best` 触发条件让自由分组退路被残局短路,结构检查直接拒绝。
修复 = 标记 + ≥1 命中即接受(无标记仍 ≥2)+ 覆盖未达验收线也降级
自由分组。
"""
import json

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


# ============ 真实事故回归(2026-10-30,90 篇中文为主池) ============
# 服务端日志:轴规划散文抢救出 6 个中文轴后,归组三次 MiniMax 全部整段
# 输出英文思考散文(行协议没写),原文预览逐字见 _ACCIDENT_PREAMBLE。

_ACCIDENT_AXES = [
    "品牌价值传播", "数字化渠道创新", "消费需求识别",
    "可持续信任机制", "数智供应链韧性", "国际市场适应",
]

_ACCIDENT_GLOSSES = {
    "品牌价值传播": "branding positioning geographical indication cultural narrative IMC",
    "数字化渠道创新": "ecommerce social live omnichannel reach transaction channels",
    "消费需求识别": "consumer preferences quality nutrition convenience sustainability purchase intention",
    "可持续信任机制": "sustainable trust certification traceability reputation",
    "数智供应链韧性": "digital supply chain resilience disruption recovery",
    "国际市场适应": "export market adaptation trade barriers localization",
}

# 每轴一个只含该轴 ≤2 个 4-gram(多数仅 1 个)的真实感中文题名:事故里
# 思考文本的分配行判词是英文复述,中文轴名很难凑满 ≥2 命中,只有
# 「结论标记 + 单命中」可辨(轴名 4-gram 见 classifier._grams 的 4 字窗口)。
_ACCIDENT_SINGLE_TITLES = {
    "品牌价值传播": "水产品品牌价值与溢价机制研究",
    "数字化渠道创新": "生鲜电商渠道创新的案例研究",
    "消费需求识别": "城乡居民水产品消费需求调查研究",
    "可持续信任机制": "消费者信任机制与购买意愿研究",
    "数智供应链韧性": "水产品供应链韧性的影响因素研究",
    "国际市场适应": "出口企业市场适应行为研究",
}

# 逐字轴名题名(事故里少量条目带轴名原词,≥2 门槛下唯一的幸存者形态)。
_ACCIDENT_VERBATIM_TITLES = {
    "品牌价值传播": "水产品品牌价值传播的影响因素研究",
    "数字化渠道创新": "水产品数字化渠道创新的实现路径研究",
    "消费需求识别": "水产品消费需求识别与细分研究",
    "可持续信任机制": "水产品可持续信任机制构建研究",
    "数智供应链韧性": "数智供应链韧性及其形成机制研究",
    "国际市场适应": "水产品国际市场适应策略研究",
}

# 服务端真实日志预览原文(逐字,截断处之后按同形态补全)。
# 注意「assigning each #1-90」:首个 #1 会被前导散文抢注,该篇只能靠孤儿回收。
_ACCIDENT_PREAMBLE = (
    "We need answer exactly 90 lines assigning each #1-90 one of six exact "
    "names. Need carefully classify based research questions. Need not output "
    "anything else. Let's map.  Axes: 1 品牌价值传播: branding positioning GI "
    "cultural narrative IMC. 2 数字化渠道创新: ecommerce social live "
    "omnichannel reach transaction channels. 3 消费需求识别: consumer "
    "preferences quality nutrition convenience sustainability, purchase "
    "intention. 4 可持续信任机制: sustainable trust certification "
    "traceability. 5 数智供应链韧性: digital supply chain resilience. "
    "6 国际市场适应: export market adaptation."
)


def _accident_axis_groups() -> list[clf.Group]:
    return [
        clf.Group(name=name, lit_ids=[], research_question=None)
        for name in _ACCIDENT_AXES
    ]


def _accident_papers() -> list[Paper]:
    """90 篇中文为主池:#85-90 带逐字轴名题名,其余为单 4-gram 题名。"""
    out: list[Paper] = []
    for i in range(1, 91):
        axis = _ACCIDENT_AXES[(i - 1) % 6]
        title = (
            _ACCIDENT_VERBATIM_TITLES[axis] if i >= 85
            else _ACCIDENT_SINGLE_TITLES[axis]
        )
        out.append(_paper(i, title))
    return out


def _accident_output() -> str:
    """事故同款完整输出:英文思考前导 + 逐篇「#N 标题 - 分析 → 判词」。"""
    lines = [_ACCIDENT_PREAMBLE]
    for i in range(1, 91):
        axis = _ACCIDENT_AXES[(i - 1) % 6]
        gloss = _ACCIDENT_GLOSSES[axis]
        if i >= 85:
            lines.append(
                f"#{i} {_ACCIDENT_VERBATIM_TITLES[axis]} - {gloss}. Clearly {axis}"
            )
        else:
            marker = "Clearly" if i % 2 else "→"
            lines.append(
                f"#{i} {_ACCIDENT_SINGLE_TITLES[axis]} - {gloss}. {marker} {gloss}"
            )
    return "\n".join(lines)


def test_accident_reasoning_salvage_covers_all_axes():
    """事故原文形态端到端抢救:6 组、89 篇(#1 被前导抢注,交孤儿回收)。

    ≥2 收紧门槛下同文本只能捞回 6 篇 —— 本用例即其回归护栏。
    """
    items = clf._salvage_assign_from_reasoning(
        _accident_output(), _accident_axis_groups(), 90,
    )
    by_name = {i["name"]: sorted(i["ids"]) for i in items}
    assert set(by_name) == set(_ACCIDENT_AXES)
    assert sorted(n for ids in by_name.values() for n in ids) == list(range(2, 91))
    assert all(len(ids) >= 14 for ids in by_name.values())


def test_reasoning_salvage_markerless_single_hit_still_rejected():
    """防误命中本意保留:无结论标记且仅 1 个 4-gram 命中不放行。"""
    raw = "#4 | 水产品品牌价值与溢价机制研究 | 2023 - no explicit verdict here"
    items = clf._salvage_assign_from_reasoning(raw, _accident_axis_groups(), 10)
    assert items == []


def test_reasoning_salvage_marker_single_hit_tie_not_guessed():
    """有标记但两轴各 1 个 4-gram 并列 → 仍不猜,留给孤儿回收。"""
    raw = "#5 | 水产品价值传播与市场适应协同研究 | 2022 - mixed signals. Clearly one of them"
    items = clf._salvage_assign_from_reasoning(raw, _accident_axis_groups(), 10)
    assert items == []


def test_accident_assign_salvage_accepted_on_first_attempt(monkeypatch):
    """集成:90 篇池事故输出 → 抢救 89/90 ≥90% 验收线,attempt1 收货,
    不再烧后续尝试;#1 由孤儿回收按标题相似度补齐。"""
    papers = _accident_papers()
    calls = {"count": 0}

    def fake_create(**kwargs):
        calls["count"] += 1
        return _accident_output()

    monkeypatch.setattr(clf, "messages_create", fake_create)
    groups = clf._assign_papers_to_axes(
        papers, _accident_axis_groups(), "水产品营销策略", abstract_limit=120,
    )
    assert calls["count"] == 1
    assert {g.name for g in groups} == set(_ACCIDENT_AXES)
    covered = {lid for g in groups for lid in g.lit_ids}
    assert covered == {p.lit_id for p in papers}


def test_assign_below_bar_after_three_attempts_falls_back_to_free_grouping(monkeypatch):
    """三次归组各只抢回 6 篇(6/90 < 90% 验收线):自由分组退路必须触发。

    旧逻辑 `not best` 被残局短路 → 结构检查拒绝「N 篇文献未归组」;
    修复后第 4 次调用自由分组,产出经 _enforce_group_budget 预算闸的
    合法分组并完整覆盖。
    """
    papers = _accident_papers()
    free_names = [
        "水产品品牌溢价机制研究",
        "水产品电商渠道创新研究",
        "水产品消费需求演进研究",
    ]
    free_json = json.dumps({"groups": [
        {"name": free_names[0], "ids": list(range(1, 31))},
        {"name": free_names[1], "ids": list(range(31, 61))},
        {"name": free_names[2], "ids": list(range(61, 91))},
    ]}, ensure_ascii=False)
    assign_raw = "\n".join(
        f"#{i} {_ACCIDENT_VERBATIM_TITLES[axis]} - "
        f"{_ACCIDENT_GLOSSES[axis]}. Clearly {axis}"
        for i, axis in zip(range(85, 91), _ACCIDENT_AXES)
    )
    calls = {"count": 0}

    def fake_create(**kwargs):
        calls["count"] += 1
        return assign_raw if calls["count"] <= 3 else free_json

    monkeypatch.setattr(clf, "messages_create", fake_create)
    events: list[dict] = []
    groups = clf._assign_papers_to_axes(
        papers, _accident_axis_groups(), "水产品营销策略",
        abstract_limit=120, progress=events.append,
    )
    assert calls["count"] == 4
    assert any(e.get("kind") == "assign_fallback_started" for e in events)
    assert {g.name for g in groups} == set(free_names)
    assert len(groups) <= clf._AXIS_MAX
    covered = {lid for g in groups for lid in g.lit_ids}
    assert covered == {p.lit_id for p in papers}
