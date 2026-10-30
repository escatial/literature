# -*- coding: utf-8 -*-
"""筛选结果抖动容忍回归测试。

历史事故 1(338/282 篇池):LLM 把 25 位十六进制 lit_id 抄错一位 →
未知 lit_id 直接 raise → 7 分钟筛选作废。修复:幻觉 id 丢弃+告警。
历史事故 2(423 篇池,2026-10-15):某批 200 OK 空正文 → 拆半 → 子批
退化 JSON({"lit_id": ""})→「lit_id 为空」不在可重试集合,炸穿顶层。
修复:空正文整批重采样一次再拆半;空 id 条目丢弃+「未覆盖」兜底。
根治(v9.9 编号方案):输入载荷改用批内数字编号 no,模型只回显数字,
后端按位置回填 lit_id —— 抄写类抖动从源头消失。
"""
import json

import pytest

import screening.llm_filter as lf
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
    base = {"no": 1, "relevant": True, "abstract_ok": True, "reason": "ok"}
    base.update(kw)
    return base


def _full_results(**overrides) -> str:
    """三篇全量的合法筛选 JSON(no 1/2/3)。"""
    entries = []
    for no in (1, 2, 3):
        entry = {
            "no": no, "relevant": True, "abstract_ok": True,
            "field_match": 80, "method_applicability": 70,
            "conclusion_value": 75, "method_rigor": 65, "reason": "ok",
        }
        entry.update(overrides.get(no, {}))
        entries.append(entry)
    return json.dumps({"results": entries}, ensure_ascii=False)


# ---------- 编号回填与抖动容忍 ----------

def test_no_mapped_back_to_real_lit_id():
    decisions = [_dec(no=1), _dec(no=3), _dec(no=2)]
    normalized = _normalize_chunk_decisions(decisions, BATCH)
    assert [d["lit_id"] for d in normalized] == [
        "lit_oa_aaaa0001", "lit_oa_cccc0003", "lit_oa_bbbb0002",
    ]
    assert all(d.get("method_rigor") is None for d in normalized)


def test_out_of_range_no_dropped_not_fatal():
    """编号越界(幻觉/抄错)只丢弃该条,批内合法决策全部保留。"""
    decisions = [_dec(no=1), _dec(no=4), _dec(no=2), _dec(no=3)]
    normalized = _normalize_chunk_decisions(decisions, BATCH)
    assert [d["lit_id"] for d in normalized] == [
        "lit_oa_aaaa0001", "lit_oa_bbbb0002", "lit_oa_cccc0003",
    ]


def test_duplicate_no_keeps_first():
    decisions = [_dec(no=1, relevant=False), _dec(no=1, relevant=True), _dec(no=2), _dec(no=3)]
    normalized = _normalize_chunk_decisions(decisions, BATCH)
    assert [d["lit_id"] for d in normalized] == [
        "lit_oa_aaaa0001", "lit_oa_bbbb0002", "lit_oa_cccc0003",
    ]
    assert normalized[0]["relevant"] is False  # 保留第一条


def test_missing_coverage_still_raises_for_split_retry():
    """编号漏掉时必须报「未覆盖」,触发整批重采样/拆半重试。"""
    decisions = [_dec(no=1), _dec(no=2)]  # no=3 漏了
    with pytest.raises(ScreeningError, match="未覆盖"):
        _normalize_chunk_decisions(decisions, BATCH)


def test_degenerate_entry_without_no_dropped():
    """低预算退化条目(缺 no)丢弃,不炸穿;漏掉的编号走「未覆盖」兜底。"""
    decisions = [{"relevant": True, "abstract_ok": True, "reason": "退化"}]
    with pytest.raises(ScreeningError, match="未覆盖"):
        _normalize_chunk_decisions(decisions, BATCH)


def test_non_bool_field_still_raises():
    decisions = [_dec(no=1), _dec(no=2, relevant="yes"), _dec(no=3)]
    with pytest.raises(ScreeningError, match="relevant"):
        _normalize_chunk_decisions(decisions, BATCH)


def test_string_no_accepted():
    """模型把 no 输出成字符串("1")时按数字接受;编号按批内切片位置计。"""
    normalized = _normalize_chunk_decisions([_dec(no="1")], BATCH[1:2])
    assert [d["lit_id"] for d in normalized] == ["lit_oa_bbbb0002"]


# ---------- 空正文/退化的批次级自愈 ----------

def test_empty_output_retries_wholesale_then_bisects(monkeypatch):
    """空正文 → 整批重采样一次即恢复,不拆半(请求数不翻倍)。"""
    calls = {"n": 0}

    def fake_llm(system, user, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return ""  # 第一次:空正文(MiniMax 思考吃预算)
        return _full_results()

    monkeypatch.setattr(lf, "messages_create", fake_llm)
    result = lf._screen_chunk_with_fallback(BATCH, "水产品营销策略", 500)
    assert calls["n"] == 2  # 整批重采样一次,未拆半
    assert [d["lit_id"] for d in result] == [
        "lit_oa_aaaa0001", "lit_oa_bbbb0002", "lit_oa_cccc0003",
    ]
    assert result[0]["method_rigor"] == 65  # 第 5 维透传


def test_empty_output_then_degenerate_recovers_via_bisect(monkeypatch):
    """空正文重采样后仍退化(全部缺 no)→ 拆半后子批恢复,整体不炸。"""
    calls = {"n": 0}

    def fake_llm(system, user, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return ""  # 整批空正文
        if calls["n"] == 2:
            return '{"results": [{"relevant": true, "abstract_ok": true, "reason": "退化"}]}'
        # 拆半后的子批调用:始终回全量 1..3,批外编号被丢弃,批内覆盖达标
        return _full_results()

    monkeypatch.setattr(lf, "messages_create", fake_llm)
    result = lf._screen_chunk_with_fallback(BATCH, "水产品营销策略", 500)
    assert [d["lit_id"] for d in result] == [
        "lit_oa_aaaa0001", "lit_oa_bbbb0002", "lit_oa_cccc0003",
    ]
    assert calls["n"] >= 4  # 空正文→重采样→退化→拆半恢复


# ---------- 批次级外层重试(2026-10-30 F2 事故:残片批连续超时全场作废) ----------

def test_batch_outer_retry_recovers_after_timeouts(monkeypatch):
    """前 2 次调用超时(内部重试耗尽)→ 外层歇 20 秒整批重跑,第 3 次成功,
    批次最终完成,不炸整场。"""
    calls = {"n": 0}
    sleeps: list[float] = []

    def fake_llm(system, user, **kwargs):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise TimeoutError("simulated gateway timeout")
        return _full_results()

    monkeypatch.setattr(lf, "messages_create", fake_llm)
    monkeypatch.setattr(lf.time, "sleep", lambda s: sleeps.append(s))
    result = lf._screen_chunk_with_fallback(BATCH, "水产品营销策略", 500)
    assert calls["n"] == 3, "两次超时 + 外层重跑一次成功"
    assert [d["lit_id"] for d in result] == [
        "lit_oa_aaaa0001", "lit_oa_bbbb0002", "lit_oa_cccc0003",
    ]
    assert sleeps == [20.0, 20.0], "每次外层重试前歇 _BATCH_OUTER_RETRY_SLEEP_SECONDS"


def test_batch_outer_retry_exhausts_and_raises(monkeypatch, caplog):
    """始终超时 → 外层最多重试 2 次后按现语义抛 ScreeningError。"""
    import logging

    calls = {"n": 0}

    def fake_llm(system, user, **kwargs):
        calls["n"] += 1
        raise TimeoutError("simulated gateway timeout")

    monkeypatch.setattr(lf, "messages_create", fake_llm)
    monkeypatch.setattr(lf.time, "sleep", lambda s: None)
    with caplog.at_level(logging.WARNING, logger="screening.llm_filter"):
        with pytest.raises(ScreeningError, match="筛选批次调用失败"):
            lf._screen_chunk_with_fallback(BATCH, "水产品营销策略", 500)
    assert calls["n"] == 3, "首跑 + 2 次外层重试,耗尽即抛"
    assert any("外层重试" in r.message for r in caplog.records), "WARNING 提示复用日志通道"
