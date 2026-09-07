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


# ---------- 端到端:classify_by_theme(338 篇) ----------

class TestClassifyByThemeLargePool:
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

    def test_llm_total_failure_falls_back_to_single_group(self, monkeypatch):
        papers = _papers(338)

        def fake_create(**kwargs):
            raise RuntimeError("provider 全挂")

        monkeypatch.setattr(clf, "messages_create", fake_create)
        groups = clf.classify_by_theme(papers, "数字政府治理")
        assert len(groups) == 1
        assert len(groups[0].lit_ids) == 338
