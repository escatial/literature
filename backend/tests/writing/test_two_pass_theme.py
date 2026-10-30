# -*- coding: utf-8 -*-
"""两遍式主题划分(全局定轴 → 固定主题归组)专项回归。

覆盖旧分块法的三类线上病灶:
1. 轴规划产出占位名/泛化名 → 必须被判无效并重试,不能进确认面板;
2. 归组模型微调轴名措辞 → 按包含关系锚定回主题轴,不产生野组;
3. 轴方案留下孤篇组 → 吸收进最相近的组,不出现写不成章节的单篇主题。
"""
import json

from retrieval.types import Paper, Source
import writing.classifier as clf


def _paper(i: int) -> Paper:
    return Paper(
        lit_id=f"lit_cnki_{i:016x}",
        source=Source.CNKI,
        title=f"研究问题场景{i % 5}的治理机制{i}",
        authors=[f"作者{i}"],
        journal="测试学报",
        year=2024,
        abstract=f"本文研究场景{i % 5}下的治理机制与协同问题。",
    )


def _papers(n: int) -> list[Paper]:
    return [_paper(i) for i in range(1, n + 1)]


def _axis_reply(names):
    return json.dumps({"themes": [
        {"name": name, "research_question": f"如何研究{name}的核心机制"}
        for name in names
    ]}, ensure_ascii=False)


def _assign_reply(papers, names, rename=None):
    """按轴均分全部文献;rename 提供模型对某轴的「措辞漂移」名。"""
    buckets = [[] for _ in names]
    for i in range(1, len(papers) + 1):
        buckets[(i - 1) % len(names)].append(i)
    out = []
    for k, ids in enumerate(buckets):
        out.append({"name": rename.get(k, names[k]) if rename else names[k], "ids": ids})
    return json.dumps({"groups": out}, ensure_ascii=False)


def test_axis_plan_retries_on_placeholder_names(monkeypatch):
    """第一遍输出占位主题名 → 判无效重试,第二次给出合格轴后成功。"""
    papers = _papers(40)
    calls: list[dict] = []

    def fake_create(**kwargs):
        calls.append(kwargs)
        body = (kwargs.get("system") or "") + "\n" + (kwargs.get("user") or "")
        if "轴名|研究问题" in body:
            if len(calls) == 1:
                return json.dumps({"themes": [
                    {"name": "主题一", "research_question": "占位名一"},
                    {"name": "其他", "research_question": "兜底名"},
                ]}, ensure_ascii=False)
            return _axis_reply(["协同治理机制", "数字化转型路径"])
        return _assign_reply(papers, ["协同治理机制", "数字化转型路径"])

    monkeypatch.setattr(clf, "messages_create", fake_create)
    groups = clf.classify_by_theme(papers, "任意研究主题")
    assert [g.name for g in groups] == ["协同治理机制", "数字化转型路径"]
    covered = {lid for g in groups for lid in g.lit_ids}
    assert covered == {p.lit_id for p in papers}


def test_axis_plan_raises_when_all_attempts_invalid(monkeypatch):
    """两遍都给不出合格主题轴 → 抛错交给 harness 升级,不产伪主题。"""
    papers = _papers(40)

    def fake_create(**kwargs):
        body = (kwargs.get("system") or "") + "\n" + (kwargs.get("user") or "")
        if "轴名|研究问题" in body:
            return json.dumps({"themes": [
                {"name": "研究方向1", "research_question": "占位"},
            ]}, ensure_ascii=False)
        raise AssertionError("轴规划失败后不应进入归组")

    monkeypatch.setattr(clf, "messages_create", fake_create)
    import pytest
    with pytest.raises(ValueError):
        clf.classify_by_theme(papers, "任意研究主题")


def test_assign_name_drift_anchors_back_to_axis(monkeypatch):
    """归组模型把轴名「数字化转型路径」写成「数字化转型路径研究」
    → 包含关系锚定回原轴,确认面板不出现措辞漂移的野组。"""
    papers = _papers(40)
    names = ["协同治理机制", "数字化转型路径"]

    def fake_create(**kwargs):
        body = (kwargs.get("system") or "") + "\n" + (kwargs.get("user") or "")
        if "轴名|研究问题" in body:
            return _axis_reply(names)
        return _assign_reply(papers, names, rename={1: "数字化转型路径研究"})

    monkeypatch.setattr(clf, "messages_create", fake_create)
    groups = clf.classify_by_theme(papers, "任意研究主题")
    assert [g.name for g in groups] == names
    covered = {lid for g in groups for lid in g.lit_ids}
    assert covered == {p.lit_id for p in papers}


def test_assign_wild_group_papers_recovered_by_orphan_reassign(monkeypatch):
    """归组模型发明完全脱离主题轴的新组 → 该组作废,文献按相似度回收。"""
    papers = _papers(60)
    names = ["协同治理机制", "数字化转型路径"]

    def fake_create(**kwargs):
        body = (kwargs.get("system") or "") + "\n" + (kwargs.get("user") or "")
        if "轴名|研究问题" in body:
            return _axis_reply(names)
        return json.dumps({"groups": [
            {"name": names[0], "ids": list(range(1, 30))},
            {"name": names[1], "ids": list(range(30, 59))},
            {"name": "凭空发明的野主题", "ids": [59, 60]},
        ]}, ensure_ascii=False)

    monkeypatch.setattr(clf, "messages_create", fake_create)
    groups = clf.classify_by_theme(papers, "任意研究主题")
    assert [g.name for g in groups] == names
    covered = {lid for g in groups for lid in g.lit_ids}
    assert covered == {p.lit_id for p in papers}


def test_singleton_axis_group_absorbed_into_neighbor(monkeypatch):
    """归组后某轴只剩 1 篇 → 吸收进相邻组,不留孤篇主题。"""
    papers = _papers(60)
    names = ["协同治理机制", "数字化转型路径", "绩效评估体系"]

    def fake_create(**kwargs):
        body = (kwargs.get("system") or "") + "\n" + (kwargs.get("user") or "")
        if "轴名|研究问题" in body:
            return _axis_reply(names)
        return json.dumps({"groups": [
            {"name": names[0], "ids": list(range(1, 30))},
            {"name": names[1], "ids": list(range(30, 60))},
            {"name": names[2], "ids": [60]},
        ]}, ensure_ascii=False)

    monkeypatch.setattr(clf, "messages_create", fake_create)
    groups = clf.classify_by_theme(papers, "任意研究主题")
    assert all(len(g.lit_ids) >= 2 for g in groups), (
        f"不允许孤篇主题: {[(g.name, len(g.lit_ids)) for g in groups]}"
    )
    covered = {lid for g in groups for lid in g.lit_ids}
    assert covered == {p.lit_id for p in papers}


def test_axis_research_question_flows_into_final_groups(monkeypatch):
    """主题轴的一句话研究问题应锚定到最终分组(供前端展示)。"""
    papers = _papers(40)
    names = ["协同治理机制", "数字化转型路径"]

    def fake_create(**kwargs):
        body = (kwargs.get("system") or "") + "\n" + (kwargs.get("user") or "")
        if "轴名|研究问题" in body:
            return _axis_reply(names)
        return _assign_reply(papers, names)

    monkeypatch.setattr(clf, "messages_create", fake_create)
    groups = clf.classify_by_theme(papers, "任意研究主题")
    for group in groups:
        assert group.research_question == f"如何研究{group.name}的核心机制"
