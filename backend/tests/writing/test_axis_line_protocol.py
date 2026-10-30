# -*- coding: utf-8 -*-
"""轴规划行协议回归(2026-10-30 F1:JSON → 行协议迁移)。

线上四场真实跑批:轴规划 JSON 首试成功率 0/4 —— MiniMax M3 对 24 万
字符大文档稳定拒绝输出 JSON(整段英文思考散文),三次 attempt JSON 全败、
散文抢救三次全败(axes=0),策略链降级 minimal_abstracts 仍失败,整场中止。

照归组阶段 2a84c79 的成功先例,轴规划输出协议迁移为行协议为主:
`#序号|轴名|研究问题`(每轴一行,RQ 可空),解析链 = 行协议 → 既有 JSON
(兼容保留)→ 散文直提 → LLM 格式转换;全部候选仍经 _normalize_axes
唯一收口。response_format=json_object 同步移除(归组先例:去掉后反而稳定)。
"""
import json

import writing.classifier as clf
from retrieval.types import Paper, Source


def _paper(i: int) -> Paper:
    return Paper(
        lit_id=f"lit_cnki_{i:016x}",
        source=Source.CNKI,
        title=f"水产品营销问题{i}的机制与对策研究",
        authors=[f"作者{i}"],
        journal="测试学报",
        year=2024,
        abstract=f"本文研究水产品营销场景{i}下的品牌、渠道与消费行为问题。",
    )


# 今晚真实失败形态:MiniMax M3 整段英文思考散文 + 末尾按新协议输出行
_ACCIDENT_MIXED = """We need produce the theme axes for this corpus. Let me analyze the catalog carefully first. The papers cover branding, e-commerce channels, consumer behavior, sustainability trust, supply chain resilience and international markets. I will map each cluster to a concise axis name. Need ensure each axis covers at least two papers. Let's systematically group them now.

#1|品牌价值传播|本轴文献共同回答水产品品牌价值如何建构并向市场传播
#2|数字化渠道创新|本轴文献共同回答数字渠道如何重塑水产品流通与交易
#3|消费需求识别|本轴文献共同回答消费者偏好与购买决策呈现哪些结构性特征
#4|可持续信任机制|本轴文献共同回答可持续认证与信任机制如何影响市场
#5|数智供应链韧性|本轴文献共同回答数智技术如何提升供应链韧性
#6|国际市场适应|本轴文献共同回答出口企业如何适应国际市场壁垒"""

_ACCIDENT_AXIS_NAMES = [
    "品牌价值传播", "数字化渠道创新", "消费需求识别",
    "可持续信任机制", "数智供应链韧性", "国际市场适应",
]


# ---------- 解析器单测 ----------

def test_axis_line_protocol_basic():
    raw = (
        "#1|品牌价值传播|品牌价值如何建构与传播\n"
        "#2|数字化渠道创新|数字渠道如何重塑流通与交易\n"
        "#3|消费需求识别|消费者偏好呈现哪些特征\n"
    )
    items = clf._parse_axis_line_protocol(raw, 40)
    assert [(i["name"], i["research_question"]) for i in items] == [
        ("品牌价值传播", "品牌价值如何建构与传播"),
        ("数字化渠道创新", "数字渠道如何重塑流通与交易"),
        ("消费需求识别", "消费者偏好呈现哪些特征"),
    ]


def test_axis_line_protocol_drops_out_of_range_and_duplicates():
    """序号越界(>8 / ≤0)丢弃;重复序号以首行为准。"""
    raw = (
        "#1|区域品牌建设|RQ一\n"
        "#1|重复品牌建设|RQ重复\n"
        "#9|越界主题|RQ越界\n"
        "#0|零号主题|RQ零号\n"
        "#2|电商渠道创新|RQ二\n"
    )
    items = clf._parse_axis_line_protocol(raw, 40)
    assert [i["name"] for i in items] == ["区域品牌建设", "电商渠道创新"]


def test_axis_line_protocol_tolerates_empty_rq():
    """RQ 可空:#2|名称| 尾段留空与 #3|名称 无第三段都容忍。"""
    raw = (
        "#1|品牌价值传播|RQ完整\n"
        "#2|数字化渠道创新|\n"
        "#3|消费需求识别\n"
        "#4|国际市场适应|含|竖线的RQ原样保留\n"
    )
    items = clf._parse_axis_line_protocol(raw, 40)
    assert [i["name"] for i in items] == [
        "品牌价值传播", "数字化渠道创新", "消费需求识别", "国际市场适应",
    ]
    assert items[1]["research_question"] == ""
    assert items[2]["research_question"] == ""
    assert items[3]["research_question"] == "含|竖线的RQ原样保留"


def test_axis_line_protocol_fullwidth_pipe_and_bold():
    """容忍全角竖线与 markdown 粗体残渣(模型常见变体)。"""
    raw = "#1|**品牌价值传播**|RQ一\n#2｜数字化渠道创新｜RQ二\n"
    items = clf._parse_axis_line_protocol(raw, 40)
    assert [i["name"] for i in items] == ["品牌价值传播", "数字化渠道创新"]


def test_axis_line_protocol_floor_enforced():
    """有效行数低于 max(2, _min_groups(n)) 视为整体失败返回空。"""
    raw = "#1|品牌价值传播|RQ一\n"
    assert clf._parse_axis_line_protocol(raw, 40) == []
    # 空输入同样返回空
    assert clf._parse_axis_line_protocol("", 40) == []


def test_axis_line_protocol_ignores_prose_json_and_headings():
    """思考散文 / JSON / markdown 标题行不误匹配(行首 #数字| 才认)。"""
    prose = (
        "We need answer with exact axes. Let's map.\n"
        "## 主题轴规划\n"
        "1. **区域品牌建设** - 相关论文探讨建设路径。\n"
        '{"themes": [{"name": "JSON主题", "research_question": "RQ"}]}\n'
    )
    assert clf._parse_axis_line_protocol(prose, 40) == []


# ---------- 解析链与 _plan_theme_axes 集成 ----------

def test_plan_theme_axes_accepts_line_protocol_on_first_attempt(monkeypatch):
    """冒烟:事故同款「英文思考散文 + 末尾行协议」混合形态 → attempt1 由
    行协议收货,全程 1 次 LLM 调用,且不再请求 json_object。"""
    papers = [_paper(i) for i in range(1, 41)]
    calls: list[dict] = []

    def fake_create(**kwargs):
        calls.append(kwargs)
        return _ACCIDENT_MIXED

    monkeypatch.setattr(clf, "messages_create", fake_create)
    axes = clf._plan_theme_axes(papers, "水产品营销策略", "zh", abstract_limit=200)
    assert [a.name for a in axes] == _ACCIDENT_AXIS_NAMES
    assert len(calls) == 1, f"行协议首试应收货,实际 {len(calls)} 次调用"
    assert "response_format" not in calls[0], "行协议迁移后必须去掉 json_object"


def test_plan_theme_axes_json_still_parsed_as_fallback(monkeypatch):
    """兼容保留:模型仍回 JSON 时经既有 _parse_axis_response 解析成功。"""
    papers = [_paper(i) for i in range(1, 41)]
    payload = json.dumps({"themes": [
        {"name": "区域品牌建设", "research_question": "区域品牌如何建设"},
        {"name": "电商渠道创新", "research_question": "电商渠道如何创新"},
        {"name": "消费行为分析", "research_question": "消费行为有何规律"},
    ]}, ensure_ascii=False)

    def fake_create(**kwargs):
        return payload

    monkeypatch.setattr(clf, "messages_create", fake_create)
    axes = clf._plan_theme_axes(papers, "水产品营销策略", "zh", abstract_limit=200)
    assert [a.name for a in axes] == ["区域品牌建设", "电商渠道创新", "消费行为分析"]


def test_plan_theme_axes_rq_partially_missing_allowed(monkeypatch):
    """部分轴 RQ 空缺:轴照常收货,research_question 为 None(下游容忍)。"""
    papers = [_paper(i) for i in range(1, 41)]
    raw = (
        "Let me think about the clusters first. Need at least three axes here. "
        "Branding, channels and consumers are the main clusters.\n"
        "#1|品牌价值传播|RQ一\n"
        "#2|数字化渠道创新|\n"
        "#3|消费需求识别\n"
    )

    def fake_create(**kwargs):
        return raw

    monkeypatch.setattr(clf, "messages_create", fake_create)
    axes = clf._plan_theme_axes(papers, "水产品营销策略", "zh", abstract_limit=200)
    assert [a.name for a in axes] == ["品牌价值传播", "数字化渠道创新", "消费需求识别"]
    assert axes[0].research_question == "RQ一"
    assert axes[1].research_question is None
    assert axes[2].research_question is None
