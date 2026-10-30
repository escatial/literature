# -*- coding: utf-8 -*-
"""终检主题名收敛回归(2026-10-30 事故:高邮大虾)。

线上事故:归组修复后 classify_done 已产出合规分组(第 2 组「高邮大虾」
在 harness 的 theme_name_quality 是短名 → 仅告警放行,T04 语义),但
build_review_sections 终检对同一名字是硬 raise —— 上游放行、终检毙掉、
中间没有改名收敛,整场写作在最后一公里失败。这正是 min-6 教训
「检查必须配收敛出口」的再现。

修复 = 终检遇违规名(unacceptable / 中文短名 / 重名)先走既有证据改名链
(①确定性证据改名 _unique_evidence_group_name → ②LLM 批量改名
_llm_retitle_group_names)收敛,改名后复跑同一组检查;仍不合规才保留
原 raise,信息附上尝试记录。
"""
import pytest

import writing.classifier as clf
import writing.orchestrator as orch
from retrieval.types import Paper, Source


def _paper(i: int, title: str) -> Paper:
    return Paper(
        lit_id=f"lit_conv_{i:016x}",
        source=Source.CNKI,
        title=title,
        authors=[f"作者{i}"],
        journal="测试学报",
        year=2023,
        abstract=f"{title}。本文研究相关机制与策略问题,内容充实。",
    )


def _fixture():
    """第 1 组合规名;第 2 组违规短名「高邮大虾」,成员题录含该词。"""
    papers = [
        _paper(1, "小型水产企业市场营销策略探究"),
        _paper(2, "小型水产企业市场竞争力评价"),
        _paper(3, "高邮大虾品牌营销策略研究"),
        _paper(4, "高邮大虾区域消费市场调查"),
    ]
    groups = [
        clf.Group(
            name="小型水产企业市场营销策略探究",
            lit_ids=[papers[0].lit_id, papers[1].lit_id],
            research_question=None,
        ),
        clf.Group(
            name="高邮大虾",
            lit_ids=[papers[2].lit_id, papers[3].lit_id],
            research_question=None,
        ),
    ]
    return groups, papers


def test_terminal_check_converges_short_name_via_llm_rename(monkeypatch):
    """事故同款:确定性证据改名修不出(候选都是标题整段被拒),LLM 返回
    合规名 → 终检通过,组名被替换,sections 标题用新名。"""
    groups, papers = _fixture()
    calls = {"count": 0}

    def fake_create(**kwargs):
        calls["count"] += 1
        return '{"names": ["大虾品牌营销策略"]}'

    monkeypatch.setattr(clf, "messages_create", fake_create)
    sections = orch.build_review_sections(
        "theme", groups, "水产品营销策略", pool_language="zh", papers=papers,
    )
    assert calls["count"] == 1, "确定性改名失败后恰好一次 LLM 改名"
    assert groups[1].name == "大虾品牌营销策略", "组名同步替换"
    assert groups[0].name == "小型水产企业市场营销策略探究", "合规组不受影响"
    assert sections[0].title == "一、小型水产企业市场营销策略探究"
    assert sections[1].title == "二、大虾品牌营销策略"


def test_terminal_check_converges_via_deterministic_evidence_no_llm(monkeypatch):
    """成员题录含从句标题(标记词后取信息量更大的半边)→ 确定性证据改名
    直接收敛,零 LLM 成本。"""
    papers = [
        _paper(1, "小型水产企业市场营销策略探究"),
        _paper(2, "小型水产企业市场竞争力评价"),
        # 顺序敏感:候选按成员题录顺序生成,从句标题在前才能抢先命中
        _paper(3, "品牌化路径下大虾产业增值研究"),
        _paper(4, "高邮大虾"),
    ]
    groups = [
        clf.Group(
            name="小型水产企业市场营销策略探究",
            lit_ids=[papers[0].lit_id, papers[1].lit_id],
            research_question=None,
        ),
        clf.Group(
            name="高邮大虾",
            lit_ids=[papers[2].lit_id, papers[3].lit_id],
            research_question=None,
        ),
    ]
    calls = {"count": 0}

    def fake_create(**kwargs):
        calls["count"] += 1
        return '{"names": ["不应被调用"]}'

    monkeypatch.setattr(clf, "messages_create", fake_create)
    sections = orch.build_review_sections(
        "theme", groups, "水产品营销策略", pool_language="zh", papers=papers,
    )
    assert calls["count"] == 0, "确定性改名成功时不得烧 LLM"
    assert groups[1].name == "大虾产业增值"
    assert sections[1].title == "二、大虾产业增值"


def test_terminal_check_raises_with_attempts_when_convergence_fails(monkeypatch):
    """成员题录只有「高邮大虾」本名,确定性改名只能取回原词,LLM 也返回
    违规名 → 两次尝试全失败,保留原 raise 且信息附上尝试记录。"""
    papers = [
        _paper(1, "小型水产企业市场营销策略探究"),
        _paper(2, "小型水产企业市场竞争力评价"),
        _paper(3, "高邮大虾"),
        _paper(4, "高邮大虾"),
    ]
    groups = [
        clf.Group(
            name="小型水产企业市场营销策略探究",
            lit_ids=[papers[0].lit_id, papers[1].lit_id],
            research_question=None,
        ),
        clf.Group(
            name="高邮大虾",
            lit_ids=[papers[2].lit_id, papers[3].lit_id],
            research_question=None,
        ),
    ]

    def fake_create(**kwargs):
        return '{"names": ["高邮大虾"]}'

    monkeypatch.setattr(clf, "messages_create", fake_create)
    with pytest.raises(ValueError, match="证据改名尝试") as excinfo:
        orch.build_review_sections(
            "theme", groups, "水产品营销策略", pool_language="zh", papers=papers,
        )
    message = str(excinfo.value)
    assert "第 2 个主题名称不可靠" in message
    assert "高邮大虾" in message, "尝试过的改名要出现在报错信息里"
    assert groups[1].name == "高邮大虾", "收敛失败不替换组名"


def test_terminal_check_without_papers_keeps_direct_raise():
    """papers 缺省(None)时保留旧行为:违规名直接拦截,不做收敛。"""
    groups = [clf.Group(name="高邮大虾", lit_ids=[])]
    with pytest.raises(ValueError, match="主题名称不可靠"):
        orch.build_review_sections("theme", groups, "水产品营销策略", pool_language="zh")


def test_compliant_names_skip_convergence(monkeypatch):
    """合规名零收敛、零 LLM 调用(回归护栏:终检不得对合格名多做事)。"""
    groups, papers = _fixture()
    groups[1].name = "高邮大虾品牌营销策略研究"
    calls = {"count": 0}

    def fake_create(**kwargs):
        calls["count"] += 1
        return '{"names": ["不应被调用"]}'

    monkeypatch.setattr(clf, "messages_create", fake_create)
    sections = orch.build_review_sections(
        "theme", groups, "水产品营销策略", pool_language="zh", papers=papers,
    )
    assert calls["count"] == 0
    assert [s.title for s in sections[:2]] == [
        "一、小型水产企业市场营销策略探究",
        "二、高邮大虾品牌营销策略研究",
    ]


def test_terminal_check_llm_rename_passes_cross_lang_evidence_gate(monkeypatch):
    """今晚事故形态(第七轮前的最后一断点):82% 英文组(8 zh + 36 en)
    短名「高邮大虾」→ LLM 改出语义贴切的「可持续水产蛋白供给」,但该名
    与 8 条中文题录零子串 —— 名实粗检的跨语言豁免(同名语言占比
    8/44=18% < 30%)放行 → 终检通过且组名替换。"""
    def _en(i: int) -> Paper:
        return Paper(
            lit_id=f"lit_openalex_{i:016x}", source=Source.OPENALEX,
            title=f"Sustainable shrimp protein supply chain study {i}",
            authors=[f"Author {i}"], journal="Aquaculture", year=2023,
            abstract="This paper studies sustainable seafood protein supply.",
        )

    zh_titles = [
        "海产品区域品牌建设路径研究", "渔业产业化经营与农户增收机制分析",
        "品牌水产品消费行为调查研究", "水海产品贸易壁垒应对策略分析",
        "渔业合作社组织模式比较研究", "海产品加工业转型升级路径分析",
        "水产品牌溢价形成机制探讨", "远洋渔业资源养护政策评述",
    ]
    papers = [_en(i) for i in range(1, 37)]
    papers += [_paper(i, t) for i, t in enumerate(zh_titles, start=100)]
    groups = [
        clf.Group(
            name="小型水产企业市场营销策略探究",
            lit_ids=[papers[0].lit_id],
            research_question=None,
        ),
        clf.Group(
            name="高邮大虾",
            lit_ids=[p.lit_id for p in papers],
            research_question=None,
        ),
    ]
    calls = {"count": 0}

    def fake_create(**kwargs):
        calls["count"] += 1
        return '{"names": ["可持续水产蛋白供给"]}'

    monkeypatch.setattr(clf, "messages_create", fake_create)
    sections = orch.build_review_sections(
        "theme", groups, "水产品营销策略", pool_language="zh", papers=papers,
    )
    assert calls["count"] == 1
    assert groups[1].name == "可持续水产蛋白供给", "语义贴切的中文名不得被零重叠误杀"
    assert sections[1].title == "二、可持续水产蛋白供给"
