"""screen_batch 单元测试(mock 掉 LLM 客户端)。"""
from __future__ import annotations

from unittest.mock import patch

from retrieval.types import Paper, Source
from screening.llm_filter import screen_batch


def make(lit_id: str, title: str = "T", abstract: str = "A") -> Paper:
    return Paper(
        lit_id=lit_id, source=Source.OPENALEX,
        title=title, authors=["X"], journal="J",
        year=2024, abstract=abstract,
    )


def test_empty_input_returns_empty():
    assert screen_batch([], topic="任意") == []


def test_normal_response():
    papers = [make("lit_a"), make("lit_b")]
    fake_resp = (
        '[{"lit_id":"lit_a","relevant":true,"reason":"相关"},'
        '{"lit_id":"lit_b","relevant":false,"reason":"无关"}]'
    )
    with patch("screening.llm_filter.messages_create", return_value=fake_resp):
        results = screen_batch(papers, topic="AI 医学影像")
    assert len(results) == 2
    by_id = {r["lit_id"]: r for r in results}
    assert by_id["lit_a"]["relevant"] is True
    assert by_id["lit_b"]["relevant"] is False


def test_llm_failure_falls_back_to_keep_all():
    papers = [make("lit_a"), make("lit_b")]
    with patch("screening.llm_filter.messages_create", side_effect=RuntimeError("LLM 出错")):
        results = screen_batch(papers, topic="AI 医学影像")
    assert all(r["relevant"] for r in results)


def test_invalid_json_falls_back_to_keep_all():
    papers = [make("lit_a")]
    with patch("screening.llm_filter.messages_create", return_value="不是 JSON"):
        results = screen_batch(papers, topic="x")
    assert results[0]["relevant"] is True


def test_malformed_item_skipped():
    papers = [make("lit_a"), make("lit_b")]
    fake_resp = (
        '[{"lit_id":"lit_a","relevant":true,"reason":"x"},'
        '{"wrong":"field"},'
        '{"lit_id":"lit_b","relevant":false}]'
    )
    with patch("screening.llm_filter.messages_create", return_value=fake_resp):
        results = screen_batch(papers, topic="x")
    assert len(results) == 2
    assert {r["lit_id"] for r in results} == {"lit_a", "lit_b"}


def test_unknown_lit_id_skipped():
    papers = [make("lit_a")]
    fake_resp = '[{"lit_id":"lit_unknown","relevant":false}]'
    with patch("screening.llm_filter.messages_create", return_value=fake_resp):
        results = screen_batch(papers, topic="x")
    # 未知 ID 被跳过,然后 lit_a 被兜底
    assert results[0]["lit_id"] == "lit_a"
    assert results[0]["relevant"] is True
