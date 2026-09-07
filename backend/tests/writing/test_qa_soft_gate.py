# -*- coding: utf-8 -*-
"""QA 软门禁回归测试。

历史事故(v9.6 及之前):QA 任一 FAIL → run_post_write_qa 抛 ValueError
→ 流式以 error 终止(无 complete)、同步接口 500 —— 整篇综述在全部章节
已写完、参考文献已生成之后被销毁,且 raise 路径连核查报告都不返回,
用户只看到一句笼统报错。常见误杀源:单篇元数据缺作者/年份、跨库同年同题
重复、在线优先出版年份超前、池子规模不达配额阈值(默认 total_min=70)。

v9.7 软门禁:FAIL 照常返回 payload,overall/issues 随 qa_done 事件与
complete.qa_report 交付前端,由用户决定是否采纳。
"""
import os

import pytest

import writing.orchestrator as orch
from retrieval.types import Paper, Source
from writing.orchestrator import SectionResult, _run_post_write_qa, generate_review


def _paper(lit_id: str, *, authors=None, year=2023) -> Paper:
    return Paper(
        lit_id=lit_id, source=Source.CNKI, title=f"治理研究{lit_id[-2:]}",
        authors=authors if authors is not None else ["张三"], journal="测试学报",
        year=year, abstract="摘要",
    )


def _section(key: str = "theme_1", content: str = "张三(2023)指出治理有效。") -> SectionResult:
    return SectionResult(
        key=key, title="治理机制", content=content,
        citations=["lit_cnki_aa01"], dropped_citations=[],
    )


# ---------- _run_post_write_qa 软门禁 ----------

def test_qa_fail_returns_report_instead_of_raising():
    """单篇缺作者 → accuracy FAIL:不抛错,payload 带回 overall=fail 与明细。"""
    bad = _paper("lit_cnki_aa01", authors=[])
    payload = _run_post_write_qa(
        papers=[bad], sections=[_section()], reference_list="[1] 张三. 治理研究01[J]. 测试学报, 2023.",
        grade_map={},
    )
    assert payload is not None
    summary = payload["summary"]
    assert summary["overall"] == "fail"
    codes = [i["code"] for i in summary["issues"]]
    assert "ACCURACY_MISSING_AUTHORS" in codes


def test_quota_thresholds_survive_ruleset_overrides(monkeypatch):
    """WRITING_QA_RULESET_OVERRIDES 一旦设置,quota 检查不得静默 SKIPPED。

    历史缺陷:to_dict()/asdict 把嵌套 quota 变 plain dict,重建 QARuleSet
    不还原 → check_literature_quota 访问 thresholds.total_min 抛
    AttributeError → 该项被记为 SKIPPED(数量合规核查静默失效)。
    """
    monkeypatch.setenv(
        "WRITING_QA_RULESET_OVERRIDES",
        '{"required_pass_rate": 0.9, "quota": {"total_min": 1}}',
    )
    papers = [_paper("lit_cnki_aa01"), _paper("lit_cnki_aa02")]
    payload = _run_post_write_qa(
        papers=papers, sections=[_section()], reference_list="[1] 张三. 治理研究01[J]. 测试学报, 2023.",
        grade_map={},
    )
    statuses = {r["check_id"]: r["status"] for r in payload["summary"]["results"]}
    assert statuses.get("quota_001") != "skipped", statuses


# ---------- 同步全链路:generate_review 在 QA FAIL 下照常返回 ----------

def test_generate_review_survives_qa_fail(monkeypatch):
    monkeypatch.setenv("WRITING_QA_ENABLED", "1")

    papers = [_paper("lit_cnki_aa01"), _paper("lit_cnki_aa02", year=2035)]  # 年份超前 → FAIL
    groups = [orch.Group(name="治理机制", lit_ids=[p.lit_id for p in papers])]

    def fake_write_section(spec, topic, groups_, section_papers, grades=None):
        return SectionResult(
            key=getattr(spec, "key", "theme_1"),
            title=getattr(spec, "title", "治理机制"),
            content="占位正文。",
            citations=[],
            dropped_citations=[],
        )

    monkeypatch.setattr(orch, "write_section", fake_write_section)
    result = generate_review(
        topic="基层治理", papers=papers, classify_mode="theme",
        confirmed_groups=groups, do_screening=False,
    )
    # QA FAIL 不再让同步接口抛 500:结果照常返回,报告随行
    assert result.sections, "章节必须照常交付"
    assert result.qa_report is not None
    assert result.qa_report["summary"]["overall"] == "fail"
    codes = [i["code"] for i in result.qa_report["summary"]["issues"]]
    assert "ACCURACY_FUTURE_YEAR" in codes
