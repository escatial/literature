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


# ============ 线上 423 篇池实测回归(2026-10-15) ============
# 事故链:某批 MiniMax 返回 200 OK 但正文为空 → 拆半重试 → 子批返回退化
# JSON({"lit_id": ""})→「lit_id 为空」不在可重试集合,直接炸穿顶层,
# 7 分钟筛选作废、主题划分终止。

def test_empty_lit_id_dropped_missing_coverage_bisects():
    """空 lit_id 条目丢弃(不 raise),漏掉的真实 lit_id 由「未覆盖」兜底重试。"""
    decisions = [
        _dec(lit_id=""),  # 退化条目:aaaa0001 被顶替
        _dec(lit_id="lit_oa_bbbb0002"),
        _dec(lit_id="lit_oa_cccc0003"),
    ]
    with pytest.raises(ScreeningError, match="未覆盖"):
        _normalize_chunk_decisions(decisions, BATCH)
    # 合法条目仍保留在异常路径之外的解析结果里(单独归一化验证)
    ok = _normalize_chunk_decisions(decisions[1:], BATCH[1:])
    assert [d["lit_id"] for d in ok] == ["lit_oa_bbbb0002", "lit_oa_cccc0003"]


def test_empty_output_retries_wholesale_then_bisects(monkeypatch):
    """空正文 → 整批重采样一次即恢复,不拆半(请求数不翻倍)。"""
    import screening.llm_filter as lf

    calls = {"n": 0}

    def fake_llm(system, user, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return ""  # 第一次:空正文(MiniMax 思考吃预算)
        return (
            '{"results": ['
            '{"lit_id": "lit_oa_aaaa0001", "relevant": true, "abstract_ok": true,'
            ' "field_match": 80, "method_applicability": 70, "conclusion_value": 75,'
            ' "method_rigor": 65, "reason": "ok"},'
            '{"lit_id": "lit_oa_bbbb0002", "relevant": false, "abstract_ok": true,'
            ' "field_match": 30, "method_applicability": 40, "conclusion_value": 35,'
            ' "method_rigor": 50, "reason": "无关"},'
            '{"lit_id": "lit_oa_cccc0003", "relevant": true, "abstract_ok": true,'
            ' "field_match": 85, "method_applicability": 72, "conclusion_value": 80,'
            ' "method_rigor": 70, "reason": "ok"}]}'
        )

    monkeypatch.setattr(lf, "messages_create", fake_llm)
    result = lf._screen_chunk_with_fallback(BATCH, "水产品营销策略", 500)
    assert calls["n"] == 2  # 整批重采样一次,未拆半
    assert [d["lit_id"] for d in result] == [
        "lit_oa_aaaa0001", "lit_oa_bbbb0002", "lit_oa_cccc0003",
    ]
    assert result[0]["method_rigor"] == 65  # 第 5 维透传


def test_empty_output_then_degenerate_recovers_via_bisect(monkeypatch):
    """空正文重采样后仍退化(全空 lit_id)→ 拆半后各子批恢复正常,整体不炸。"""
    import json as _json
    import screening.llm_filter as lf

    def decision(lit_id):
        return (_json.dumps({"lit_id": lit_id, "relevant": True, "abstract_ok": True,
                             "field_match": 80, "method_applicability": 70,
                             "conclusion_value": 75, "method_rigor": 65,
                             "reason": "ok"}, ensure_ascii=False))

    calls = {"n": 0}

    def fake_llm(system, user, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return ""  # 整批空正文
        if calls["n"] == 2:
            return '{"results": [{"lit_id": "", "relevant": true, "abstract_ok": true, "reason": "退化"}]}'
        # 拆半后的子批调用:按 user 里出现的 lit_id 逐篇返回
        ids = [i for i in (
            "lit_oa_aaaa0001", "lit_oa_bbbb0002", "lit_oa_cccc0003") if i in user]
        return '{"results": [' + ",".join(
            '{"lit_id": "%s", "relevant": true, "abstract_ok": true,'
            ' "field_match": 80, "method_applicability": 70, "conclusion_value": 75,'
            ' "method_rigor": 65, "reason": "ok"}' % i for i in ids) + "]}"

    monkeypatch.setattr(lf, "messages_create", fake_llm)
    result = lf._screen_chunk_with_fallback(BATCH, "水产品营销策略", 500)
    assert [d["lit_id"] for d in result] == [
        "lit_oa_aaaa0001", "lit_oa_bbbb0002", "lit_oa_cccc0003",
    ]
    assert calls["n"] >= 4  # 空正文→重采样→退化→拆半恢复
