# -*- coding: utf-8 -*-
"""④名实粗检 与 ⑪小组吸收 回归(2026-10-27 T04)。

名实粗检:组名关键词与组内题名零重叠说明名实脱节 —— classifier 出口
(_retitle_groups_for_pool)先经证据改名链收敛,修不掉的保留原名并由
harness 名实告警通道提示(见 test_theme_quality_restore.py)。
小组吸收:小于 MIN_GROUP_PAPERS 的组并入标题证据最相近的组(不再直接
丢弃),循环 2 轮防御性重估,并后组数满足 _min_groups 下限,成员不丢。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

import writing.classifier as clf
from retrieval.types import Paper, Source


def _pool(titles: list[str], start: int = 1) -> list[Paper]:
    return [
        Paper(
            lit_id=f"lit_cnki_{i:016x}",
            source=Source.CNKI,
            title=title,
            authors=[f"作者{i}"],
            journal="水产经济",
            year=2022,
            abstract=f"本文研究{title[:10]}相关问题的机制与对策。",
        )
        for i, title in enumerate(titles, start=start)
    ]


def test_en_term_variants_plural_merging():
    """英文粗归并:ies→y、去尾 s;ss/us 结尾与短词不受误伤。"""
    assert clf._en_term_variants("topics") == {"topics", "topic"}
    assert clf._en_term_variants("studies") == {"studies", "study"}
    assert clf._en_term_variants("channels") == {"channels", "channel"}
    assert clf._en_term_variants("class") == {"class"}
    assert clf._en_term_variants("status") == {"status"}
    assert clf._en_term_variants("data") == {"data"}


def test_name_evidence_overlap_zh_and_en():
    """中文子串与英文变体词面都能建立名实关联;无关名判零重叠。"""
    papers = _pool(["水产品冷链物流配送网络优化研究", "生鲜冷链运输成本控制研究"])
    ids = [p.lit_id for p in papers]
    assert clf._name_evidence_overlap("冷链物流配送", papers, ids)
    # 英文:题名单数 / 组名复数,粗归并后应判有重叠
    papers_en = _pool(["Traceability of seafood supply chains", "Retail channel performance"])
    ids_en = [p.lit_id for p in papers_en]
    assert clf._name_evidence_overlap("Seafood traceability topics", papers_en, ids_en)
    # 零重叠:组名关键词与题名毫无交集
    assert not clf._name_evidence_overlap("品牌溢价机制", papers, ids)


def test_zero_overlap_group_renamed_via_llm_chain(monkeypatch):
    """零重叠组在 classifier 出口先收敛:证据名不可用(整段标题型文献)时
    走 LLM 改名链,且 LLM 新名必须通过名实重叠门槛才被接受。"""
    import json

    papers = _pool([
        "水产品冷链物流配送网络优化研究一", "水产品冷链物流配送网络优化研究二",
        "生鲜冷链运输成本控制策略研究", "冷链仓储温控技术应用研究",
        "冷链物流企业服务质量评价研究",
    ])
    ids = [p.lit_id for p in papers]
    groups = [clf.Group(name="品牌溢价机制", lit_ids=list(ids))]
    # 证据名对整段标题型文献产不出(候选全被标题复制检查打回,既有行为),
    # 走 LLM 批量改名;patch messages_create 返回与题名相关的合规新名。
    calls: list[dict] = []

    def fake_create(**kwargs):
        calls.append(kwargs)
        return json.dumps({"names": ["冷链物流配送网络优化"]}, ensure_ascii=False)

    monkeypatch.setattr(clf, "messages_create", fake_create)
    out = clf._retitle_groups_for_pool(papers, groups, "水产品营销策略")
    assert out, "改名链失败不允许丢组"
    assert calls, "零重叠组应触发 LLM 改名调用"
    assert out[0].name == "冷链物流配送网络优化"
    assert clf._name_evidence_overlap(out[0].name, papers, ids), "新名必须与题名重叠"


def test_zero_overlap_irrelevant_llm_name_rejected(monkeypatch):
    """LLM 给出的新名仍零重叠时拒收(名实门槛),保留原名交 harness 告警。"""
    import json

    papers = _pool([
        "水产品冷链物流配送网络优化研究一", "水产品冷链物流配送网络优化研究二",
        "生鲜冷链运输成本控制策略研究", "冷链仓储温控技术应用研究",
        "冷链物流企业服务质量评价研究",
    ])
    ids = [p.lit_id for p in papers]
    groups = [clf.Group(name="品牌溢价机制", lit_ids=list(ids))]

    def fake_create(**kwargs):
        return json.dumps({"names": ["消费者购买意愿"]}, ensure_ascii=False)

    monkeypatch.setattr(clf, "messages_create", fake_create)
    out = clf._retitle_groups_for_pool(papers, groups, "水产品营销策略")
    assert out and out[0].name == "品牌溢价机制", "仍零重叠的新名必须拒收"


def test_absorb_small_groups_merges_into_nearest():
    """孤篇组并入标题证据最相近的组,组数收窄、成员一篇不丢。"""
    cold = _pool([f"水产品冷链物流优化研究{i}" for i in range(5)])
    brand = _pool([f"水产品品牌溢价机制研究{i}" for i in range(5)], start=100)
    lone = _pool(["水产品冷链物流成本分析"], start=200)
    papers = cold + brand + lone
    groups = [
        clf.Group(name="冷链物流优化", lit_ids=[p.lit_id for p in cold]),
        clf.Group(name="品牌溢价机制", lit_ids=[p.lit_id for p in brand]),
        clf.Group(name="冷链成本补充", lit_ids=[p.lit_id for p in lone]),
    ]
    out = clf._absorb_small_groups(groups, papers)
    assert len(out) == 2
    assert sum(len(g.lit_ids) for g in out) == len(papers), "成员一篇不丢"
    cold_group = next(g for g in out if g.name == "冷链物流优化")
    assert lone[0].lit_id in cold_group.lit_ids, "孤篇应并入标题最相近的冷链组"


def test_absorb_respects_min_groups_floor():
    """并后组数将跌破 _min_groups 时保留小组(交确认面板处置)。"""
    papers = _pool([f"研究主题{i}的机制分析" for i in range(10)])
    groups = [
        clf.Group(name="主要研究主题", lit_ids=[p.lit_id for p in papers[:8]]),
        clf.Group(name="边缘补充主题", lit_ids=[papers[8].lit_id]),
        clf.Group(name="零散观察主题", lit_ids=[papers[9].lit_id]),
    ]
    # 10 篇池 _min_groups=2:吸收一个小组后剩 2 组恰在下限,第二个小组
    # 必须保留(吸收会跌破下限)。
    out = clf._absorb_small_groups(groups, papers)
    assert len(out) == 2
    assert sum(len(g.lit_ids) for g in out) == 10


def test_cross_language_group_passes_overlap_gate(monkeypatch):
    """跨语言豁免(锁 classifier._name_evidence_overlap 语义):zh 池的英文
    文献组配上 LLM 改出的中文名时,中英词面必然零重叠 —— 该场景必须保守
    放行(不判零重叠),命名质量由语言一致性链路负责;同语言零重叠仍拒绝。"""
    # zh 池:6 篇中文 + 2 篇英文(英文文献为一组,对应 T04 存量场景)
    papers = _pool([
        "水产品冷链物流配送网络优化研究一", "水产品冷链物流配送网络优化研究二",
        "生鲜冷链运输成本控制策略研究", "冷链仓储温控技术应用研究",
        "冷链物流企业服务质量评价研究", "水产品电子商务模式发展对策研究",
    ])
    papers += [
        Paper(
            lit_id=f"lit_openalex_{i:016x}", source=Source.OPENALEX,
            title=title, authors=[f"Author {i}"], journal="Aquaculture",
            year=2022, abstract="This paper studies seafood marketing questions.",
        )
        for i, title in enumerate([
            "Ecolabels and Sustainability in the Seafood Sector",
            "Consumer preferences for traceable seafood products",
        ], start=100)
    ]
    import json

    def fake_create(**kwargs):
        return json.dumps({"names": ["海产品电商营销渠道"]}, ensure_ascii=False)

    monkeypatch.setattr(clf, "messages_create", fake_create)
    groups = [clf.Group(
        name="Sustainability, resilience, and economic transformation in seafood systems",
        lit_ids=[papers[6].lit_id, papers[7].lit_id],
    )]
    out = clf._retitle_groups_for_pool(papers, groups, "水产品营销策略")
    assert out and out[0].name == "海产品电商营销渠道", (
        "跨语言组的中文名不得被零重叠门槛误杀"
    )
    # 函数级语义锁定:组名语言与全部题名不同 → 放行;同语言零重叠 → 拒绝
    en_ids = [papers[6].lit_id, papers[7].lit_id]
    assert clf._name_evidence_overlap("海产品电商营销渠道", papers, en_ids) is True
    assert clf._name_evidence_overlap("冷链物流优化", papers, en_ids) is True, (
        "同语言零重叠才判 False,跨语言放行不影响本判定"
    )


def test_absorb_below_min_papers_threshold():
    """MIN_GROUP_PAPERS 之外的篇数下限可通过参数覆盖(测试与策略调节)。"""
    papers = _pool([f"研究主题{i}的机制分析" for i in range(12)])
    groups = [
        clf.Group(name="主题甲甲", lit_ids=[p.lit_id for p in papers[:6]]),
        clf.Group(name="主题乙乙", lit_ids=[p.lit_id for p in papers[6:9]]),
        clf.Group(name="主题丙丙", lit_ids=[p.lit_id for p in papers[9:]]),
    ]
    out = clf._absorb_small_groups(groups, papers, min_papers=4)
    assert len(out) == 2, "3 篇组在 min_papers=4 下应被吸收"
    assert sum(len(g.lit_ids) for g in out) == 12
