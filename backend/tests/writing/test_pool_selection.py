# -*- coding: utf-8 -*-
"""写作池配额精选回归(v11):核心期刊分层 × 相关性分级 × 语言引导线。

设计立场(用户 2026-10-21:不要写得太死,配额是引导线不是卡尺):
1. 池子 ≤ 软上限(目标 90 +10% = 99)时全量保留,91 篇不值得裁;
2. 明显超配额才裁到目标:英文先在 40% 进池线(90×40%=36)内取满,
   35 篇高质量英文不会被卡;中文核心期刊优先补齐;
3. 中文占比 60% 是英文引导带的补集,不是 2/3 硬约束;
4. 总量跌破下限 70 才允许英文越线回填(纯英文池可到 90);
5. method_rigor < 40 只在同等级内垫底,不跨级淘汰;
6. 裁剪明细随 relevance_report["quota_cut"] 透出(与 excluded_low_relevance 同构)。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

import core_journals
from retrieval.types import Paper, Source
from writing.pool_selection import select_writing_pool
from writing.relevance import RelevanceScore


def _zh(i: int, journal: str, core: bool = False) -> Paper:
    return Paper(
        lit_id=f"lit_cnki_{i:016x}",
        source=Source.CNKI,
        title=f"中文研究文献{i}",
        authors=[f"作者{i}"],
        journal=journal,
        year=2024,
        abstract=f"研究问题{i}的实证分析。",
        cited_by_count=i % 7,
    )


def _en(i: int, cited: int = 0, title: str | None = None) -> Paper:
    return Paper(
        lit_id=f"lit_oa_{i:016x}",
        source=Source.OPENALEX,
        title=title or f"English study {i}",
        authors=[f"Author {i}"],
        journal=f"Journal of Studies {i % 5}",
        year=2023,
        abstract=f"Study on topic {i}.",
        cited_by_count=cited,
    )


@pytest.fixture(autouse=True)
def _core_journal_env(monkeypatch):
    """核心期刊判定不依赖真实清单:期刊名含「核心」即核心,否则非核心。"""
    monkeypatch.setattr(
        core_journals,
        "classify_journal",
        lambda journal: {"in_core": "核心" in (journal or "")},
    )


def _scores(papers: list[Paper], total: float = 80.0, rigor: float | None = None) -> dict:
    return {
        p.lit_id: RelevanceScore(
            lit_id=p.lit_id,
            keyword_overlap=total,
            field_match=total,
            method_applicability=total,
            conclusion_value=total,
            method_rigor=rigor,
        )
        for p in papers
    }


def test_pool_within_target_selects_all():
    papers = [_zh(i, "普通期刊") for i in range(30)] + [_en(i) for i in range(20)]
    selected, cuts = select_writing_pool(papers, _scores(papers))
    assert len(selected) == 50
    assert cuts == []
    assert {p.lit_id for p in selected} == {p.lit_id for p in papers}


def test_soft_ceiling_keeps_small_excess():
    """软上限 99:91/99 篇的池子全量保留,不为凑整硬裁;100 篇才动刀。"""
    papers_99 = [_zh(i, "核心期刊") for i in range(60)] + [_en(i) for i in range(39)]
    selected, cuts = select_writing_pool(papers_99, _scores(papers_99))
    assert len(selected) == 99
    assert cuts == []

    papers_100 = [_zh(i, "核心期刊") for i in range(60)] + [_en(i) for i in range(40)]
    selected, cuts = select_writing_pool(papers_100, _scores(papers_100))
    assert len(selected) == 90
    assert len(cuts) == 10


def test_deep_pools_cut_to_target_with_english_share_line():
    """100 核心 + 60 非核心中文 + 100 英文:英文取满 36(40% 线),
    中文补齐到 54(40 核心 + 14 非核心)。"""
    papers = (
        [_zh(i, "核心期刊" if i < 40 else "普通期刊") for i in range(100)]
        + [_en(i) for i in range(100)]
    )
    selected, cuts = select_writing_pool(papers, _scores(papers))
    assert len(selected) == 90
    zh = [p for p in selected if p.source is Source.CNKI]
    en = [p for p in selected if p.source is Source.OPENALEX]
    assert len(en) == 36
    assert len(zh) == 54
    assert len(en) / 90 == pytest.approx(0.40)
    # 核心期刊全部入选,非核心只补 14 篇
    core_ids = {f"lit_cnki_{i:016x}" for i in range(40)}
    selected_ids = {p.lit_id for p in zh}
    assert core_ids <= selected_ids
    assert len(selected_ids - core_ids) == 14
    # 裁剪明细:非核心 46 篇 + 英文 64 篇,原因按层级注明
    assert len(cuts) == 110
    non_core_cuts = [c for c in cuts if c.core_tier == "non_core"]
    foreign_cuts = [c for c in cuts if c.core_tier == "foreign"]
    assert len(non_core_cuts) == 46
    assert len(foreign_cuts) == 64
    assert all("非核心" in c.reason for c in non_core_cuts)


def test_english_35_papers_pass_share_line():
    """用户场景:35 篇合格英文不被 30 的名额卡掉。

    70 篇核心中文 + 35 篇英文 = 105 > 99,裁到 90:35 篇英文全部入选
    (38.9%,落在 30%-40% 引导带内),只裁超线的中文。
    """
    papers = [_zh(i, "核心期刊") for i in range(70)] + [_en(i) for i in range(35)]
    selected, cuts = select_writing_pool(papers, _scores(papers))
    en = [p for p in selected if p.source is Source.OPENALEX]
    zh = [p for p in selected if p.source is Source.CNKI]
    assert len(selected) == 90
    assert len(en) == 35
    assert 0.30 <= len(en) / 90 <= 0.40
    assert len(zh) == 55
    assert all(c.core_tier == "core" for c in cuts)


def test_english_tiebreak_by_cited_count():
    """英文同等级同分时,被引数高者优先进入配额。

    64 篇中文 + 37 篇英文竞争 36 个英文名额:被引 99 的文献与 35 篇填充
    文献全部入选,被引 0 且标题排序垫底的文献被裁。
    """
    papers = [_zh(i, "核心期刊") for i in range(64)]
    filler = [_en(100 + i) for i in range(35)]
    strong = _en(1, cited=99, title="Aaa strongest evidence")
    weak = _en(2, cited=0, title="Zzz weakest signal")
    papers += filler + [strong, weak]
    selected, _ = select_writing_pool(papers, _scores(papers))
    selected_ids = {p.lit_id for p in selected}
    assert len(selected_ids) == 90
    assert strong.lit_id in selected_ids
    assert weak.lit_id not in selected_ids
    assert all(p.lit_id in selected_ids for p in filler)


def test_low_rigor_papers_sink_within_grade():
    """method_rigor < 40 只在同等级内垫底:低严谨文献让位给同分高严谨文献。

    53 篇普通核心 + 2 篇同分核心(严谨性 85 vs 20)竞争 54 个中文名额:
    高严谨文献拿走最后一个名额,低严谨文献被裁。
    """
    papers = [_zh(i, "核心期刊") for i in range(53)]
    sloppy = _zh(900, "核心期刊")
    solid = _zh(901, "核心期刊")
    papers += [sloppy, solid]
    papers += [_en(i) for i in range(45)]
    scores = _scores(papers)
    scores[sloppy.lit_id] = RelevanceScore(
        lit_id=sloppy.lit_id, keyword_overlap=80, field_match=80,
        method_applicability=80, conclusion_value=80, method_rigor=20,
    )
    scores[solid.lit_id] = RelevanceScore(
        lit_id=solid.lit_id, keyword_overlap=80, field_match=80,
        method_applicability=80, conclusion_value=80, method_rigor=85,
    )
    selected, cuts = select_writing_pool(papers, scores)
    selected_ids = {p.lit_id for p in selected}
    assert len(selected_ids) == 90
    assert solid.lit_id in selected_ids
    assert sloppy.lit_id not in selected_ids
    assert any(c.lit_id == sloppy.lit_id for c in cuts)


def test_short_chinese_pool_keeps_all_chinese_and_english_line():
    """中文只有 40 篇:中文全保留,英文取满 40% 线 36 篇,总量 76 仍高于下限。"""
    papers = [_zh(i, "核心期刊" if i < 10 else "普通期刊") for i in range(40)]
    papers += [_en(i) for i in range(100)]
    selected, cuts = select_writing_pool(papers, _scores(papers))
    zh = [p for p in selected if p.source is Source.CNKI]
    en = [p for p in selected if p.source is Source.OPENALEX]
    assert len(selected) == 76
    assert len(zh) == 40
    assert len(en) == 36
    assert len(cuts) == 64


def test_below_floor_backfills_english():
    """中文 20 篇且总量跌破下限 70:英文越 40% 线回填到下限(20+50=70)。"""
    papers = [_zh(i, "核心期刊") for i in range(20)] + [_en(i) for i in range(100)]
    selected, cuts = select_writing_pool(papers, _scores(papers))
    zh = [p for p in selected if p.source is Source.CNKI]
    en = [p for p in selected if p.source is Source.OPENALEX]
    assert len(selected) == 70
    assert len(zh) == 20
    assert len(en) == 50
    assert len(cuts) == 50


def test_unscored_papers_fall_back_to_neutral_order():
    """无评分数据时按中性顺序参与配额,不抛错、不丢文献。"""
    papers = [_zh(i, "核心期刊") for i in range(60)] + [_en(i) for i in range(40)]
    selected, cuts = select_writing_pool(papers, None)
    assert len(selected) == 90
    assert len(cuts) == 10


def test_apply_relevance_quota_attaches_quota_cut(monkeypatch):
    """_apply_relevance_quota 把裁剪明细挂 report["quota_cut"]。"""
    import writing.orchestrator as orch

    monkeypatch.setattr(orch, "build_concept_groups", lambda topic: None)

    kept = [_zh(i, "核心期刊" if i < 60 else "普通期刊") for i in range(100)]
    kept += [_en(i) for i in range(50)]
    decisions = [
        {
            "lit_id": p.lit_id,
            "relevant": True,
            "abstract_ok": True,
            "field_match": 85,
            "method_applicability": 80,
            "conclusion_value": 75,
            "method_rigor": 70,
            "reason": "直接相关",
        }
        for p in kept
    ]
    quota_papers, excluded, report = orch._apply_relevance_quota(
        "测试主题", kept, kept, decisions,
    )
    assert len(quota_papers) == 90
    cuts = report["quota_cut"]
    assert len(cuts) == 60
    row = cuts[0]
    assert {"lit_id", "title", "journal", "year", "language",
            "core_tier", "grade", "total", "reason"} <= set(row)
    # 英文取满 36(裁 14);中文补齐 54:60 篇核心竞争,6 篇靠标题序落选,
    # 40 篇非核心全裁
    tiers = [row_item["core_tier"] for row_item in cuts]
    assert tiers.count("core") == 6
    assert tiers.count("non_core") == 40
    assert tiers.count("foreign") == 14
    # excluded 语义不变:候选中落选者 = 60 篇裁剪
    assert len(excluded) == 60
