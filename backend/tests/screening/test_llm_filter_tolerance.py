# -*- coding: utf-8 -*-
"""筛选结果 lit_id 抖动容忍回归测试。

历史事故(338/282 篇池实测复现):LLM 在筛选 JSON 里把 25 字符十六进制
lit_id 抄错一位 → "_normalize_chunk_decisions: 未知 lit_id" 直接 raise
→ 7 分钟的筛选全部作废、规划以错误终止。修复后幻觉 id 丢弃+告警;
真正漏掉的 lit_id 仍由「未覆盖」校验拆半重试兜底。
"""
import pytest

from retrieval.types import Paper, Source
from screening.llm_filter import (
    ScreeningError,
    _normalize_chunk_decisions,
)


def _paper(lit_id: str) -> Paper:
    return Paper(
        lit_id=lit_id, source=Source.OPENALEX, title=f"Paper {lit_id[-4:]}",
        authors=["Smith J"], journal="J", year=2023, abstract="abs",
    )


BATCH = [_paper("lit_oa_aaaa0001"), _paper("lit_oa_bbbb0002"), _paper("lit_oa_cccc0003")]


def _dec(**kw):
    base = {"relevant": True, "abstract_ok": True, "reason": "ok"}
    base.update(kw)
    return base


def test_hallucinated_lit_id_dropped_not_fatal():
    """幻觉 id(抄错一位)只丢弃该条,同批合法决策全部保留。"""
    decisions = [
        _dec(lit_id="lit_oa_aaaa0001"),
        _dec(lit_id="lit_oa_aaaa0002"),  # 幻觉:bbbb 抄成 aaaa,不在批内
        _dec(lit_id="lit_oa_bbbb0002"),
        _dec(lit_id="lit_oa_cccc0003"),
    ]
    normalized = _normalize_chunk_decisions(decisions, BATCH)
    assert [d["lit_id"] for d in normalized] == [
        "lit_oa_aaaa0001", "lit_oa_bbbb0002", "lit_oa_cccc0003",
    ]


def test_duplicate_lit_id_keeps_first():
    decisions = [
        _dec(lit_id="lit_oa_aaaa0001", relevant=False),
        _dec(lit_id="lit_oa_aaaa0001", relevant=True),
        _dec(lit_id="lit_oa_bbbb0002"),
        _dec(lit_id="lit_oa_cccc0003"),
    ]
    normalized = _normalize_chunk_decisions(decisions, BATCH)
    assert [d["lit_id"] for d in normalized] == [
        "lit_oa_aaaa0001", "lit_oa_bbbb0002", "lit_oa_cccc0003",
    ]
    assert normalized[0]["relevant"] is False  # 保留第一条


def test_missing_coverage_still_raises_for_split_retry():
    """幻觉 id 丢掉导致真实 lit_id 漏掉时,仍必须走「未覆盖」拆半重试。"""
    decisions = [
        _dec(lit_id="lit_oa_aaaa0001"),
        _dec(lit_id="lit_oa_bbbb0002"),
        _dec(lit_id="lit_oa_halluc0099"),  # cc..0003 被幻觉顶替
    ]
    with pytest.raises(ScreeningError, match="未覆盖"):
        _normalize_chunk_decisions(decisions, BATCH)


def test_non_bool_field_still_raises():
    decisions = [
        _dec(lit_id="lit_oa_aaaa0001"),
        _dec(lit_id="lit_oa_bbbb0002", relevant="yes"),
        _dec(lit_id="lit_oa_cccc0003"),
    ]
    with pytest.raises(ScreeningError, match="relevant"):
        _normalize_chunk_decisions(decisions, BATCH)
