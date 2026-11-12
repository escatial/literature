# -*- coding: utf-8 -*-
"""run 质量单合成测试(商业化可观测性底座)。"""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
for p in (str(BACKEND_ROOT / "src"), str(BACKEND_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

import pytest
from db.session import Base, reset_engine
from writing import job_store  # noqa: E402


@pytest.fixture()
def fresh_db(tmp_path):
    """独立临时库:测试不写真实 data/lit_review.db。"""
    reset_engine(f"sqlite:///{tmp_path / 'qr_test.db'}")
    import db.models  # noqa: F401
    Base.metadata.create_all(bind=reset_engine(f"sqlite:///{tmp_path / 'qr_test.db'}"))
    yield
    reset_engine("sqlite:///:memory:")


def _feed(job_id: str, seq_start: int, events: list[tuple[str, dict]]) -> None:
    for i, (name, payload) in enumerate(events, start=seq_start):
        job_store.append_event(job_id, i, name, payload)


def test_quality_report_full_pipeline(monkeypatch, fresh_db):
    """完整流水线事件 → 质量单各节齐全、数值正确。"""
    monkeypatch.setenv("WRITING_QA_ENABLED", "0")
    job_id = "job-qr-1"
    job_store.create_job(job_id, "usr_qr", "测试主题", "theme", None)

    _feed(job_id, 1, [
        ("start", {"topic": "测试主题", "phase": "planning"}),
        ("screening_started", {"total": 30}),
        ("screening_done", {"kept": 12, "screened_out": [f"x{i}" for i in range(18)], "quota_cut": 0}),
        ("relevance_report", {"total": 12}),
        ("classify_started", {"total": 12}),
        ("readthrough_started", {"papers": 12, "chunks": 1}),
        ("readthrough_chunk_done", {"done": 1, "total": 1, "notes": 12}),
        ("readthrough_done", {"papers": 12, "chunks": 1, "failed_chunks": 0, "notes": 12}),
        ("classify_progress", {"kind": "axis_plan_done", "themes": ["甲", "乙"]}),
        ("classify_progress", {"kind": "llm_result", "attempt": 1, "papers": 12,
                               "parsed_groups": 2, "valid_groups": 2, "covered": 12,
                               "decision": "accept"}),
        ("classify_done", {"groups": [{"name": "甲", "lit_ids": []}]}),
        ("plan_complete", {"phase": "await_confirm", "groups": []}),
    ])
    _feed(job_id, 100, [
        ("writing_started", {"phase": "writing", "total_sections": 3}),
        ("synthesis_started", {}),
        ("synthesis_done", {}),
        ("section_started", {"index": 0}),
        ("section_done", {"title": "一、甲", "content": "正文甲" * 50, "citations": ["a", "b"]}),
        ("section_done", {"title": "二、乙", "content": "正文乙" * 40, "citations": ["c"]}),
        ("section_done", {"title": "三、文献述评", "content": "述评" * 30, "citations": []}),
        ("qa_started", {"checks": []}),
        ("qa_done", {"overall": "pass", "pass_rate": 1.0, "auto_repair_count": 2}),
        ("complete", {"phase": "complete",
                      "manifest": {"anchor_leaks": 0, "missing_sections": [],
                                   "reference_count": 3}}),
    ])

    r = job_store.build_quality_report(job_id)
    assert r["status"] in ("running", "awaiting_confirm")
    assert r["total_events"] > 0
    assert r["screening"]["kept"] == 12
    assert r["screening"]["screened_out"] == 18
    assert r["readthrough"]["notes"] == 12
    assert r["grouping"]["axis_themes_planned"] == ["甲", "乙"]
    assert r["grouping"]["attempts"][0]["covered"] == 12
    assert r["grouping"]["attempts"][0]["decision"] == "accept"
    assert r["grouping"]["fallback_used"] is False
    assert len(r["writing_sections"]) == 3
    assert r["writing_sections"][0]["chars"] == len("正文甲" * 50)
    assert r["qa"]["overall"] == "pass"
    assert r["qa"]["auto_repair_count"] == 2
    assert r["manifest"]["anchor_leaks"] == 0
    assert r["errors"] == []
    # 阶段时间轴:start → screening_done 等相邻里程碑有秒数
    secs = [s.get("seconds") for s in r["stages"] if "seconds" in s]
    assert any(v is not None for v in secs)


def test_quality_report_records_errors_and_fallback(fresh_db):
    """失败运行:错误与降级路径如实入单(商业化排查依据)。"""
    job_id = "job-qr-2"
    job_store.create_job(job_id, "usr_qr", "主题", "theme", None)
    _feed(job_id, 1, [
        ("start", {"phase": "planning"}),
        ("classify_progress", {"kind": "llm_result", "attempt": 1, "papers": 90,
                               "parsed_groups": 8, "valid_groups": 8,
                               "covered": 9, "decision": "repair"}),
        ("classify_progress", {"kind": "assign_fallback_started", "message": "降级"}),
        ("error", {"message": "第 3 个主题名称不可靠"}),
    ])
    job_store.set_job_status(job_id, "failed", error="第 3 个主题名称不可靠")

    r = job_store.build_quality_report(job_id)
    assert r["status"] == "failed"
    assert r["grouping"]["attempts"][0]["covered"] == 9
    assert r["grouping"]["fallback_used"] is True
    assert r["errors"] == ["第 3 个主题名称不可靠"]
    job_store.stop_job(job_id)
