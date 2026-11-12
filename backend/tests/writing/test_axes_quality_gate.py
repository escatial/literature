# -*- coding: utf-8 -*-
"""定轴产物质量闸门测试(2026-11-11「可持续替代蛋白...」事故)。"""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
for p in (str(BACKEND_ROOT / "src"), str(BACKEND_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from writing.classifier import Group, _sanitize_axes_quality  # noqa: E402


def test_strips_truncation_dots():
    axes = [
        Group(name="可持续消费替代蛋白", lit_ids=[], research_question=""),
        Group(name="可持续替代蛋白...", lit_ids=[], research_question=""),
    ]
    out = _sanitize_axes_quality(axes, cap=6)
    # 近重复(相似度 ≥0.5)合并为一轴;合并保留更长的名字(剥掉点号后的)
    assert len(out) == 1
    assert "..." not in out[0].name


def test_merges_near_duplicate_axes():
    axes = [
        Group(name="电商数字化营销", lit_ids=[], research_question=" rq1"),
        Group(name="电商数字化营销策略", lit_ids=[], research_question=""),
    ]
    out = _sanitize_axes_quality(axes, cap=6)
    assert len(out) == 1
    # 保留更长的名字,研究问题并入
    assert out[0].name == "电商数字化营销策略"
    assert out[0].research_question == " rq1"


def test_enforces_cap_by_merging_smallest():
    axes = [
        Group(name="品牌建设传播", lit_ids=[], research_question=""),
        Group(name="电商渠道模式", lit_ids=[], research_question=""),
        Group(name="消费者购买行为", lit_ids=[], research_question=""),
        Group(name="石狮电商营销", lit_ids=[], research_question=""),
        Group(name="大连互联网营销", lit_ids=[], research_question=""),
        Group(name="社群营销策略", lit_ids=[], research_question=""),
        Group(name="出口国际营销", lit_ids=[], research_question=""),
        Group(name="技术赋能渔业", lit_ids=[], research_question=""),
    ]
    out = _sanitize_axes_quality(axes, cap=6)
    assert len(out) <= 6
    # 近重复合并优先,超上限最小轴并入最相似轴
    names = [g.name for g in out]
    assert len(names) == len(set(names))


def test_clean_axes_untouched():
    axes = [
        Group(name="品牌建设与传播", lit_ids=[], research_question="rq"),
        Group(name="电商渠道创新", lit_ids=[], research_question="rq2"),
    ]
    out = _sanitize_axes_quality(axes, cap=6)
    assert [(g.name, g.research_question) for g in out] == [
        ("品牌建设与传播", "rq"), ("电商渠道创新", "rq2"),
    ]
