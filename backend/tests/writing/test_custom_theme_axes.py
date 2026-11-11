# -*- coding: utf-8 -*-
"""用户自定义主题模式测试(2026-11-11):预设主题直接归组。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2]
for p in (str(BACKEND_ROOT / "src"), str(BACKEND_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

import writing.classifier as clf  # noqa: E402


def _papers(n: int):
    papers = []
    for i in range(1, n + 1):
        papers.append(clf.Paper(
            lit_id=f"lit_x_{i}",
            source="user_imported",
            title=f"水产品研究{i}",
            authors=["张三"],
            journal="测试刊",
            year=2024,
            abstract=f"研究{i}:水产品营销策略分析。",
        ))
    return papers


def test_custom_axes_full_coverage(monkeypatch):
    """预设主题归组:全部文献覆盖、组名逐字使用用户主题。"""

    def fake_create(system, user, **kwargs):
        lines = [
            f"{i}. " + ("品牌建设" if i <= 20 else "数字渠道")
            for i in range(1, 31)
        ]
        return "\n".join(lines)

    monkeypatch.setattr(clf, "messages_create", fake_create)

    groups = clf.classify_with_custom_axes(
        _papers(30), "水产品营销策略", ["品牌建设", "数字渠道"],
    )

    assert len(groups) == 2
    covered = sum(len(g.lit_ids) for g in groups)
    assert covered == 30
    assert {g.name for g in groups} <= {"品牌建设", "数字渠道"}


def test_custom_axes_rejects_single_theme():
    """预设主题不足 2 个 → 直接拒绝。"""
    with pytest.raises(ValueError, match="至少需要 2 个"):
        clf.classify_with_custom_axes(_papers(10), "任意主题", ["唯一主题"])
