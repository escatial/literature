# -*- coding: utf-8 -*-
"""⑦theme_name_quality 恢复真实检查回归(2026-10-27 T04)。

两级语义:
- _group_name_acceptable 失败(碎片/占位/语言不符) → 阻断(ok=False);
- _zh_theme_name_too_short → 仅告警(min-6 教训:短名不阻断);
- local_fallback 策略豁免阻断(兜底稿不能全链 blocked);
- ④名实粗检修不掉的零重叠组 → 告警保留。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from agent.harness import inspect_theme_groups
from retrieval.types import Paper, Source
from writing.classifier import Group


def _pool(n: int, title: str = "水产品冷链物流优化研究") -> list[Paper]:
    return [
        Paper(
            lit_id=f"lit_cnki_{i:016x}",
            source=Source.CNKI,
            title=f"{title}{i}",
            authors=[f"作者{i}"],
            journal="水产经济",
            year=2022,
            abstract=f"本文研究{title}相关问题的机制与对策。",
        )
        for i in range(1, n + 1)
    ]


def _check(groups: list[Group], papers: list[Paper], strategy: str | None = None):
    checks = inspect_theme_groups(groups, papers, strategy=strategy)
    by_name = {c.name: c for c in checks}
    return by_name["theme_name_quality"]


def test_english_name_blocks_in_zh_pool():
    """中文池的纯英文通用名 → 阻断(ok=False),与 T01 事故形态对齐。"""
    papers = _pool(6)
    groups = [
        Group(name="Sea Table Role Traceability", lit_ids=[p.lit_id for p in papers[:3]]),
        Group(name="冷链物流优化", lit_ids=[p.lit_id for p in papers[3:]]),
    ]
    check = _check(groups, papers)
    assert not check.passed, "英文名必须阻断"
    assert "Sea Table Role Traceability" in check.metrics["unacceptable"]


def test_short_name_warns_only():
    """短名(min-6)只告警不阻断:ok=True,short_names 记录。"""
    papers = _pool(6)
    groups = [
        Group(name="冷链物流", lit_ids=[p.lit_id for p in papers[:3]]),
        Group(name="品牌溢价机制研究", lit_ids=[p.lit_id for p in papers[3:]]),
    ]
    check = _check(groups, papers)
    assert check.passed, "短名不得阻断"
    assert "冷链物流" in check.metrics["short_names"]
    assert not check.metrics["unacceptable"]


def test_local_fallback_exempt_from_blocking():
    """local_fallback 兜底稿豁免组名阻断:ok=True + strategy_exempt。"""
    papers = _pool(6)
    groups = [
        Group(name="Sea Table Role Traceability", lit_ids=[p.lit_id for p in papers[:3]]),
        Group(name="冷链物流优化研究", lit_ids=[p.lit_id for p in papers[3:]]),
    ]
    check = _check(groups, papers, strategy="local_fallback")
    assert check.passed, "兜底策略下英文名不阻断"
    assert check.metrics["strategy_exempt"] is True
    assert "Sea Table Role Traceability" in check.metrics["unacceptable"], (
        "豁免只是不阻断,不良名单仍如实记录"
    )


def test_no_evidence_group_warns():
    """名实粗检零重叠组(修不掉保留原名)→ 告警通道 no_evidence。"""
    papers = _pool(6)
    groups = [
        Group(name="品牌溢价机制研究", lit_ids=[p.lit_id for p in papers[:3]]),
        Group(name="冷链物流优化研究", lit_ids=[p.lit_id for p in papers[3:]]),
    ]
    check = _check(groups, papers)
    assert check.passed, "零重叠组保留原名不阻断"
    assert "品牌溢价机制研究" in check.metrics["no_evidence"]
    assert any("零重叠" in w for w in check.metrics["warnings"])
