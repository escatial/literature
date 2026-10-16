# -*- coding: utf-8 -*-
"""降级命名提取质量 + 主题轴散文抢救回归测试。

线上事故(2026-10-15 第二起):中文池确认面板出现
- "世界都在吃"(通威鱼广告口号,4-5 字短语被当主题名原料)
- "产业链视角"("产业链视角下福建泉州…研究"被分句逻辑切成视角残片)
两例都出自 local_fallback 的标题短语提取器,格式合格但语义残缺。
"""
import json

import pytest

import writing.classifier as clf
from retrieval.types import Paper, Source


def _paper(i, title, source=Source.CNKI):
    return Paper(
        lit_id=f"lit_{i:03d}", source=source, title=title,
        authors=["张三"], journal="J", year=2023, abstract="摘要",
    )


# ---------- 提取器质量 ----------

def test_slogan_title_not_used_as_name():
    """广告口号短语(5 字)不再作为主题名原料,取信息量更大的长短语。"""
    name = clf._best_title_phrase(
        "通威鱼,世界都在吃!通威海南纳生鲤鱼整合营销传播策略", "zh",
    )
    assert name != "世界都在吃"
    assert len(name) >= 6
    assert clf._group_name_acceptable(name, pool_language="zh")


def test_perspective_prefix_not_chosen_over_object():
    """"X视角下Y"的名称取对象半边(Y),不再取视角残片(X视角)。"""
    name = clf._best_title_phrase(
        "产业链视角下福建泉州海洋渔业产业现代化发展研究——以泉州为例", "zh",
    )
    assert name == "福建泉州海洋渔业产业现代化发展"


def test_object_side_extracted_from_de_marker():
    """"X的Y"标题在超长时取信息量更大的 Y 半边。"""
    name = clf._best_title_phrase("消费者行为的水产品营销策略研究", "zh")
    assert name == "水产品营销策略"


def test_background_marker_object_extracted():
    name = clf._best_title_phrase("乡村振兴背景下福清沙浦镇海产品内容营销策略研究", "zh")
    assert name == "福清沙浦镇海产品内容营销策略"


def test_short_title_kept_whole():
    """不超长且无切分必要的短标题保持原样。"""
    assert clf._best_title_phrase("渔村的营销转型", "zh") == "渔村的营销转型"


# ---------- 大文档规划(用户策略:先拼接、再划分) ----------

def test_corpus_document_budget_and_numbering():
    from writing.classifier import _build_corpus_document
    papers = [
        Paper(lit_id="a", source=Source.CNKI, title="甲主题研究", authors=["张三"], journal="J", year=2024, abstract="全" * 3000),
        Paper(lit_id="b", source=Source.OPENALEX, title="Salmon marketing", authors=["Lee"], journal="J", year=2025, abstract="x" * 500),
        Paper(lit_id="c", source=Source.CNKI, title="无摘要文献", authors=["王五"], journal="J", year=2023, abstract=None),
    ]
    doc = _build_corpus_document(papers)
    # 预算内:完整摘要、连续编号、无摘要也有占位
    assert all(f"# {i}" in doc for i in (1, 2, 3))
    assert "全" * 3000 in doc
    assert "(无摘要)" in doc
    # 超预算:摘要按篇缩减,但任何文献都不丢号
    doc2 = _build_corpus_document(papers, budget_chars=800)
    assert ("全" * 3000) not in doc2
    assert all(f"# {i}" in doc2 for i in (1, 2, 3))


def test_plan_uses_corpus_document(monkeypatch):
    """plan 阶段的 catalog 应为全池大文档(完整摘要),而非 600 字截断目录。"""
    captured = {}

    def fake_llm(system, user, **kwargs):
        captured["system"] = system
        return json.dumps({"themes": [
            {"name": "区域水产品品牌建设", "research_question": "区域特色水产品如何建设品牌"},
            {"name": "生鲜电商营销渠道", "research_question": "生鲜电商如何重构营销渠道"},
            {"name": "水产品消费行为", "research_question": "消费者为何购买水产品"},
        ]}, ensure_ascii=False)

    monkeypatch.setattr(clf, "messages_create", fake_llm)
    papers = [
        Paper(lit_id=f"lit_{i:03d}", source=Source.CNKI, title=f"水产品品牌建设研究{i}",
              authors=["张三"], journal="J", year=2024, abstract="全" * 1500)
        for i in range(1, 31)
    ]
    clf._plan_theme_axes(papers, "水产品营销策略", "zh", abstract_limit=600)
    system_text = captured["system"]
    # 完整摘要(1500 字)未按 600 截断 → 大文档生效
    assert "全" * 1500 in system_text

# ---------- plan 阶段散文抢救 ----------

_PROSE = (
    "1. **Regional brand building** - Papers about 高邮大虾 (#3) and 灵山岛海参 (#10)\n"
    "2. **E-commerce marketing** - Papers about 生鲜电商 (#5) and 直播带货 (#7)\n"
    "3. **Consumer behavior** - Papers about 消费者购买意愿 (#12) and 消费偏好 (#15)"
)
_SALVAGED_JSON = json.dumps({"themes": [
    {"name": "区域水产品品牌建设", "research_question": "区域特色水产品如何建设品牌"},
    {"name": "生鲜电商营销渠道", "research_question": "生鲜电商如何重构营销渠道"},
    {"name": "水产品消费行为", "research_question": "消费者为何购买水产品"},
]}, ensure_ascii=False)


def test_plan_salvages_prose_output(monkeypatch):
    """散文输出后立即抢救:第 1 次散文 + 第 1 次转换即通过,不再空转两轮。"""
    calls = {"n": 0}

    def fake_llm(system, user, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return _PROSE  # 第一次 markdown 散文(线上实测形态)
        assert "格式转换器" in system and "分组方案文本" in user
        return _SALVAGED_JSON

    monkeypatch.setattr(clf, "messages_create", fake_llm)
    papers = [_paper(i, f"水产品品牌建设研究{i}", ) for i in range(1, 31)]
    axes = clf._plan_theme_axes(papers, "水产品营销策略", "zh", abstract_limit=600)
    assert [a.name for a in axes] == [
        "区域水产品品牌建设", "生鲜电商营销渠道", "水产品消费行为",
    ]
    assert axes[0].research_question
    assert calls["n"] == 2  # 1 次散文 + 1 次抢救(v9.10:不等三次全败)


def test_plan_salvage_failure_still_raises(monkeypatch):
    """抢救也失败时保留原有抛错语义(不让坏数据静默通过)。"""
    def fake_llm(system, user, **kwargs):
        return _PROSE  # 每次尝试与抢救都返回散文 → _normalize_axes 空 → 抛错

    monkeypatch.setattr(clf, "messages_create", fake_llm)
    papers = [_paper(i, f"水产品品牌建设研究{i}") for i in range(1, 31)]
    with pytest.raises(ValueError, match="主题轴规划失败"):
        clf._plan_theme_axes(papers, "水产品营销策略", "zh", abstract_limit=600)
