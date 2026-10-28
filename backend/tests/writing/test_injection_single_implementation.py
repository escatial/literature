# -*- coding: utf-8 -*-
"""引用注入单一实现守卫(2026-10-27 死代码清理回归)。

历史隐患:模块内 _build_surname_year_index / _inject_citations_by_author_year
各定义了两次,前一份旧实现(AUTHOR_YEAR_PATTERNS + _extract_surname,返回
2 元组)被后定义的 V63 版(返回 3 元组)覆盖 —— 运行时死代码,但维护者修改
旧版不生效、两版返回契约不同,极易造成事故。

本测试锁定:
1. 旧版符号已从模块中移除(重名定义不再存在);
2. 生效的 _inject_citations_by_author_year 是 V63 版:返回 3 元组,
   dropped_unmatched 承载未命中夹注(幻觉引用)原文。
注入行为本身由 test_citation_injection_edits.py 详细锁定,此处不重复。
"""
import inspect

import writing.section_writer as sw
from retrieval.types import Paper, Source


def test_legacy_duplicate_symbols_are_removed():
    """旧版死代码符号(AUTHOR_YEAR_PATTERNS / _extract_surname)不得再存在。"""
    assert not hasattr(sw, "AUTHOR_YEAR_PATTERNS"), (
        "旧版 AUTHOR_YEAR_PATTERNS 仍存在:V63 版使用 _AUTHOR_YEAR_PATTERNS_V63,"
        "同名旧版是被覆盖的死代码,修改它不会生效"
    )
    assert not hasattr(sw, "_extract_surname"), (
        "旧版 _extract_surname 仍存在:只被旧版死代码引用,应随死代码一并移除"
    )


def test_injection_is_v63_three_tuple_implementation():
    """生效实现必须是返回 3 元组的 V63 版(单一定义,含 dropped_unmatched)。"""
    func = sw._inject_citations_by_author_year
    source = inspect.getsource(func)
    # V63 版签名特征:docstring 声明三返回值 + 返回语句为 3 元组
    assert "dropped_unmatched" in source
    assert "return content, [], []" in source

    paper = Paper(
        lit_id="lit_guard_0001", source=Source.CNKI, title="守卫研究",
        authors=["钱守卫"], journal="守卫学报", year=2024, abstract="摘要",
    )
    content = "钱守卫(2024)提出守卫框架。孙幻觉(2099)提出相反结论。"
    out, cited, dropped = func(content, [paper])
    assert isinstance(out, str)
    assert cited == ["lit_guard_0001"]
    # 幻觉夹注:正文剥掉(年份)保留作者,原文进 dropped_unmatched
    assert "孙幻觉(2099)" not in out
    assert "孙幻觉" in out
    assert dropped == ["孙幻觉(2099)"]
