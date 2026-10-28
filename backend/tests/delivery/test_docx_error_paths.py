# -*- coding: utf-8 -*-
"""docx 渲染异常路径回归(2026-10-27 新增,不改 renderer)。

目标:四类异常输入下 render_docx_with_footnotes 不得以未诊断异常炸掉 ——
要么安全降级产出合法 .docx,要么抛出带上下文、可定位的明确错误。

缺陷处置记录:
- DEFECT-1(非 str title)与 DEFECT-2(非 str authors 元素)已于 2026-10-27
  Batch B0 修复:非 str 标题降级「未命名」、非 str 作者元素跳过,
  test_non_str_title_and_authors_degrade 锁定降级行为。
- 关联(types 层)同步修复:Paper(raw_citation=非 str) 构造期不再炸
  (canonical_publication_year 忽略非 str 引文,回退显式 year)。
"""
import re
import zipfile

import pytest

from delivery.docx_renderer import render_docx_with_footnotes
from retrieval.types import Paper, Source


def _cnki_paper(**overrides) -> Paper:
    base = dict(
        lit_id="lit_errpath_0001", source=Source.CNKI, title="异常路径研究",
        authors=["张三"], journal="测试学报", year=2024, abstract="摘要",
    )
    base.update(overrides)
    return Paper(**base)


def _render(tmp_path, name: str, **kw):
    out = tmp_path / f"{name}.docx"
    result = render_docx_with_footnotes(out_path=str(out), **kw)
    assert out.exists() and out.stat().st_size > 0
    return result, out


# ───────────────────────── 1. 空 content ─────────────────────────

def test_empty_md_text_renders_shell_document(tmp_path):
    """md_text 为空:不得崩,产出仅含标题与参考文献段的合法 docx。"""
    refs = "[1] 张三. 空正文降级研究[J]. 测试学报, 2024."
    result, out = _render(
        tmp_path, "empty",
        md_text="", papers=[], title="空正文综述", refs_text=refs,
    )
    assert result.footnote_count == 1
    assert result.body_paragraphs == 0
    z = zipfile.ZipFile(out)
    assert "word/footnotes.xml" in z.namelist()
    doc = z.read("word/document.xml").decode("utf-8")
    assert "参考文献" in doc


def test_empty_md_text_and_empty_refs(tmp_path):
    """md_text 与 refs_text 全空:空文档骨架,脚注为 0。"""
    result, out = _render(
        tmp_path, "all_empty",
        md_text="", papers=[], title="全空综述", refs_text="",
    )
    assert result.footnote_count == 0
    assert zipfile.ZipFile(out).namelist()


# ─────────────────── 2. 引用锚点指向不存在的文献 ───────────────────

def test_unknown_lit_anchor_and_out_of_range_number_degrade(tmp_path):
    """不存在的 [lit_xxx] 与越界 [999] 必须保留原文,不得误建脚注引用。"""
    refs = "[1] 李四. 唯一合法条目[J]. 测试学报, 2024."
    md = "正文引用了不存在的锚点[lit_nonexistent_999],以及越界编号[999]。唯一合法引用是[1]。"
    result, out = _render(
        tmp_path, "bad_anchor",
        md_text=md, papers=[], title="坏锚点综述", refs_text=refs,
    )
    z = zipfile.ZipFile(out)
    doc = z.read("word/document.xml").decode("utf-8")
    body = doc.split("参考文献")[0]
    # 坏锚点原文保留(不脚注化、不删除)
    assert "lit_nonexistent_999" in body
    assert "[999]" in body
    # 唯一合法引用已脚注化;全文档不存在 id=999 的脚注引用
    assert "<w:footnoteReference" in doc
    assert 'w:id="999"' not in doc
    assert result.footnote_count == 1


# ───────────────────────── 3. 超长标题 ─────────────────────────

def test_extremely_long_title_renders(tmp_path):
    """5 万字符标题:不得崩,标题完整写入 document.xml。"""
    long_title = "超" * 50000
    refs = "[1] 王五. 长标题研究[J]. 测试学报, 2024."
    result, out = _render(
        tmp_path, "long_title",
        md_text="## 一、主题\n正文段落[1]。",
        papers=[], title=long_title, refs_text=refs,
    )
    doc = zipfile.ZipFile(out).read("word/document.xml").decode("utf-8")
    assert long_title in doc


def test_multiline_title_renders_each_line(tmp_path):
    """含换行的标题不崩(python-docx 标题接受任意文本)。"""
    result, out = _render(
        tmp_path, "multiline_title",
        md_text="正文。",
        papers=[], title="第一行\n第二行", refs_text="",
    )
    doc = zipfile.ZipFile(out).read("word/document.xml").decode("utf-8")
    assert "第一行" in doc


# ───────────────────── 4. 非 str 类型字段 ─────────────────────

def test_none_authors_and_year_degrade_to_placeholder(tmp_path):
    """authors/year 为 None:当前实现安全降级("佚名"/"n.d."),锁定该行为。"""
    refs = "[1] 佚名. 元数据缺失研究[J]. 来源不详, n.d."
    paper = _cnki_paper(authors=None, journal=None, year=None, raw_citation=None)
    result, out = _render(
        tmp_path, "none_fields",
        md_text="正文[1]。", papers=[paper], title="元数据缺失综述",
    )
    assert result.footnote_count == 1
    doc = zipfile.ZipFile(out).read("word/document.xml").decode("utf-8")
    assert "佚名" in doc


def test_non_str_title_and_authors_degrade(tmp_path):
    """title=int / authors 含 int:B0 修复后安全降级(DEFECT-1/2 转绿)。

    - 非 str 标题 → 「未命名」占位,不得崩溃也不得原样渲染数字;
    - 非 str 作者元素跳过,合法作者保留。
    """
    paper = _cnki_paper(title=12345, authors=["张三", 123, None])
    result, out = _render(
        tmp_path, "non_str_fields",
        md_text="正文[1]。", papers=[paper], title="脏类型综述",
    )
    assert result.footnote_count == 1
    doc = zipfile.ZipFile(out).read("word/document.xml").decode("utf-8")
    assert "未命名" in doc
    assert "张三" in doc
    # 脏作者元素(123)被跳过:仅作者名段出现"张三",不出现裸数字作者
    assert "12345" not in doc.split("参考文献")[0]


def test_non_str_raw_citation_survives_paper_construction(tmp_path):
    """关联缺陷(B0):raw_citation 非 str 不得在 Paper 构造期炸
    (canonical_publication_year 的 re.search)。构造安全 + year 回退。"""
    paper = _cnki_paper(title="构造期防御研究", year=2024, raw_citation=999)  # type: ignore[arg-type]
    assert paper.year == 2024  # raw_citation 非 str 被忽略,回退显式 year
    result, out = _render(
        tmp_path, "non_str_raw_citation",
        md_text="正文[1]。", papers=[paper], title="构造期防御综述",
    )
    assert result.footnote_count == 1