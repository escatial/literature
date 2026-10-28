# -*- coding: utf-8 -*-
"""通读笔记批(T02)端到端 stub 测试。

覆盖:
- 分类侧 e2e:乱序分块 → 逐块行协议笔记 → 笔记目录定轴(≤8) →
  固定轴归组 → trace 落盘(readthrough/axis_plan 两个 stage);
- 写作侧 e2e:节级通读 → 蒸馏笔记进 prompt → 正文经作者(年份)夹注
  回填落 [lit_xxx] 锚点;
- plan 流桥接:readthrough_done 紧凑笔记进 plan_complete payload;
- SSE 事件名路由:readthrough_* 升格为独立事件名,既有 kind 零改动;
- 降级路径:R1 败 → 原文观点卡直通、R1' 败 → 既有证据层;
- 开关:WRITING_READING_NOTES=0 时行为与现状完全一致。
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))
# tests/crawler/helpers.py 会在 pytest prepend 模式下抢占裸名 `helpers`,
# 故把 helpers 目录本身加进 path,直接导入 llm_stub 顶层模块(无撞名)。
sys.path.insert(0, str(Path(__file__).resolve().parent / "helpers"))

from api.writing import _classify_emit
import src.writing.classifier as clf
import src.writing.orchestrator as orch
import src.writing.readthrough as rt
import src.writing.section_writer as sw
from llm_stub import (
    assign_line_payload,
    axis_plan_payload,
    stub_llm_queue,
    stub_readthrough,
    stub_section_notes,
)
from retrieval.types import Paper, Source
from src.writing.templates import SectionSpec

AXES = ["数字营销渠道策略", "消费者行为机理", "品牌价值建设", "供应链协同治理"]
AUTHORS = ["王小明", "李航海", "张远航", "陈观澜"]


def _zh_pool(n: int, start: int = 1) -> list[Paper]:
    return [
        Paper(
            lit_id=f"lit_cnki_{i:016x}",
            source=Source.CNKI,
            title=f"水产品数字营销问题研究{i}",
            authors=[f"作者{i}"],
            journal="水产经济",
            year=2022,
            abstract=(
                f"本文研究水产品营销问题{i},采用实证方法检验渠道策略对企业绩效的"
                "影响机制,样本来自沿海城市,结论支持数字化转型路径。"
            ),
        )
        for i in range(start, start + n)
    ]


def _section_papers() -> list[Paper]:
    papers = []
    for i, name in enumerate(AUTHORS, start=1):
        papers.append(Paper(
            lit_id=f"lit_cnki_{i:016x}",
            source=Source.CNKI,
            title=f"渠道策略对企业绩效的影响研究{name}",
            authors=[name],
            journal="水产经济",
            year=2022,
            abstract=f"{name}研究渠道策略影响绩效的机制,实证支持数字化路径。",
        ))
    return papers


def test_classify_side_readthrough_e2e(monkeypatch, tmp_path):
    """分类侧全链:逐块笔记 → 笔记目录定轴 ≤8 → 归组 → trace 落盘。"""
    monkeypatch.setenv("LLM_TRACE_DIR", str(tmp_path / "trace"))
    monkeypatch.delenv("WRITING_READING_NOTES", raising=False)
    papers = _zh_pool(90)
    rt_calls = stub_readthrough(monkeypatch)
    clf_calls = stub_llm_queue(monkeypatch, clf, [
        axis_plan_payload(AXES),
        assign_line_payload(AXES, 90),
    ])

    groups = clf.classify_by_theme(papers, "水产品营销策略")

    assert groups and len(groups) <= 8, "轴数必须落在 _AXIS_MAX 内"
    assert {g.name for g in groups} <= set(AXES)
    covered = {lid for g in groups for lid in g.lit_ids}
    assert covered == {p.lit_id for p in papers}, "归组后全池文献一篇不丢"
    # R1:90 篇按 12 篇/块 → 8 块逐块通读;R2 一次定轴 + 一次归组
    assert len(rt_calls) == 8, f"应分 8 块通读,实际 {len(rt_calls)}"
    assert len(clf_calls) == 2
    # R2 定轴输入是笔记目录(带【通读笔记】段),不再是 24 万字符大文档
    r2_prompt = str(clf_calls[0].get("system") or "")
    assert "通读笔记" in r2_prompt and "题录目录" in r2_prompt
    # trace 落盘:readthrough 与 axis_plan 两个 stage 都有文件
    traces = sorted(p.name for p in (tmp_path / "trace").rglob("*.txt"))
    assert any(n.startswith("readthrough_") for n in traces), traces
    assert any(n.startswith("axis_plan_") for n in traces), traces


def test_classify_r1_failure_falls_back_to_raw_cards(monkeypatch, tmp_path):
    """R1 全败 → 该池原文观点卡直通 R2(无感降级,主链不中断)。"""
    monkeypatch.setenv("LLM_TRACE_DIR", str(tmp_path / "trace"))
    papers = _zh_pool(90)
    rt_calls = stub_readthrough(monkeypatch, fail=True)
    clf_calls = stub_llm_queue(monkeypatch, clf, [
        axis_plan_payload(AXES),
        assign_line_payload(AXES, 90),
    ])

    groups = clf.classify_by_theme(papers, "水产品营销策略")

    assert groups and len(groups) <= 8
    # R1 仍逐块尝试过(每块都失败)
    assert len(rt_calls) == 8
    # R2 目录降级为大文档观点卡形态,不再含笔记段
    r2_prompt = str(clf_calls[0].get("system") or "")
    assert "通读笔记" not in r2_prompt
    assert "认为：" in r2_prompt, "降级路径应回到原文观点卡大文档"


def test_env_off_disables_readthrough_entirely(monkeypatch, tmp_path):
    """WRITING_READING_NOTES=0:R1 完全不触发,行为与现状一致。"""
    monkeypatch.setenv("WRITING_READING_NOTES", "0")
    monkeypatch.setenv("LLM_TRACE_DIR", str(tmp_path / "trace"))
    papers = _zh_pool(90)

    def no_readthrough_llm(**kwargs):
        raise AssertionError("开关关闭时不应有任何通读 LLM 调用")

    monkeypatch.setattr(rt, "messages_create", no_readthrough_llm)
    clf_calls = stub_llm_queue(monkeypatch, clf, [
        axis_plan_payload(AXES),
        assign_line_payload(AXES, 90),
    ])

    groups = clf.classify_by_theme(papers, "水产品营销策略")
    assert groups and len(groups) <= 8
    assert len(clf_calls) == 2
    traces = [p.name for p in (tmp_path / "trace").rglob("*.txt")]
    assert not any(n.startswith("readthrough_") for n in traces), traces


def test_plan_stream_carries_readthrough_notes(monkeypatch):
    """plan 流桥接:readthrough_done 紧凑笔记进 plan_complete payload。"""
    papers = _zh_pool(30)
    groups = [
        clf.Group(name=AXES[i], lit_ids=[
            p.lit_id for j, p in enumerate(papers) if j % 4 == i
        ])
        for i in range(4)
    ]
    items = [
        {"lit_id": papers[0].lit_id, "author_year": "作者1（2022）",
         "question": "渠道如何影响绩效", "clue": "渠道管理"},
    ]
    emitted: list[dict] = []

    def fake_loop(topic, papers_, *, progress=None, check_stop=None):
        if progress:
            progress({"kind": "readthrough_started", "chunks": 3, "papers": 30})
            progress({
                "kind": "readthrough_done", "chunks": 3, "failed_chunks": 0,
                "notes": 1, "items": items,
            })
        return groups, {"status": "passed", "strategies": ["llm"], "issues": []}

    monkeypatch.setattr(orch, "_run_theme_classification_loop", fake_loop)

    def fake_screen(topic, papers_, flag):
        result = (list(papers_), [], {"quota_cut": []})

        def _gen():
            return result
            yield  # noqa: 让函数成为生成器,return 值经 StopIteration.value 带出

        return _gen()

    monkeypatch.setattr(orch, "_screen_papers_stream", fake_screen)

    chunks = list(orch.plan_review_stream(
        topic="水产品营销策略", papers=papers, classify_mode="theme",
        progress=emitted.append,
    ))
    plan_chunks = [c for c in chunks if '"plan_complete"' in c]
    assert plan_chunks, "必须产出 plan_complete 事件"
    data = json.loads(plan_chunks[0][len("data:"):].strip())["data"]
    assert data.get("readthrough_notes") == items, "payload 必须携带紧凑笔记"
    # 桥透传:classifier progress dict 原样到达 emit 通道(事件名由 api 层定)
    kinds = [d.get("kind") for d in emitted if isinstance(d, dict)]
    assert "readthrough_started" in kinds and "readthrough_done" in kinds


def test_api_emit_routes_readthrough_events():
    """SSE 事件名路由:readthrough_* 升格为独立事件,既有 kind 零改动。"""

    class _FakeStop:
        def is_set(self) -> bool:
            return False

    sent: list[tuple[str, dict]] = []
    emit = _classify_emit(_FakeStop(), lambda channel, payload: sent.append((channel, payload)))
    emit({"kind": "readthrough_started", "chunks": 3})
    emit({"kind": "readthrough_chunk_done", "done": 1, "total": 3})
    emit({"kind": "readthrough_done", "items": []})
    emit({"kind": "llm_attempt", "attempt": 1})
    events = [
        json.loads(payload[len("data:"):].strip())["event"]
        for _, payload in sent
    ]
    assert events == [
        "readthrough_started",
        "readthrough_chunk_done",
        "readthrough_done",
        "classify_progress",
    ]


def test_section_notes_e2e_with_anchor_injection(monkeypatch, tmp_path):
    """写作侧 e2e:节级通读 → 笔记进 prompt → 正文夹注回填 [lit_xxx]。"""
    monkeypatch.setenv("LLM_TRACE_DIR", str(tmp_path / "trace"))
    monkeypatch.delenv("WRITING_READING_NOTES", raising=False)
    papers = _section_papers()
    grades = {p.lit_id: "high" for p in papers}
    rt_calls = stub_section_notes(monkeypatch)
    events: list[tuple[str, dict]] = []
    spec = SectionSpec(key="theme_1", title="数字营销渠道策略", instruction="")

    block = orch._prepare_section_notes(
        spec, papers, "水产品营销策略", grades, progress=lambda e, d: events.append((e, d)),
    )

    assert block, "通读笔记块必须产出"
    assert "王小明（2022）" in block, "笔记行必须携带与文献一致的作者(年份)"
    assert "section_notes_done" in [e for e, _ in events]
    assert len(rt_calls) == 1, "节级通读只发一次 LLM 调用"
    assert any(n.startswith("section_notes_") for n in
               (p.name for p in (tmp_path / "trace").rglob("*.txt")))

    # 笔记作为蒸馏证据层进 section prompt
    system, _user, allowed, _alias, is_comment = sw._prepare_section_context(
        spec, "水产品营销策略",
        [clf.Group(name="数字营销渠道策略", lit_ids=[p.lit_id for p in papers])],
        papers, grades, notes_block=block,
    )
    assert "通读笔记" in system and "王小明（2022）" in system

    # 正文流式写作:夹注经既有机制回填 [lit_xxx] 锚点
    groups = [clf.Group(name="数字营销渠道策略", lit_ids=[p.lit_id for p in papers])]
    stream_events = list(sw.write_section_stream(
        spec, "水产品营销策略", groups, papers, grades=grades, notes_block=block,
    ))
    done = [e for e in stream_events if e.get("kind") == "done"]
    assert done, "流式写作必须以 done 事件收尾"
    result = done[0]["result"]
    assert "[lit_" in result.content, "正文必须含真实锚点"
    assert len(result.citations) >= 3, "引用密度须达标(4 篇节 ≥3),否则会触发重写"


def test_section_notes_failure_falls_back(monkeypatch, tmp_path):
    """R1' 失败 → 返回 None + section_notes_done{notes: False} 降级。"""
    monkeypatch.setenv("LLM_TRACE_DIR", str(tmp_path / "trace"))
    papers = _section_papers()
    stub_section_notes(monkeypatch, fail=True)
    events: list[tuple[str, dict]] = []
    spec = SectionSpec(key="theme_1", title="数字营销渠道策略", instruction="")

    block = orch._prepare_section_notes(
        spec, papers, "水产品营销策略",
        {p.lit_id: "high" for p in papers},
        progress=lambda e, d: events.append((e, d)),
    )

    assert block is None, "失败必须返回 None(无感降级)"
    done = [d for e, d in events if e == "section_notes_done"]
    assert done and done[0].get("notes") is False


def test_section_notes_env_off(monkeypatch):
    """开关关闭:R1' 不触发、不发事件,行为与现状一致。"""
    monkeypatch.setenv("WRITING_READING_NOTES", "0")
    papers = _section_papers()

    def no_llm(**kwargs):
        raise AssertionError("开关关闭时不应有通读 LLM 调用")

    monkeypatch.setattr(rt, "messages_create", no_llm)
    events: list[tuple[str, dict]] = []
    spec = SectionSpec(key="theme_1", title="数字营销渠道策略", instruction="")

    block = orch._prepare_section_notes(
        spec, papers, "水产品营销策略", None,
        progress=lambda e, d: events.append((e, d)),
    )

    assert block is None
    assert events == [], "关闭开关时不发布 section_notes_done"


def test_sync_section_notes_matches_stream_behavior(monkeypatch, tmp_path):
    """T03:同步链与流式链行为一致 —— write_section 前通读,笔记进 prompt,
    正文经夹注回填落 [lit_xxx] 锚点;同步路径不发布事件。"""
    monkeypatch.setenv("LLM_TRACE_DIR", str(tmp_path / "trace"))
    monkeypatch.delenv("WRITING_READING_NOTES", raising=False)
    papers = _section_papers()
    grades = {p.lit_id: "high" for p in papers}
    rt_calls = stub_section_notes(monkeypatch)
    spec = SectionSpec(key="theme_1", title="数字营销渠道策略", instruction="")
    events: list[tuple[str, dict]] = []

    block = orch._prepare_section_notes(
        spec, papers, "水产品营销策略", grades,
        progress=lambda e, d: events.append((e, d)),
    )
    assert block and "王小明（2022）" in block

    groups = [clf.Group(name="数字营销渠道策略", lit_ids=[p.lit_id for p in papers])]
    result = sw.write_section(
        spec, "水产品营销策略", groups, papers,
        grades=grades, notes_block=block,
    )
    assert "[lit_" in result.content, "同步链正文同样必须落真实锚点"
    assert len(result.citations) >= 3
    assert len(rt_calls) == 1, "同步链节级通读同样只发一次 LLM 调用"


def test_shuffle_is_deterministic_and_identity_stable():
    """乱序确定性(md5 seed)且不换 identity —— 池顺序口径不动的前提。"""
    papers = _zh_pool(20)
    a = rt.deterministic_shuffle(papers, "水产品营销")
    b = rt.deterministic_shuffle(papers, "水产品营销")
    assert [p.lit_id for p in a] == [p.lit_id for p in b]
    assert {id(p) for p in a} == {id(p) for p in papers}, "仅排序,不复制对象"
    assert sorted(p.lit_id for p in a) == sorted(p.lit_id for p in papers)


def test_chunk_respects_dual_limits():
    """分块双上限:篇数与字符任一触顶即开新块,文献一篇不丢。"""
    papers = _zh_pool(30)
    chunks = rt.chunk_for_readthrough(papers, max_papers=12, max_chars=10 ** 9)
    assert [len(c) for c in chunks] == [12, 12, 6]
    assert sum(len(c) for c in chunks) == 30
    tiny = rt.chunk_for_readthrough(papers, max_papers=100, max_chars=100)
    assert all(len(c) == 1 for c in tiny), "单篇超字符上限时独占成块"
    assert sum(len(c) for c in tiny) == 30
