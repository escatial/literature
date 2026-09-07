"""Word 原生脚注 DOCX 渲染器(方案 §1 "输出底线:使用 Word 原生脚注元素")。

方案硬约束:每处正文夹注都必须对应 word/footnotes.xml 中的真实脚注条目,
而参考文献列表统一按 GB/T 7714 渲染,DOI 不显示。

实现要点:
- python-docx 没有原生 add_footnote() API,
  需要手动操作 OOXML 元素 w:footnoteReference + w:footnote;
- 脚注 ID 在 1..N 之间单调递增,不可重复;
- 正文段落里需要把 [N] 标记替换为 w:r/w:rPr/w:vertAlign (superscript) + w:footnoteReference。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Iterable

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import nsmap, qn
from docx.shared import Pt

from writing.orchestrator import render_reference_list

log = logging.getLogger(__name__)

# 兼容 markdown / inline / 数字三种锚点形态
_CITE_RE = re.compile(r"\[(?:(\d{1,3})|lit_[a-zA-Z0-9_]+|hash:[a-zA-Z0-9_]+)\]")


@dataclass
class FootnoteItem:
    """单个脚注项。

    - index: 1-based 编号(在 word/footnotes.xml 中对应 w:id)
    - text:  脚注正文(GB/T 7714 渲染后的单条引文)
    """

    index: int
    text: str


@dataclass
class DocxBuildResult:
    """构建产物。"""

    out_path: str
    footnote_count: int
    reference_count: int
    body_paragraphs: int = 0


def _ensure_footnotes_part(doc: Document) -> None:
    """确保文档存在 word/footnotes.xml 部件。

    python-docx 默认不创建脚注部件,需要在第一次写脚注前手动初始化。
    """
    try:
        # 触发 _part._add_footnotes_part 的内部方法(若已存在则忽略)
        from docx.opc.constants import CONTENT_TYPE, RELATIONSHIP_TYPE
        from docx.opc.part import PartFactory
        # 通过访问 footnotes 属性强制创建
        _ = doc.part.footnotes_part  # type: ignore[attr-defined]
    except Exception:
        # 多数 python-docx 版本没有 footnotes_part 属性,需自己创建
        from docx.opc.constants import CONTENT_TYPE, RELATIONSHIP_TYPE
        from docx.opc.packuri import PackURI
        from docx.opc.part import Part

        partname = PackURI("/word/footnotes.xml")
        xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<w:footnotes xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">\n'
            '<w:footnote w:type="separator" w:id="-1">\n'
            '<w:p><w:r><w:separator/></w:r></w:p>\n'
            '</w:footnote>\n'
            '<w:footnote w:type="continuationSeparator" w:id="0">\n'
            '<w:p><w:r><w:continuationSeparator/></w:r></w:p>\n'
            '</w:footnote>\n'
            '</w:footnotes>\n'
        ).encode("utf-8")
        part = Part(
            partname,
            CONTENT_TYPE.WML_FOOTNOTES,
            xml,
            doc.part.package,
        )
        doc.part.relate_to(part, RELATIONSHIP_TYPE.FOOTNOTES)


def _add_footnote_definition(doc: Document, fn_id: int, text: str) -> None:
    """向 word/footnotes.xml 追加一条 w:footnote。"""
    from lxml import etree

    fn_part = None
    for rel in doc.part.rels.values():
        if rel.reltype.endswith("/footnotes"):
            fn_part = rel.target_part
            break
    if fn_part is None:
        raise RuntimeError("footnotes 部件不存在;请先调用 _ensure_footnotes_part")

    root = etree.fromstring(fn_part.blob)
    W = nsmap["w"]
    fn = OxmlElement("w:footnote")
    fn.set(qn("w:id"), str(fn_id))
    p = OxmlElement("w:p")
    pPr = OxmlElement("w:pPr")
    pStyle = OxmlElement("w:pStyle")
    pStyle.set(qn("w:val"), "FootnoteText")
    pPr.append(pStyle)
    p.append(pPr)

    # 脚注编号上标
    r0 = OxmlElement("w:r")
    rPr0 = OxmlElement("w:rPr")
    rStyle0 = OxmlElement("w:rStyle")
    rStyle0.set(qn("w:val"), "FootnoteReference")
    rPr0.append(rStyle0)
    r0.append(rPr0)
    ref = OxmlElement("w:footnoteRef")
    r0.append(ref)
    p.append(r0)

    # 空格分隔
    r_sp = OxmlElement("w:r")
    t_sp = OxmlElement("w:t")
    t_sp.text = " "
    t_sp.set(qn("xml:space"), "preserve")
    r_sp.append(t_sp)
    p.append(r_sp)

    # 脚注正文
    r1 = OxmlElement("w:r")
    t1 = OxmlElement("w:t")
    t1.text = text
    t1.set(qn("xml:space"), "preserve")
    r1.append(t1)
    p.append(r1)

    fn.append(p)
    root.append(fn)
    fn_part._blob = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


def _append_text_run(paragraph, text: str) -> None:
    r = OxmlElement("w:r")
    t = OxmlElement("w:t")
    t.text = text
    t.set(qn("xml:space"), "preserve")
    r.append(t)
    paragraph._p.append(r)


def _append_footnote_ref_run(paragraph, fn_index: int) -> None:
    """上标 + footnoteReference(w:id 对应脚注定义的 fn_id)。"""
    r1 = OxmlElement("w:r")
    rPr = OxmlElement("w:rPr")
    rStyle = OxmlElement("w:rStyle")
    rStyle.set(qn("w:val"), "FootnoteReference")
    rPr.append(rStyle)
    r1.append(rPr)
    fn_ref = OxmlElement("w:footnoteReference")
    fn_ref.set(qn("w:id"), str(fn_index))
    r1.append(fn_ref)
    paragraph._p.append(r1)


def _replace_anchors_with_footnote_refs(paragraph, max_index: int) -> int:
    """把段落中的所有 [N] 替换为脚注上标引用。

    python-docx 的 paragraph.runs 不暴露 XML 操作,
    这里直接对 paragraph._p 做字符串级处理后重建 runs。
    v9.6:此前只替换每个段落的第一个 [N],其余以裸文本残留,多数引用
    不可跳转——改为 finditer 全量替换;编号越界/非数字锚点(lit_xxx 等)
    保留原文不脚注化。返回成功替换的引用数。
    """
    raw = paragraph.text or ""
    matches = list(_CITE_RE.finditer(raw))
    if not matches:
        return 0

    # 清空现有 runs
    for r in list(paragraph._p):
        if r.tag == qn("w:r"):
            paragraph._p.remove(r)

    replaced = 0
    cursor = 0
    for m in matches:
        if m.start() > cursor:
            _append_text_run(paragraph, raw[cursor:m.start()])
        num = int(m.group(1)) if m.group(1) else None
        if num is not None and 1 <= num <= max_index:
            _append_footnote_ref_run(paragraph, fn_index=num)
            replaced += 1
        else:
            # 非数字/越界锚点保留原文
            _append_text_run(paragraph, m.group(0))
        cursor = m.end()
    if cursor < len(raw):
        _append_text_run(paragraph, raw[cursor:])
    return replaced


def render_docx_with_footnotes(
    *,
    md_text: str,
    out_path: str,
    papers: list,
    title: str = "文献综述",
) -> DocxBuildResult:
    """主入口:把 Markdown 综述 + 论文元数据渲染为带 Word 原生脚注的 .docx。

    Args:
        md_text:   综述 Markdown 文本
        out_path:  输出 .docx 路径
        papers:    Paper 列表(必须与正文中 [lit_xxx] / [N] 一一对应)
        title:     文档主标题
        style_id:  citeproc CSL 样式 ID

    Returns:
        DocxBuildResult(out_path, footnote_count, reference_count, body_paragraphs)
    """
    # 1. 先用 citeproc 渲染参考文献清单(统一按引用顺序编号 [1..N])
    refs_text = render_reference_list(papers)

    # 2. 把 references 拆成 list[FootnoteItem]
    fn_items: list[FootnoteItem] = []
    for i, line in enumerate(refs_text.splitlines(), start=1):
        s = line.strip()
        if not s:
            continue
        # citeproc 渲染的条目形如 "[1] 张三. ..."——剥掉 [N] 前缀
        m = re.match(r"^\[(\d+)\]\s*(.*)$", s)
        if m:
            idx = int(m.group(1))
            body = m.group(2)
        else:
            idx = i
            body = s
        fn_items.append(FootnoteItem(index=idx, text=body))

    # 3. 创建 Document
    doc = Document()

    # 默认中文字体
    style = doc.styles["Normal"]
    style.font.name = "Times New Roman"
    style.font.size = Pt(10.5)
    style.element.rPr.rFonts.set(qn("w:eastAsia"), "宋体")

    # 4. 确保 footnotes 部件存在
    _ensure_footnotes_part(doc)

    # 5. 写入脚注定义
    for fn in fn_items:
        _add_footnote_definition(doc, fn_id=fn.index, text=fn.text)

    # 6. 写入正文(按 Markdown 简单解析:# / ## / ### / 普通段落)
    doc.add_heading(title, level=0)
    fn_index = 0
    in_code = False
    body_paragraphs = 0
    for raw in md_text.splitlines():
        line = raw.rstrip()
        if line.startswith("```"):
            in_code = not in_code
            continue
        if not line:
            continue
        if line.startswith("# "):
            doc.add_heading(line[2:], level=0)
        elif line.startswith("## "):
            doc.add_heading(line[3:], level=1)
        elif line.startswith("### "):
            doc.add_heading(line[4:], level=2)
        else:
            p = doc.add_paragraph()
            run = p.add_run(line)
            run.font.name = "Times New Roman"
            run.font.size = Pt(10.5)
            # v9.6:把段落中所有 [N] 都替换为脚注引用(此前只替换第一个)
            if _replace_anchors_with_footnote_refs(p, max_index=len(fn_items)):
                body_paragraphs += 1

    # 7. 末尾添加参考文献列表段
    doc.add_heading("参考文献", level=1)
    for fn in fn_items:
        p = doc.add_paragraph()
        run = p.add_run(f"[{fn.index}] {fn.text}")
        run.font.name = "Times New Roman"
        run.font.size = Pt(10.5)

    doc.save(out_path)

    return DocxBuildResult(
        out_path=out_path,
        footnote_count=len(fn_items),
        reference_count=len(fn_items),
        body_paragraphs=body_paragraphs,
    )


__all__ = ["render_docx_with_footnotes", "DocxBuildResult", "FootnoteItem"]