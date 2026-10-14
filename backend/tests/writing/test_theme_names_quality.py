# -*- coding: utf-8 -*-
"""主题命名质量回归:确认面板暴露的四类坏名字。

线上案例(90 篇/水产品营销策略):
1. 中文主导文献池出现英文章节名("E-commerce and digital marketing channels…");
2. 主题名照抄单篇论文标题("我国水产品出口企业国际营销策略研究");
3. 个案研究副标题碎片("以乳山市为例");
4. 变相兜底桶("特定水产品综合研究")。
契约:
- _group_name_acceptable 整词拒绝 3/4 类;
- _looks_like_title_prefix 把"与某篇标题完全相同"也判为标题复制;
- _retitle_groups_for_pool 用全池主导语言统一重校,证据重命名优先,
  纯英文文献组走一次批量 LLM 改名,LLM 失败保留原名(结构优先)。
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from retrieval.types import Paper, Source
import writing.classifier as clf


def _paper(i: int, title: str, source: Source = Source.CNKI) -> Paper:
    return Paper(
        lit_id=f"lit_{source.value}_{i:016x}", source=source, title=title,
        authors=[f"作者{i}"], journal="水产经济", year=2024,
        abstract=f"本文研究{title}涉及的问题、方法与结论。",
    )


def test_case_study_suffix_and_catchall_names_rejected():
    assert not clf._group_name_acceptable("以乳山市为例")
    assert not clf._group_name_acceptable("以河北省沿海渔民为例")
    assert not clf._group_name_acceptable("特定水产品综合研究")
    assert not clf._group_name_acceptable("水产品市场概述")
    # 正常主题名不受影响
    assert clf._group_name_acceptable("疫情对水产品市场的影响")
    assert clf._group_name_acceptable("以旧换新政策对消费行为的作用")


def test_title_prefix_check_rejects_exact_title_copy():
    papers = [_paper(1, "我国水产品出口企业国际营销策略研究")]
    assert clf._looks_like_title_prefix(
        "我国水产品出口企业国际营销策略研究", papers, [papers[0].lit_id],
    )
    assert not clf._looks_like_title_prefix(
        "水产品出口企业国际营销策略", papers, [papers[0].lit_id],
    ), "泛化改写(去掉'我国'和'研究')不算标题复制"


def test_retitle_groups_enforces_pool_language(monkeypatch):
    cn_titles = [
        "我国海产品电子商务发展对策研究", "荣成市海产品O2O电子商务模式研究",
        "水产品网络营销中网络零售的服务整合研究", "生鲜电商背景下水产品营销渠道创新研究",
        "水产品品牌建设与区域营销路径研究", "水产批发市场的流通效率与整合策略研究",
    ]
    en_titles = [
        "Ecolabels and Sustainability in the Seafood Sector",
        "Emerging COVID-19 impacts and responses in the seafood system",
        "Consumer preferences for traceability information of seafood safety",
    ]
    papers = [_paper(i + 1, t) for i, t in enumerate(cn_titles)]
    papers += [
        _paper(100 + i, t, source=Source.OPENALEX) for i, t in enumerate(en_titles)
    ]
    assert clf._pool_dominant_language(papers) == "zh", "测试池应为中文主导"
    cn_ids = [p.lit_id for p in papers[:6]]
    en_ids = [p.lit_id for p in papers[6:]]
    groups = [
        clf.Group(name="E-commerce and digital marketing channels for aquatic products", lit_ids=cn_ids),
        clf.Group(name="Sustainability, resilience, and economic transformation in seafood systems", lit_ids=en_ids),
    ]
    llm_calls: list[dict] = []

    def fake_create(**kwargs):
        llm_calls.append(kwargs)
        return json.dumps({"names": ["海产品电商营销渠道", "海产品系统可持续性转型"]})

    monkeypatch.setattr(clf, "messages_create", fake_create)
    out = clf._retitle_groups_for_pool(papers, groups, "水产品营销策略")
    renamed = [g.name for g in out]
    assert all(clf._group_name_acceptable(n, pool_language="zh") for n in renamed), renamed
    assert "E-commerce" not in renamed[0] and "Sustainability" not in renamed[1]
    assert len(llm_calls) == 1, "批量改名只允许一次 LLM 调用"


def test_retitle_keeps_original_names_when_llm_down(monkeypatch):
    en_titles = [
        "Ecolabels and Sustainability in the Seafood Sector",
        "Emerging COVID-19 impacts and responses in the seafood system",
        "Consumer preferences for traceability information of seafood safety",
    ]
    papers = [
        _paper(100 + i, t, source=Source.OPENALEX) for i, t in enumerate(en_titles)
    ]
    groups = [clf.Group(
        name="Sustainability, resilience, and economic transformation in seafood systems",
        lit_ids=[p.lit_id for p in papers],
    )]

    def broken_create(**kwargs):
        raise RuntimeError("minimax 不可用")

    monkeypatch.setattr(clf, "messages_create", broken_create)
    out = clf._retitle_groups_for_pool(papers, groups, "水产品营销策略")
    assert len(out) == 1 and out[0].lit_ids == groups[0].lit_ids, "改名失败不允许丢组或丢文献"


def test_retitle_passes_when_names_already_conform(monkeypatch):
    papers = [_paper(i + 1, f"水产品品牌建设与区域营销研究{i}") for i in range(4)]
    groups = [clf.Group(name="水产品品牌建设与区域营销", lit_ids=[p.lit_id for p in papers])]

    def fail_create(**kwargs):
        raise AssertionError("名字全部合格时不应触发 LLM 改名")

    monkeypatch.setattr(clf, "messages_create", fail_create)
    out = clf._retitle_groups_for_pool(papers, groups, "水产品营销策略")
    assert [g.name for g in out] == ["水产品品牌建设与区域营销"]
