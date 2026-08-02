"""提示词模板单元测试(纯本地, 不调 LLM)。"""
from __future__ import annotations

import pytest

from prompts import PromptTemplate, list_templates


def test_list_templates():
    ids = list_templates()
    assert "humanizer-zh" in ids
    assert "literature-review-section" in ids
    assert "literature-review-classify" in ids


def test_humanizer_loads():
    tpl = PromptTemplate.load("humanizer-zh")
    assert tpl.id == "humanizer-zh"
    assert "{{text}}" in tpl.body


def test_humanizer_render_basic():
    tpl = PromptTemplate.load("humanizer-zh")
    out = tpl.render(text="测试文本")
    assert "测试文本" in out
    assert "{{text}}" not in out


def test_humanizer_render_missing_required():
    tpl = PromptTemplate.load("humanizer-zh")
    with pytest.raises(ValueError, match="missing required params: \\['text'\\]"):
        tpl.render()


def test_litrev_section_renders():
    tpl = PromptTemplate.load("literature-review-section")
    out = tpl.render(
        topic="AI 营销",
        section_key="introduction",
        section_title="一、引言",
        section_role="综述开篇",
        papers_catalog="- lit_aaa | 测试 | 某 | J | 2024",
        available_lit_ids="lit_aaa",
        humanize=True,
    )
    assert "AI 营销" in out
    assert "lit_aaa" in out
    assert "批判性写作" in out  # body 关键字


def test_litrev_classify_renders():
    tpl = PromptTemplate.load("literature-review-classify")
    out = tpl.render(
        topic="AI 营销",
        classify_mode="theme",
        papers_catalog="- lit_aaa | t | j | 2024 | source=openalex",
    )
    assert "AI 营销" in out
    assert "theme" in out


def test_cache_loads_once():
    a = PromptTemplate.load("humanizer-zh")
    b = PromptTemplate.load("humanizer-zh")
    assert a is b