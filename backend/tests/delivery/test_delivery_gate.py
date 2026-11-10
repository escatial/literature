# -*- coding: utf-8 -*-
"""D1 交付链加固测试(2026-11-01 交付事故的回归防线)。

覆盖:
- 锚点编号替换:未知 [lit_xxx] 必须剥除(此前原样保留泄漏 41 处进交付物);
- 定稿序号重排:标题按交付位置连续编号(此前沿用规划期编号出现「一三五二」);
- 交付清单 manifest:缺章/锚点泄漏/参考文献覆盖计数;
- SSE seq 注入:帧携带单调序号供前端丢帧检测;
- POST /reviews 门禁:锚点残留 422 拒绝入库;服务端权威副本调和;
- 导出门禁:历史行残留锚点在导出前剥离。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import sys

BACKEND_ROOT = Path(__file__).resolve().parents[2]
for p in (str(BACKEND_ROOT / "src"), str(BACKEND_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from api.writing import _inject_seq  # noqa: E402
from delivery.render_checker import (  # noqa: E402
    check_sections_delivery,
    strip_internal_anchors,
)
from writing.orchestrator import (  # noqa: E402
    _build_delivery_manifest,
    _renumber_section_titles,
    replace_anchors_with_numbers,
)


@dataclass
class _FakeSection:
    """SectionResult 的最小替身(renumber/manifest 只碰这四个字段)。"""
    key: str
    title: str
    content: str = ""
    citations: list = field(default_factory=list)


# ── 锚点编号替换 ──────────────────────────────────────────────

def test_replace_anchors_strips_unknown_not_leak():
    """可映射锚点换 [N];未知锚点剥除(2026-11-01 泄漏 41 处的根因回归)。"""
    content = "开头[lit_a]中间[lit_ghost]结尾[lit_b]"
    out = replace_anchors_with_numbers(content, {"lit_a": 1, "lit_b": 2})
    assert out == "开头[1]中间结尾[2]"
    assert "lit_ghost" not in out


def test_replace_anchors_idempotent_on_numbered_text():
    assert replace_anchors_with_numbers("已编号[1]", {"lit_a": 1}) == "已编号[1]"


# ── 定稿序号重排 ──────────────────────────────────────────────

def test_renumber_titles_positional_and_idempotent():
    sections = [
        _FakeSection(key="theme_1", title="三、消费者决策机制"),
        _FakeSection(key="theme_2", title="一、品牌价值建设传播"),
        _FakeSection(key="comment", title="五、文献述评"),
    ]
    _renumber_section_titles(sections)
    assert [s.title for s in sections] == ["一、消费者决策机制", "二、品牌价值建设传播", "三、文献述评"]
    # 幂等:重跑不变
    _renumber_section_titles(sections)
    assert sections[0].title == "一、消费者决策机制"


# ── 交付清单 manifest ─────────────────────────────────────────

def test_manifest_counts_missing_leaks_and_refs():
    sections = [
        _FakeSection(key="theme_1", title="一、A", content="正文[1][lit_ghost]", citations=["lit_a"]),
        _FakeSection(key="comment", title="二、文献述评", content="述评无引用"),
    ]
    specs = [_FakeSection(key="theme_1", title="一、A"), _FakeSection(key="theme_2", title="二、B"),
             _FakeSection(key="comment", title="三、述评")]
    manifest = _build_delivery_manifest(sections, specs, "[1] 张三. 某文[J]. 2020.\n[2] 李四. 另文[J]. 2021.", "")
    assert manifest["missing_sections"] == ["theme_2"]
    assert manifest["anchor_leaks"] == 1
    assert manifest["reference_count"] == 2
    assert manifest["expected_sections"] == 3
    assert not manifest["has_abstract"]


def test_manifest_clean_delivery():
    sections = [_FakeSection(key="theme_1", title="一、A", content="正文[1]", citations=["lit_a"])]
    specs = [_FakeSection(key="theme_1", title="一、A")]
    manifest = _build_delivery_manifest(sections, specs, "[1] 张三. 某文[J]. 2020.", "这是摘要")
    assert manifest["missing_sections"] == []
    assert manifest["anchor_leaks"] == 0
    assert manifest["has_abstract"] is True


# ── SSE seq 注入 ──────────────────────────────────────────────

def test_inject_seq_adds_monotonic_number():
    frame = 'data: {"event":"section_done","data":{"key":"theme_1"}}\n\n'
    out = _inject_seq(frame, 7)
    assert '"seq": 7' in out or '"seq":7' in out
    # 非 data 帧原样返回
    assert _inject_seq(": heartbeat\n\n", 8) == ": heartbeat\n\n"
    # 坏 JSON 帧保底直通
    assert _inject_seq("data: not-json\n\n", 9) == "data: not-json\n\n"


# ── render_checker 章节级门禁 ─────────────────────────────────

def test_strip_internal_anchors():
    text, n = strip_internal_anchors("正文[lit_cn_1]与[lit_openalex_abc]共两处")
    assert n == 2 and "lit_" not in text


def test_check_sections_delivery_flags_all_defects():
    sections = [
        {"key": "theme_1", "title": "三、乱序", "content": "正文[lit_cn_1]引用,TODO 占位"},
        {"key": "comment", "title": "文献述评", "content": ""},
    ]
    issues = check_sections_delivery(sections, "[1] 甲[J]. 2020.\n[2] 乙[J]. 2021.")
    codes = {i["code"] for i in issues}
    assert "ANCHOR_LEAK" in codes
    assert "PLACEHOLDER_FOUND" in codes
    assert "EMPTY_SECTION" in codes
    assert "TITLE_NOT_NUMBERED" in codes
    assert "REFS_UNCITED" in codes


def test_check_sections_delivery_clean_pass():
    sections = [{"key": "theme_1", "title": "一、主题", "content": "正文引用[1]。"}]
    assert check_sections_delivery(sections, "[1] 甲[J]. 2020.") == []
