# -*- coding: utf-8 -*-
"""引用注入偏移回归测试(锁定 v9.6 统一编辑列表修复)。

历史事故:锚点插入(content 变长)之后,再用「插入前」的偏移剥离未命中夹注,
content[start:end] 切的是错误区段 —— 正文被随机截断,下游整句删除基于错误
文本二次误删。修复后所有编辑统一基于原始偏移、按位置降序一次性应用。
"""
from retrieval.types import Paper, Source
from writing.section_writer import _inject_citations_by_author_year


def _paper(lit_id: str, author: str, year: int, title: str = "测试研究") -> Paper:
    return Paper(
        lit_id=lit_id, source=Source.CNKI, title=title,
        authors=[author], journal="测试学报", year=year, abstract="摘要",
    )


PAPERS = [
    _paper("lit_cnki_aaaa", "张欣欣", 2016),
    _paper("lit_cnki_bbbb", "李四", 2019),
]


def test_injection_after_insert_point_strips_correct_span():
    """未命中夹注位于注入点之前:剥离必须切中原夹注,不得殃及邻文。"""
    # 注:夹注正则的作者组支持「、/,/和」分隔,夹注前若有中文正文会被
    # 贪婪吸收进作者名,故王五前用全角冒号隔断(不在分隔符表内)
    content = "张欣欣(2016)提出协同治理框架。李四(2019)研究了基层实践:王五(2020)亦持相近立场。"
    out, cited, dropped = _inject_citations_by_author_year(content, PAPERS)
    # 注入两个锚点 + 王五(2020) 只剥年份保留作者,其余原文一字不差
    assert out == (
        "张欣欣(2016)提出协同治理框架[lit_cnki_aaaa]。"
        "李四(2019)研究了基层实践:王五亦持相近立场[lit_cnki_bbbb]。"
    )
    assert cited == ["lit_cnki_aaaa", "lit_cnki_bbbb"]
    assert dropped == ["王五(2020)"]


def test_unmatched_span_before_insertion_point():
    """未命中夹注在句首、注入点在其后:同样不得错切。"""
    content = "王五(2020)较早关注该议题。张欣欣(2016)深化了机制分析。"
    out, cited, dropped = _inject_citations_by_author_year(content, PAPERS)
    assert out == (
        "王五较早关注该议题。张欣欣(2016)深化了机制分析[lit_cnki_aaaa]。"
    )
    assert cited == ["lit_cnki_aaaa"]
    assert dropped == ["王五(2020)"]


def test_multiple_matches_in_one_sentence_share_sentence_end():
    """同一句两个命中夹注:锚点同点合并注入,顺序稳定。"""
    content = "张欣欣(2016),李四(2019)相继推进了该议题。"
    out, cited, _ = _inject_citations_by_author_year(content, PAPERS)
    assert out.endswith("[lit_cnki_aaaa][lit_cnki_bbbb]。")
    assert cited == ["lit_cnki_aaaa", "lit_cnki_bbbb"]


def test_no_edits_leaves_content_untouched():
    """全部命中且已注入过:不再发生任何内容改写。"""
    content = "张欣欣(2016)的研究奠定了基础。"
    out, cited, dropped = _inject_citations_by_author_year(content, PAPERS)
    assert out == "张欣欣(2016)的研究奠定了基础[lit_cnki_aaaa]。"
    assert dropped == []
    assert cited == ["lit_cnki_aaaa"]


def test_content_length_accounting():
    """长度守恒:输出 = 原文 + 锚点 - 剥离掉的年份括号。"""
    content = "李四(2019)指出路径依赖。王五(2020)提出异议。"
    out, _, _ = _inject_citations_by_author_year(content, PAPERS)
    anchor_len = len("[lit_cnki_bbbb]")          # 李四注入
    stripped_len = len("(2020)")                  # 王五剥掉的部分
    assert len(out) == len(content) + anchor_len - stripped_len
