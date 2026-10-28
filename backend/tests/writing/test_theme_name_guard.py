# -*- coding: utf-8 -*-
"""主题命名防线回归测试(2026-10-15 线上 423 篇池事故复盘)。

线上事故:中文主题「水产品营销策略」的确认面板出现
- 主题 1 "背景下福清沙浦镇海产品内容营销策略研究"  ← 论文标题砍头中段碎片
- 主题 2 "的新疆昌吉市鲜活水产品的营销渠道优化研究" ← 切头残渣(的)
- 主题 3 "Sea Table Role Traceability"             ← 纯英文桶 + 英文标题证据名
根因(三条缝):碎片检测只认前缀、pool_language 口径不统一、缩写豁免
正则放行任意全大写词。本文件用真实泄漏名锁定修复行为。
"""
import pytest

import writing.classifier as clf
from retrieval.types import Paper, Source


def _paper(i: int, title: str, source=Source.CNKI) -> Paper:
    return Paper(
        lit_id=f"lit_{i:03d}", source=source, title=title,
        authors=["张三"], journal="J", year=2023, abstract="摘要",
    )


# ---------- 线上泄漏的三个碎片名,必须被判定为不合格 ----------

LEAKED_CN_TITLE = "乡村振兴背景下福清沙浦镇海产品内容营销策略研究"
LEAKED_EN_TITLE = "Sea Table Role Traceability in Aquaculture Supply Chains"

LEAKED_PAPERS = [
    _paper(1, LEAKED_CN_TITLE),
    _paper(2, "的新疆昌吉市鲜活水产品的营销渠道优化研究"),
    _paper(3, "消费者行为的水产品营销策略研究"),
    _paper(101, LEAKED_EN_TITLE, source=Source.OPENALEX),
]


def test_mid_title_fragment_rejected():
    """主题 1 复现:标题砍头中段碎片(去掉了"乡村振兴")必须拒绝。"""
    assert clf._looks_like_title_prefix(
        "背景下福清沙浦镇海产品内容营销策略研究", LEAKED_PAPERS, ["lit_001"],
    )


def test_seam_residue_rejected():
    """主题 2 复现:以"的"开头的切头残渣必须拒绝。"""
    assert clf._looks_like_title_prefix(
        "的新疆昌吉市鲜活水产品的营销渠道优化研究", LEAKED_PAPERS, ["lit_002"],
    )


def test_english_fragment_rejected_in_zh_pool():
    """主题 3 复现:中文池下纯英文名(无中文、无白名单缩写)必须拒绝。"""
    assert not clf._group_name_acceptable(
        "Sea Table Role Traceability", pool_language="zh",
    )


def test_covid_style_caps_no_longer_exempt():
    """缩写豁免收口:COVID 这类普通大写词不再让英文名在中文池放行。"""
    assert not clf._group_name_acceptable(
        "Emerging COVID-19 impacts responses", pool_language="zh",
    )


def test_legit_acronyms_still_exempt():
    """白名单内的学术缩写(AI/B2B)仍可豁免。"""
    assert clf._group_name_acceptable("AI adoption research", pool_language="zh")
    assert clf._group_name_acceptable("B2B marketing research", pool_language="zh")


# ---------- 不误伤:合法合成名/泛化改写仍放行 ----------

def test_generalized_rewrite_not_treated_as_copy():
    """去掉"我国"和"研究"的泛化改写不是标题复制(既有回归语义)。"""
    papers = [_paper(1, "我国水产品出口企业国际营销策略研究")]
    assert not clf._looks_like_title_prefix(
        "水产品出口企业国际营销策略", papers, [papers[0].lit_id],
    )


def test_legit_synthesized_name_passes_zh_pool():
    assert clf._group_name_acceptable("消费者行为的水产品营销策略", pool_language="zh")
    assert clf._group_name_acceptable("疫情对水产品市场的影响", pool_language="zh")


def test_long_title_fragment_rejected_by_length():
    assert not clf._group_name_acceptable(
        "背景下福清沙浦镇海产品内容营销策略研究", pool_language="zh",
    )


# ---------- zh 池纯英文桶:强制中文兜底名 ----------

def test_english_group_renamed_for_zh_pool():
    groups = [clf.Group(name="Sea Table Role Traceability", lit_ids=["lit_101"])]
    clf._rename_english_groups_for_zh_pool(LEAKED_PAPERS, groups, "水产品营销策略")
    assert groups[0].name == "水产品营销策略国际研究"
    assert clf._group_name_acceptable(groups[0].name, pool_language="zh")


def test_mixed_or_chinese_groups_untouched():
    groups = [
        clf.Group(name="水产品营销策略研究", lit_ids=["lit_001", "lit_101"]),
        clf.Group(name="消费者行为的水产品营销策略", lit_ids=["lit_003"]),
    ]
    clf._rename_english_groups_for_zh_pool(LEAKED_PAPERS, groups, "水产品营销策略")
    assert [g.name for g in groups] == [
        "水产品营销策略研究", "消费者行为的水产品营销策略",
    ]


def test_en_pool_english_names_untouched():
    """英文池不做中文强制,英文名原样保留。"""
    en_papers = [_paper(101, LEAKED_EN_TITLE, source=Source.OPENALEX)]
    groups = [clf.Group(name="Sea Table Role Traceability", lit_ids=["lit_101"])]
    clf._rename_english_groups_for_zh_pool(en_papers, groups, "seafood marketing")
    assert groups[0].name == "Sea Table Role Traceability"


# ---------- harness 结构核查:名字质量进检查集 ----------

def test_harness_inspects_name_quality():
    # ⑦恢复真实检查(2026-10-27):标题碎片名阻断(ok=False),
    # 短名/零重叠告警走 metrics;旧 bad_names 字段由 unacceptable 承接。
    from agent.harness import inspect_theme_groups
    groups = [
        clf.Group(name="背景下福清沙浦镇海产品内容营销策略研究", lit_ids=["lit_001"]),
        clf.Group(name="消费者行为的水产品营销策略", lit_ids=["lit_003"]),
    ]
    checks = inspect_theme_groups(groups, LEAKED_PAPERS)
    by_name = {c.name: c for c in checks}
    assert "theme_name_quality" in by_name
    # 碎片名("背景下福清…"标题中段切片)必须阻断,不再放行进确认面板
    assert by_name["theme_name_quality"].passed is False
    metrics = by_name["theme_name_quality"].metrics
    assert "背景下福清沙浦镇海产品内容营销策略研究" in metrics.get("unacceptable", [])
    assert "背景下福清沙浦镇海产品内容营销策略研究" in metrics.get("blocked", [])


# ---------- finalize 终检:碎片名触发证据重命名,不能原样过关 ----------

def test_finalize_replaces_mid_title_fragment():
    papers = [
        _paper(1, LEAKED_CN_TITLE),
        _paper(2, "生鲜电商背景下水产品营销渠道创新研究"),
        _paper(3, "水产品品牌建设与区域营销路径研究"),
    ]
    groups = [clf.Group(name="背景下福清沙浦镇海产品内容营销策略研究", lit_ids=[p.lit_id for p in papers])]
    out = clf._finalize_theme_groups(papers, groups, "水产品营销策略")
    assert out, "finalize 后必须仍有可交付主题"
    for g in out:
        assert clf._group_name_acceptable(g.name, pool_language="zh"), g.name
        assert not clf._looks_like_title_prefix(g.name, papers, g.lit_ids), g.name


# ---------- 中文主题名最低 6 字(第五起事故:"高邮大虾" 24 篇) ----------

def test_product_name_too_short_rejected():
    """单篇产品名作主题名(4 字)在策略层(轴规划 normalize)必须被拒。"""
    assert clf._zh_theme_name_too_short("高邮大虾", "zh")
    items = [{"name": "高邮大虾", "research_question": "高邮大虾品牌如何建设"}]
    assert clf._normalize_axes(items, 30, "zh") == []  # 过短轴被丢弃


def test_internal_acceptable_unchanged():
    """底层格式校验不承担长度策略:内部占位名契约保持原样。"""
    assert clf._group_name_acceptable("高邮大虾")


def test_legit_names_at_length_boundary():
    assert clf._group_name_acceptable("生鲜电商营销渠道", pool_language="zh")
    assert clf._group_name_acceptable("电子商务模式研究", pool_language="zh")


def test_en_pool_short_names_unaffected():
    assert clf._group_name_acceptable("Salmon marketing", pool_language="en")
