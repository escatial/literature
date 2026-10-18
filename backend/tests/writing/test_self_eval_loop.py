# -*- coding: utf-8 -*-
"""正文写作自评闭环(write → LLM 自评 → 带反馈重写 → 再评)专项回归。

覆盖 agent 循环的关键判据:
- 质量达标 → 不重写(只发 1 次评估调用);
- 质量不达标 → 带具体改进指令重写,重写稿走完整引用过滤;
- 重写稿引用塌方(≥初稿一半)或非正文 → 拒绝,保留初稿;
- WRITING_SELF_EVAL=0 → 整个闭环关闭,零 LLM 调用。
"""
import json

import writing.section_writer as sw
from retrieval.types import Paper, Source


def _paper(i: int) -> Paper:
    return Paper(
        lit_id=f"lit_cnki_{i:016x}",
        source=Source.CNKI,
        title=f"研究问题场景{i % 3}的治理机制{i}",
        authors=[f"作者{i}"],
        journal="测试学报",
        year=2024,
        abstract=f"本文研究场景{i % 3}下的治理机制与协同问题。",
    )


def _result(content: str, lit_ids: list[str]) -> sw.SectionResult:
    return sw.SectionResult(
        key="theme_1", title="协同治理机制",
        content=content, citations=list(lit_ids),
    )


_DRAFT = (
    "现有研究围绕协同治理机制展开了较为充分的讨论。[lit_cnki_%016x] "
    "有学者比较了不同情境下的治理绩效差异,并提出了相应的解释框架,"
    "认为结构变量在其中扮演关键角色。[lit_cnki_%016x] "
    "不过这些研究大多停留在静态描述层面。"
) % (1, 2)

_ALLOWED = {f"lit_cnki_{i:016x}" for i in (1, 2, 3)}
_SPEC = sw.SectionSpec(key="theme_1", title="协同治理机制", instruction="述评")

_GOOD_EVAL = json.dumps({
    "scores": {"分析深度": 7, "逻辑结构": 7, "批判性": 6, "引用自然": 6, "语言规范": 7},
    "total": 33, "verdict": "合格", "issues": [], "rewrite_instructions": [],
}, ensure_ascii=False)

_BAD_EVAL = json.dumps({
    "scores": {"分析深度": 3, "逻辑结构": 4, "批判性": 2, "引用自然": 5, "语言规范": 5},
    "total": 19, "verdict": "需要改进",
    "issues": ["文献罗列多、比较少"],
    "rewrite_instructions": ["增加文献间观点对比", "补一节批判性评述"],
}, ensure_ascii=False)


def _patch(monkeypatch, eval_reply: str, rewrite_reply: str | None = None):
    calls: list[dict] = []

    def fake_create(**kwargs):
        calls.append(kwargs)
        if kwargs.get("system") == sw._SELF_EVAL_SYSTEM:
            return eval_reply
        return rewrite_reply or ""

    monkeypatch.setattr(sw, "messages_create", fake_create)
    return calls


def test_self_eval_skips_when_disabled(monkeypatch):
    """开关关闭 → 零 LLM 调用,原稿原样返回。"""
    monkeypatch.setenv("WRITING_SELF_EVAL", "0")
    calls = _patch(monkeypatch, _GOOD_EVAL)
    result = _result(_DRAFT, ["lit_cnki_%016x" % 1, "lit_cnki_%016x" % 2])
    out = sw._self_evaluate_and_improve(
        result, "治理", [_paper(i) for i in (1, 2, 3)], "sys", "usr",
        section=_SPEC, allowed=_ALLOWED, alias_map={}, is_comment=False,
    )
    assert out.content == result.content
    assert calls == []


def test_self_eval_pass_keeps_draft_without_rewrite(monkeypatch):
    """评估达标 → 不重写,只发 1 次评估调用。"""
    monkeypatch.setenv("WRITING_SELF_EVAL", "1")
    calls = _patch(monkeypatch, _GOOD_EVAL)
    result = _result(_DRAFT, ["lit_cnki_%016x" % 1, "lit_cnki_%016x" % 2])
    out = sw._self_evaluate_and_improve(
        result, "治理", [_paper(i) for i in (1, 2, 3)], "sys", "usr",
        section=_SPEC, allowed=_ALLOWED, alias_map={}, is_comment=False,
    )
    assert out.content == result.content
    assert len(calls) == 1


def test_self_eval_rewrite_accepted_with_feedback(monkeypatch):
    """评估不达标 → 带反馈重写;重写稿引用合法且保留 → 接受。"""
    monkeypatch.setenv("WRITING_SELF_EVAL", "1")
    improved = (
        "不同研究对协同治理机制的因果解释存在明显分歧:[lit_cnki_%016x] 强调结构"
        "因素的决定性作用,[lit_cnki_%016x] 则关注过程变量的动态演化,"
        "这一分歧至今尚未弥合,也为后续研究留下了空间。"
    ) % (1, 2)
    calls = _patch(monkeypatch, _BAD_EVAL, improved)
    result = _result(_DRAFT, ["lit_cnki_%016x" % 1, "lit_cnki_%016x" % 2])
    out = sw._self_evaluate_and_improve(
        result, "治理", [_paper(i) for i in (1, 2, 3)], "sys", "usr",
        section=_SPEC, allowed=_ALLOWED, alias_map={}, is_comment=False,
        max_rounds=1,
    )
    assert "分歧" in out.content
    assert out.citations == ["lit_cnki_%016x" % 1, "lit_cnki_%016x" % 2]
    # 1 次评估 + 1 次重写;重写提示词必须带上评估给出的具体指令
    assert len(calls) == 2
    rewrite_user = calls[1]["user"]
    assert "增加文献间观点对比" in rewrite_user


def test_self_eval_rewrites_are_filtered_to_allowed_citations(monkeypatch):
    """重写稿编造清单外的引用 → 被过滤,不得混入 citations。"""
    monkeypatch.setenv("WRITING_SELF_EVAL", "1")
    hallucinated = (
        "比较视角下的治理绩效差异显著。[lit_cnki_%016x] "
        "越界引用也会出现。[lit_cnki_%016x]"
    ) % (1, 999)
    calls = _patch(monkeypatch, _BAD_EVAL, hallucinated)
    result = _result(_DRAFT, ["lit_cnki_%016x" % 1, "lit_cnki_%016x" % 2])
    out = sw._self_evaluate_and_improve(
        result, "治理", [_paper(i) for i in (1, 2, 3)], "sys", "usr",
        section=_SPEC, allowed=_ALLOWED, alias_map={}, is_comment=False,
        max_rounds=1,
    )
    assert "lit_cnki_%016x" % 999 not in out.citations
    assert "lit_cnki_%016x" % 999 not in out.content


def test_self_eval_rejects_citation_collapsed_rewrite(monkeypatch):
    """重写稿引用塌方(少于初稿一半)→ 保留初稿,不接受退化。"""
    monkeypatch.setenv("WRITING_SELF_EVAL", "1")
    collapsed = "重写稿丢了所有引用,只剩光秃秃的论述,没有任何标注。"
    _patch(monkeypatch, _BAD_EVAL, collapsed)
    result = _result(_DRAFT, ["lit_cnki_%016x" % 1, "lit_cnki_%016x" % 2])
    out = sw._self_evaluate_and_improve(
        result, "治理", [_paper(i) for i in (1, 2, 3)], "sys", "usr",
        section=_SPEC, allowed=_ALLOWED, alias_map={}, is_comment=False,
        max_rounds=1,
    )
    assert out.content == result.content
    assert out.citations == result.citations
