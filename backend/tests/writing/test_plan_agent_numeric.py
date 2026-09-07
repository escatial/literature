# -*- coding: utf-8 -*-
"""主题划分质检 agent 回归测试。

背景(338 篇池子线上事故):submit_final_groups 工具调用需在参数里回显
全部 lit_id(~4000+ token),而 max_tokens=2500 → 参数必然截断 →
json.loads 失败 → 4 轮耗尽 → 单兜底组无法被修复。修复后质检走编号协议。
"""
import json
from types import SimpleNamespace

from retrieval.types import Paper, Source
from agent import plan_agent
from agent.plan_agent import Group, _validate_submission, classify_agent_stream
from writing.classifier import _min_groups


def _make_paper(i: int) -> Paper:
    return Paper(
        lit_id=f"lit_cnki_{i:016x}",
        source=Source.CNKI,
        title=f"数字政府治理研究之维度{i % 4}视角",
        authors=[f"作者{i}"], journal="测试学报", year=2023,
        abstract="摘要节选",
    )


def _papers(n: int) -> list[Paper]:
    return [_make_paper(i) for i in range(1, n + 1)]


# ---------- _validate_submission(编号协议) ----------

def test_validate_submission_maps_numeric_ids():
    papers = _papers(8)  # <9 篇无组数下限,2 组即可
    initial = [Group(name="初版大组", lit_ids=[p.lit_id for p in papers])]
    groups, reason = _validate_submission(
        [
            {"name": "甲组", "ids": [1, 2, 3]},
            # 旧格式 lit_id 仍被接受(编号 4),其余走编号协议
            {"name": "乙组", "lit_ids": [papers[3].lit_id], },
            {"name": "丙组", "ids": [5, 6, 7, 8]},
        ],
        papers, initial,
        lid_of_idx={str(i + 1): p.lit_id for i, p in enumerate(papers)},
    )
    assert groups is not None, reason
    assert groups[0].lit_ids == [papers[0].lit_id, papers[1].lit_id, papers[2].lit_id]
    assert groups[1].lit_ids == [papers[3].lit_id]  # 旧格式 lit_id 兼容
    assert groups[2].lit_ids == [papers[4].lit_id, papers[5].lit_id,
                                 papers[6].lit_id, papers[7].lit_id]


def test_validate_submission_numeric_passes_min_groups_for_large_pool():
    papers = _papers(338)
    lid_of_idx = {str(i + 1): p.lit_id for i, p in enumerate(papers)}
    initial = [Group(name="初版大组", lit_ids=[p.lit_id for p in papers])]
    buckets = [list(range(k + 1, 339, 4)) for k in range(4)]  # 每组 ~84 篇
    submission = [
        {"name": name, "ids": ids}
        for name, ids in zip(
            ["协同治理机制", "绩效评估方法", "数字化转型路径", "公众参与研究"],
            buckets,
        )
    ]
    groups, reason = _validate_submission(
        submission, papers, initial, lid_of_idx=lid_of_idx,
    )
    assert groups is not None, reason
    assert len(groups) == _min_groups(338) == 4
    assert sum(len(g.lit_ids) for g in groups) == 338


def test_validate_submission_rejects_below_min_groups():
    papers = _papers(40)
    lid_of_idx = {str(i + 1): p.lit_id for i, p in enumerate(papers)}
    initial = [Group(name="初版大组", lit_ids=[p.lit_id for p in papers])]
    groups, reason = _validate_submission(
        [{"name": "甲组", "ids": list(range(1, 41))}], papers, initial,
        lid_of_idx=lid_of_idx,
    )
    assert groups is None
    assert "低于下限" in reason


# ---------- classify_agent_stream 端到端(编号协议修复单兜底组) ----------

def _tool_call_msg(name: str, args: dict) -> SimpleNamespace:
    return SimpleNamespace(
        content=None,
        tool_calls=[SimpleNamespace(
            id="call_0",
            function=SimpleNamespace(name=name, arguments=json.dumps(args, ensure_ascii=False)),
        )],
    )


def test_agent_resplits_fallback_group_with_numeric_ids(monkeypatch):
    """初版单兜底组(338 篇) → 质检 agent 用编号提交 4 组 → 修复成功。"""
    papers = _papers(338)
    initial = [Group(name="数字政府治理", lit_ids=[p.lit_id for p in papers])]

    seen_kwargs: list[dict] = []

    def fake_tools_create(messages, tools, max_tokens=2500, **kwargs):
        seen_kwargs.append({"max_tokens": max_tokens, "messages": messages})
        buckets = [list(range(k + 1, 339, 4)) for k in range(4)]
        submission = [
            {"name": name, "ids": ids}
            for name, ids in zip(
                ["协同治理机制", "绩效评估方法", "数字化转型路径", "公众参与研究"],
                buckets,
            )
        ]
        return _tool_call_msg("submit_final_groups", {"groups": submission, "note": "已拆分"})

    monkeypatch.setattr(plan_agent, "messages_create_with_tools", fake_tools_create)

    events: list[tuple[str, dict]] = []
    gen = classify_agent_stream("数字政府治理", papers, initial)
    while True:
        try:
            events.append(next(gen))
        except StopIteration as stop:
            final_groups, meta = stop.value
            break

    assert meta["checked"] is True
    assert len(final_groups) == 4
    covered = [lid for g in final_groups for lid in g.lit_ids]
    assert sorted(covered) == sorted(p.lit_id for p in papers)
    # 大池子下工具调用输出预算必须放大,不再固定 2500
    assert seen_kwargs[0]["max_tokens"] > 2500
    # 初始分组以编号序列化给模型(编号协议)
    first_user = seen_kwargs[0]["messages"][1]["content"]
    assert "编号" in first_user
    assert "1,2,3" in first_user  # lit_id 已换成短编号,不再回显 25 字符十六进制串
    assert "lit_cnki_" not in first_user
    kinds = [k for k, _ in events]
    assert "submit_ok" in kinds


def test_agent_exception_keeps_initial_groups(monkeypatch):
    papers = _papers(20)
    initial = [Group(name="初版组", lit_ids=[p.lit_id for p in papers])]

    def boom(*a, **k):
        raise RuntimeError("provider 挂了")

    monkeypatch.setattr(plan_agent, "messages_create_with_tools", boom)
    gen = classify_agent_stream("主题", papers, initial)
    while True:
        try:
            next(gen)
        except StopIteration as stop:
            final_groups, meta = stop.value
            break
    assert final_groups == initial
    assert meta["checked"] is False
