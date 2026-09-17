# -*- coding: utf-8 -*-
"""主题划分大池子回归测试。

背景(338 篇池子线上事故):模型被要求在输出里回显全部 lit_id
(lit_cnki_+16 位十六进制 ≈ 10-18 token/个),max_tokens=5000 必然截断,
三次重试全废 → 降级单组兜底。修复后:
- 编号协议:模型只回显行首编号(数字),输出体积小一个数量级;
- token 预算随池子规模缩放;
- 截断输出可抢救:完整的分组对象保留,缺组文献按标题相似度归组。
"""
import json

import pytest

from retrieval.types import Paper, Source
import writing.classifier as clf


def _make_paper(i: int, title_prefix: str = "数字政府治理") -> Paper:
    return Paper(
        lit_id=f"lit_cnki_{i:016x}",
        source=Source.CNKI,
        title=f"{title_prefix}研究之维度{i % 4}视角下的方法{i}",
        authors=[f"作者{i}"],
        journal="测试学报",
        year=2020 + (i % 5),
        abstract=f"本文研究{title_prefix}的第{i % 4}个维度,采用方法{i}进行分析。",
    )


def _papers(n: int) -> list[Paper]:
    return [_make_paper(i) for i in range(1, n + 1)]


def test_language_rebalance_keeps_each_feasible_theme_bilingual():
    papers = _papers(4)
    for i in range(4, 8):
        papers.append(Paper(
            lit_id=f"lit_openalex_{i:016x}", source=Source.OPENALEX,
            title=f"Digital governance optimization model {i}", authors=[f"Smith {i}"],
            journal="Test Journal", year=2024,
            abstract="This study examines digital governance optimization and coordination.",
        ))
    groups = [
        clf.Group("协同治理机制", [p.lit_id for p in papers[:4]]),
        clf.Group("治理绩效优化", [p.lit_id for p in papers[4:]]),
    ]
    clf._rebalance_language_groups(groups, papers)
    assert clf._groups_language_balanced(groups, papers)
    by_id = {p.lit_id: p for p in papers}
    for group in groups:
        sources = {by_id[lid].source for lid in group.lit_ids}
        assert Source.CNKI in sources
        assert Source.OPENALEX in sources


# ---------- _output_token_budget ----------

def test_token_budget_floor_and_scaling():
    assert clf._output_token_budget(_papers(10)) == 5000
    # 1000 篇:1500 + 6000 = 7500
    assert clf._output_token_budget(_papers(1000)) == 7500
    # 硬顶 32000,不给 provider 拒绝的机会
    assert clf._output_token_budget(_papers(100000)) == 32000


# ---------- 截断抢救 ----------

def test_salvage_truncated_groups_recovers_complete_objects():
    raw = (
        '{"groups": [{"name": "协同治理机制", "ids": [1, 2, 3]}, '
        '{"name": "绩效评估方法", "ids": [4, 5'
    )  # 第二组写到一半被 max_tokens 截断
    items = clf._salvage_truncated_groups(raw)
    assert items is not None
    assert len(items) == 1
    assert items[0]["name"] == "协同治理机制"
    assert items[0]["ids"] == [1, 2, 3]


def test_salvage_truncated_groups_returns_none_when_nothing_complete():
    assert clf._salvage_truncated_groups('{"groups": [{"name": "截断') is None
    assert clf._salvage_truncated_groups("完全无关的文本") is None


def test_parse_group_response_salvages_truncation():
    raw = (
        "分析:本批文献可分为...\n"
        '{"groups": [{"name": "协同治理机制", "ids": [1, 2]}, '
        '{"name": "数字化转型路径", "ids": [3,'
    )
    items = clf._parse_group_response(raw)
    assert len(items) == 1
    assert items[0]["ids"] == [1, 2]


def test_parse_group_response_prefers_complete_json_over_salvage():
    raw = '{"groups": [{"name": "甲组", "ids": [1]}, {"name": "乙组", "ids": [2]}]}'
    items = clf._parse_group_response(raw)
    assert len(items) == 2


def test_parse_group_response_salvages_multiline_markdown_groups():
    raw = """**Group 1: 灾后道路损毁/路网中断下的协同路径规划**
- #1: 道路损毁
- #6: 道路中断

**Group 2: 公平性/紧迫度/满意度/人本关怀导向的配送优化**
- #4: 需求紧迫度
- #9: 满意度
"""
    items = clf._parse_group_response(raw)
    assert items == [
        {"name": "灾后道路损毁与路网中断", "ids": [1, 6]},
        {"name": "人本关怀导向的配送优化", "ids": [4, 9]},
    ]


# ---------- 编号映射 ----------

def test_map_group_items_maps_numeric_ids():
    papers = _papers(5)
    index_of = {str(i + 1): p.lit_id for i, p in enumerate(papers)}
    items = [{"name": "甲", "ids": [1, "2", 3]},
             {"name": "乙", "lit_ids": [papers[3].lit_id, papers[4].lit_id]}]
    mapped = clf._map_group_items(items, index_of)
    assert mapped[0]["lit_ids"] == [papers[0].lit_id, papers[1].lit_id, papers[2].lit_id]
    # 模型仍按旧格式回 lit_id 时原样保留
    assert mapped[1]["lit_ids"] == [papers[3].lit_id, papers[4].lit_id]


def test_map_group_items_drops_unknown_and_keeps_lit_prefix():
    index_of = {"1": "lit_cnki_aaaa"}
    mapped = clf._map_group_items(
        [{"name": "甲", "ids": [1, 99, "lit_cnki_bbbb", "垃圾"]}], index_of,
    )
    assert mapped[0]["lit_ids"] == ["lit_cnki_aaaa", "lit_cnki_bbbb"]


def test_group_name_rejects_english_stopword_fragments():
    assert not clf._group_name_acceptable("of与in与to")
    assert not clf._group_name_acceptable("this与learnin")
    assert not clf._group_name_acceptable("this与that与ch")


def test_group_name_rejects_placeholders_and_keyword_chains():
    assert not clf._group_name_acceptable("主题5")
    assert not clf._group_name_acceptable("主题方向六")
    assert not clf._group_name_acceptable("研究议题3")
    assert not clf._group_name_acceptable("无人机协同子主题2")
    assert not clf._group_name_acceptable("无人与人机与配送")
    assert not clf._group_name_acceptable("市区内卡车与无人机协同配方向")
    assert clf._group_name_acceptable("Hybrid Routing Models")


def test_clean_group_name_does_not_hardcode_domain_terms():
    raw = "领域甲参与乙与丙"
    assert clf._clean_group_name(raw) == raw


def test_build_groups_drops_duplicate_names():
    papers = _papers(3)
    valid = {p.lit_id for p in papers}
    groups, covered = clf._build_groups(
        [
            {"name": "无人机配送", "lit_ids": [papers[0].lit_id]},
            {"name": "无人机配送", "lit_ids": [papers[1].lit_id]},
            {"name": "协同调度", "lit_ids": [papers[2].lit_id]},
        ],
        valid,
    )
    assert [g.name for g in groups] == ["无人机配送", "协同调度"]
    assert papers[1].lit_id not in covered


# ---------- 端到端:classify_by_theme(338 篇) ----------

class TestClassifyByThemeLargePool:
    def test_local_cluster_name_model_has_bounded_timeout(self, monkeypatch):
        papers = _papers(120)
        calls: list[dict] = []

        def fake_create(**kwargs):
            calls.append(kwargs)
            return json.dumps({"names": [f"问题导向主题{i}" for i in range(1, 7)]}, ensure_ascii=False)

        monkeypatch.setattr(clf, "messages_create", fake_create)
        groups = clf._semantic_cluster_groups(papers, "任意研究主题")
        assert groups
        assert calls and calls[0]["timeout"] == 45.0
        covered = [lid for g in groups for lid in g.lit_ids]
        assert sorted(covered) == sorted(p.lit_id for p in papers)

    def test_normal_writing_pool_uses_one_cross_language_llm_classification(self, monkeypatch):
        papers = _papers(80)
        calls: list[dict] = []

        def fake_create(**kwargs):
            calls.append(kwargs)
            return self._mock_reply(papers)

        monkeypatch.setattr(clf, "messages_create", fake_create)
        groups = clf.classify_by_theme(papers, "任意研究主题")

        assert len(groups) == 4
        assert len(calls) == 1
        assert calls[0]["timeout"] == 75.0

    def test_normal_writing_pool_never_returns_title_truncation_fallback(self, monkeypatch):
        papers = _papers(80)

        def fail_create(**kwargs):
            raise RuntimeError("provider unavailable")

        monkeypatch.setattr(clf, "messages_create", fail_create)
        with pytest.raises(ValueError, match="不会再用截断标题或编号主题"):
            clf.classify_by_theme(papers, "任意研究主题")

    def _mock_reply(self, papers, n_groups=4):
        """构造覆盖全部 338 篇、按编号输出的合法分组 JSON。"""
        buckets: list[list[int]] = [[] for _ in range(n_groups)]
        for i in range(1, len(papers) + 1):
            buckets[(i - 1) % n_groups].append(i)
        names = ["协同治理机制", "绩效评估方法", "数字化转型路径", "公众参与研究"]
        payload = {
            "groups": [
                {"name": names[k], "ids": ids} for k, ids in enumerate(buckets)
            ]
        }
        return json.dumps(payload, ensure_ascii=False)

    def test_numeric_protocol_full_coverage(self, monkeypatch):
        papers = _papers(338)
        calls: list[dict] = []

        def fake_create(**kwargs):
            calls.append(kwargs)
            return self._mock_reply(papers)

        monkeypatch.setattr(clf, "messages_create", fake_create)
        groups = clf.classify_by_theme(papers, "数字政府治理")

        assert len(groups) == 4
        covered = [lid for g in groups for lid in g.lit_ids]
        assert sorted(covered) == sorted(p.lit_id for p in papers)  # 一篇不丢
        assert calls, "LLM 应至少被调用一次"
        # 编号协议落到了 prompt 上:输出格式说明用 ids,不是 lit_ids
        assert '"ids": [1, 2, 3]' in calls[0]["user"]

    def test_truncated_output_degrades_to_salvage_not_fallback(self, monkeypatch):
        """输出被截断:抢救完整分组 + 按标题相似度归并,绝不丢文献。"""
        papers = _papers(338)

        def fake_create(**kwargs):
            # 只完整写出 2 组,第三组截断;338 篇只覆盖 2/3
            bucket1 = list(range(1, 113))
            bucket2 = list(range(113, 226))
            return (
                '{"groups": ['
                '{"name": "协同治理机制", "ids": ' + json.dumps(bucket1) + "}, "
                '{"name": "绩效评估方法", "ids": ' + json.dumps(bucket2) + "}, "
                '{"name": "数字化转型路径", "ids": [226, 227, 22'
            )

        monkeypatch.setattr(clf, "messages_create", fake_create)
        groups = clf.classify_by_theme(papers, "数字政府治理")

        # 抢救出 2 个完整组,剩余 116 篇按标题相似度并入,总量一篇不丢
        assert len(groups) >= 2
        covered = [lid for g in groups for lid in g.lit_ids]
        assert sorted(covered) == sorted(p.lit_id for p in papers)

    def test_llm_total_failure_falls_back_to_deterministic_groups(self, monkeypatch):
        papers = _papers(338)

        def fake_create(**kwargs):
            raise RuntimeError("provider 全挂")

        monkeypatch.setattr(clf, "messages_create", fake_create)
        groups = clf.classify_by_theme(papers, "数字政府治理")
        assert len(groups) >= clf._min_groups(len(papers))
        covered = [lid for g in groups for lid in g.lit_ids]
        assert sorted(covered) == sorted(p.lit_id for p in papers)
