# -*- coding: utf-8 -*-
"""RQ 输入贯通与 synthesis 写作流注入测试。"""
import json

import pytest

import writing.orchestrator as orch
from api.writing import PlanRequest, WritingRequest
from retrieval.types import Paper, Source
from writing.classifier import Group
from writing.orchestrator import (
    SectionResult,
    build_review_blueprint,
    generate_review,
    generate_review_stream,
)


def _paper(lit_id: str, abstract: str = "问卷调查发现X正向影响Y。") -> Paper:
    return Paper(
        lit_id=lit_id, source=Source.CNKI, title=f"研究{lit_id[-2:]}",
        authors=["张三"], journal="测试学报", year=2023, abstract=abstract,
    )


def _section_result(spec, content="张三(2023)指出X有效。", citations=None) -> SectionResult:
    return SectionResult(
        key=getattr(spec, "key", "theme_1"),
        title=getattr(spec, "title", "主题"),
        content=content,
        citations=citations or [],
        dropped_citations=[],
    )


# ---------- blueprint 存档 RQ ----------

def test_blueprint_archives_research_questions():
    papers = [_paper("lit_a"), _paper("lit_b"), _paper("lit_c")]
    groups = [Group(name="数字技术使用研究", lit_ids=["lit_a", "lit_b"])]
    bp = build_review_blueprint(
        "研究主题", papers, "theme", groups, [],
        research_questions=["RQ1: X如何影响Y?", "RQ2: 有哪些机制?"],
        review_goal="硕士论文第二章",
    )
    assert bp["research_questions"] == ["RQ1: X如何影响Y?", "RQ2: 有哪些机制?"]
    assert bp["review_goal"] == "硕士论文第二章"
    assert bp["research_question"] == "研究主题"   # 兼容字段保留


# ---------- API 请求模型 ----------

def test_writing_request_accepts_rq_fields():
    req = WritingRequest(
        topic="主题", papers=[], classify_mode="theme",
        research_questions=["RQ1"], review_goal="目标",
    )
    assert req.research_questions == ["RQ1"]
    plan = PlanRequest(topic="主题", papers=[], classify_mode="theme",
                       research_questions=["RQ1"])
    assert plan.research_questions == ["RQ1"]


# ---------- 写作流注入 ----------

def _run_stream_with_synthesis(monkeypatch, tmp_events):
    """跑 generate_review_stream(confirmed_groups),mock 综合与写作,收集 SSE。"""
    monkeypatch.setenv("WRITING_SYNTHESIS", "1")
    monkeypatch.setenv("WRITING_QA_ENABLED", "0")
    papers = [_paper("lit_a"), _paper("lit_b"), _paper("lit_c")]
    groups = [Group(name="数字技术使用研究", lit_ids=["lit_a", "lit_b"]),
              Group(name="技术焦虑机制", lit_ids=["lit_c"])]

    captured: dict = {}

    def fake_synthesis(topic, groups_, papers_, rqs):
        captured["rq_passed"] = rqs
        meta = {"succeeded": 2, "failed": 0, "failures": [],
                "counts": {"consensus": 2, "controversies": 1, "gaps": 1},
                "evidence_extracted": 3}
        return {"theme_1": {"consensus": [{"claim": "c", "lit_ids": ["lit_a"]}],
                            "controversies": [], "method_differences": [], "gaps": []},
                "theme_2": {"consensus": [], "controversies": [],
                            "method_differences": [], "gaps": []}}, meta

    monkeypatch.setattr(orch, "_run_theme_synthesis", fake_synthesis)
    # 重写路径会 import 真实 write_section 打真实 LLM;wiring 测试只验证
    # 流程接线,直接旁路(_retry_section_quality 的确定性行为已有专项覆盖)
    monkeypatch.setattr(orch, "_retry_section_quality",
                        lambda **kw: kw["result"])

    def fake_write_section_stream(spec, topic, groups_, section_papers,
                                  model=None, grades=None, synthesis_map=None,
                                  research_questions=None, review_goal=None, **kw):
        captured.setdefault("sections", []).append({
            "key": spec.key,
            "synthesis_keys": sorted((synthesis_map or {}).keys()),
            "rq": research_questions,
        })
        # theme_1 章应拿到本主题综合素材
        if spec.key == "theme_1":
            captured["theme1_has_synthesis"] = bool(
                (synthesis_map or {}).get("theme_1")
            )
        yield {"kind": "done", "result": _section_result(spec)}

    monkeypatch.setattr(orch, "write_section_stream", fake_write_section_stream)

    events = list(generate_review_stream(
        topic="研究主题", papers=papers, classify_mode="theme",
        do_screening=False, confirmed_groups=groups,
        research_questions=["RQ1"],
    ))
    return events, captured


def test_stream_runs_synthesis_and_delivers_flow_stats(monkeypatch):
    events, captured = _run_stream_with_synthesis(monkeypatch, None)
    text = "".join(events)
    assert '"synthesis_started"' in text
    assert '"synthesis_done"' in text
    assert '"flow_stats"' in text
    assert '"synthesis_themes": 2' in text
    assert '"themes": 2' in text
    # 综合素材传到了写作层,RQ 一路透传
    assert captured["theme1_has_synthesis"] is True
    assert captured["rq_passed"] == ["RQ1"]
    assert all(s["rq"] == ["RQ1"] for s in captured["sections"])


def test_stream_skips_synthesis_when_disabled(monkeypatch):
    monkeypatch.setenv("WRITING_SYNTHESIS", "0")
    monkeypatch.setenv("WRITING_QA_ENABLED", "0")
    papers = [_paper("lit_a"), _paper("lit_b"), _paper("lit_c")]
    groups = [Group(name="数字技术使用研究", lit_ids=["lit_a", "lit_b", "lit_c"])]

    def fail(*a, **k):
        raise AssertionError("synthesis 不应在关闭时执行")
    monkeypatch.setattr(orch, "_run_theme_synthesis", fail)

    monkeypatch.setattr(orch, "_retry_section_quality", lambda **kw: kw["result"])

    def fake_write_section_stream(spec, topic, groups_, section_papers, **kw):
        yield {"kind": "done", "result": _section_result(spec)}
    monkeypatch.setattr(orch, "write_section_stream", fake_write_section_stream)

    events = list(generate_review_stream(
        topic="研究主题", papers=papers, classify_mode="theme",
        do_screening=False, confirmed_groups=groups,
    ))
    text = "".join(events)
    assert "synthesis_started" not in text
    assert '"flow_stats"' in text
    assert '"synthesis_themes": 0' in text


# ---------- 过程透明化:思考/步骤事件 ----------

def test_stream_emits_reasoning_and_step_events(monkeypatch):
    """思考增量与步骤事件必须进 SSE;思考绝不混入正文 content。"""
    monkeypatch.setenv("WRITING_SYNTHESIS", "0")
    monkeypatch.setenv("WRITING_QA_ENABLED", "0")
    papers = [_paper("lit_a"), _paper("lit_b"), _paper("lit_c")]
    groups = [Group(name="数字技术使用研究", lit_ids=["lit_a", "lit_b", "lit_c"])]

    def fake_write_section_stream(spec, topic, groups_, section_papers, **kw):
        yield {"kind": "step", "step": "prepare_context", "status": "start",
               "message": "构建上下文"}
        yield {"kind": "reasoning", "delta": "先梳理文献脉络再下笔"}
        yield {"kind": "token", "delta": "张三(2023)指出X有效。"}
        yield {"kind": "done", "result": _section_result(spec)}
    monkeypatch.setattr(orch, "write_section_stream", fake_write_section_stream)
    monkeypatch.setattr(orch, "_retry_section_quality", lambda **kw: kw["result"])

    published: list[tuple[str, dict]] = []

    def fake_polish(result, papers_, *, progress=None):
        # 带外 progress:模拟润色闭环在生成器挂起期间上报步骤
        if progress is not None:
            progress("section_step", {"step": "humanize", "status": "ok",
                                      "message": "润色完成", "seconds": 1.2})
        return result
    monkeypatch.setattr(orch, "_polish_and_verify_section", fake_polish)

    events = list(generate_review_stream(
        topic="研究主题", papers=papers, classify_mode="theme",
        do_screening=False, confirmed_groups=groups,
        progress=lambda ev, data: published.append((ev, data)),
    ))
    text = "".join(events)
    assert '"section_step"' in text
    assert '"prepare_context"' in text
    assert '"section_reasoning"' in text
    assert "先梳理文献脉络再下笔" in text

    # 思考文本不得进入交付正文
    done_events = [e for e in events if '"section_done"' in e]
    assert done_events
    done_data = json.loads(done_events[0].removeprefix("data: "))
    assert "先梳理文献脉络再下笔" not in done_data["data"]["content"]

    # 带外上报的步骤带章节定位字段
    step_pubs = [d for ev, d in published if ev == "section_step"]
    assert any(d["step"] == "humanize" for d in step_pubs)
    assert all("key" in d and "index" in d for d in step_pubs)


def test_stream_degrades_when_synthesis_fails(monkeypatch):
    monkeypatch.setenv("WRITING_SYNTHESIS", "1")
    monkeypatch.setenv("WRITING_QA_ENABLED", "0")
    papers = [_paper("lit_a"), _paper("lit_b"), _paper("lit_c")]
    groups = [Group(name="数字技术使用研究", lit_ids=["lit_a", "lit_b", "lit_c"])]

    def boom(*a, **k):
        raise RuntimeError("LLM 不可用")
    monkeypatch.setattr(orch, "_run_theme_synthesis", boom)

    monkeypatch.setattr(orch, "_retry_section_quality", lambda **kw: kw["result"])

    def fake_write_section_stream(spec, topic, groups_, section_papers, **kw):
        yield {"kind": "done", "result": _section_result(spec)}
    monkeypatch.setattr(orch, "write_section_stream", fake_write_section_stream)

    events = list(generate_review_stream(
        topic="研究主题", papers=papers, classify_mode="theme",
        do_screening=False, confirmed_groups=groups,
    ))
    text = "".join(events)
    assert '"synthesis_skipped"' in text
    assert '"complete"' in text          # 降级不阻塞交付


def test_sync_generate_review_passes_synthesis(monkeypatch):
    monkeypatch.setenv("WRITING_SYNTHESIS", "1")
    monkeypatch.setenv("WRITING_QA_ENABLED", "0")
    papers = [_paper("lit_a"), _paper("lit_b"), _paper("lit_c")]
    groups = [Group(name="数字技术使用研究", lit_ids=["lit_a", "lit_b"]),
              Group(name="技术焦虑机制", lit_ids=["lit_c"])]
    seen: dict = {}

    def fake_synthesis(topic, groups_, papers_, rqs):
        seen["called"] = True
        return {}, {"succeeded": 0, "failed": 0, "failures": [], "counts": {}}
    monkeypatch.setattr(orch, "_run_theme_synthesis", fake_synthesis)
    monkeypatch.setattr(orch, "_retry_section_quality", lambda **kw: kw["result"])

    def fake_write_section(spec, topic, groups_, section_papers,
                           model=None, grades=None, synthesis_map=None,
                           research_questions=None, review_goal=None, **kw):
        seen["rq"] = research_questions
        return _section_result(spec)
    monkeypatch.setattr(orch, "write_section", fake_write_section)

    result = generate_review(
        topic="研究主题", papers=papers, classify_mode="theme",
        do_screening=False, confirmed_groups=groups,
        research_questions=["RQ1"], review_goal="目标",
    )
    assert seen["called"] is True
    assert seen["rq"] == ["RQ1"]
    assert result.sections
