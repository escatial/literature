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
from lxml import etree
from docx.shared import Pt

from writing.orchestrator import render_reference_list

log = logging.getLogger(__name__)

# 兼容 markdown / inline / 数字三种锚点形态
_CITE_RE = re.compile(r"\[(?:(\d{1,3})|lit_[a-zA-Z0-9_]+|hash:[a-zA-Z0-9_]+)\]")

# 生成的文档不允许出现 DOI 标识(用户硬性要求,模块 docstring 同款约束):
# DOI 前缀/doi.org 链接/PubMed 链接整段移除。corpus 导出路径
# (api/papers.py::_strip_doi_marks)已同语义;review 导出的英文条目
# (citeproc/OpenAlex 渲染)此前直通泄漏([25]-[48] 带 DOI:10.xxx 与
# pubmed 链接,2026-10-30 导出产物实锤),在 refs 拆行处统一剥 ——
# 脚注定义与参考文献列表段同源自 fn_items,单点覆盖两个出口;
# 对已剥过的 corpus 条目幂等无害。
_DOI_MARK_RE = re.compile(
    r"(?:DOI\s*[:：]?\s*|https?://(?:dx\.)?doi\.org/|doi\.org/)\S+"
    r"|https?://(?:www\.)?pubmed\.ncbi\.nlm\.nih\.gov/\S+",
    re.IGNORECASE,
)


def _strip_doi_marks(text: str) -> str:
    """移除参考文献条目中的 DOI 标识与 PubMed 链接(不改写其余字符)。"""
    cleaned = _DOI_MARK_RE.sub("", text or "")
    # 剥离后可能留下悬空的分隔标点("作者. 标题. , ."形态),只做
    # 保守的尾段清理:重复标点/空白收敛,不碰任何文字内容。
    cleaned = re.sub(r"\s+,(\s*,)*\s*$", "", cleaned)
    cleaned = re.sub(r"\.\s*(,\s*)+\.$", ".", cleaned)
    return cleaned.strip()


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


def _ensure_footnotes_part(doc: Document) -> tuple:
    """确保文档存在 word/footnotes.xml 部件,返回 (part, 已解析的 lxml 根)。

    python-docx 默认不创建脚注部件,需要在第一次写脚注前手动初始化。
    返回的根节点由调用方持有并批量追加脚注,最后一次序列化写回 ——
    此前每加一条脚注都重新解析+序列化整个 footnotes.xml,540 篇池的
    O(N²) 处理要跑几分钟(线上英文桶实测超时)。
    """
    from lxml import etree

    fn_part = None
    for rel in doc.part.rels.values():
        if rel.reltype.endswith("/footnotes"):
            fn_part = rel.target_part
            break
    if fn_part is not None:
        return fn_part, etree.fromstring(fn_part.blob)

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
        return part, etree.fromstring(part.blob)


def _add_footnote_definition(fn_root, fn_id: int, text: str) -> None:
    """向已解析的 footnotes 根节点追加一条 w:footnote(内存操作,零重复解析)。"""
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

    # 脚注正文(宋体/Times New Roman 小五 9pt)
    r1 = OxmlElement("w:r")
    rPr1 = OxmlElement("w:rPr")
    rFonts1 = OxmlElement("w:rFonts")
    rFonts1.set(qn("w:ascii"), "Times New Roman")
    rFonts1.set(qn("w:hAnsi"), "Times New Roman")
    rFonts1.set(qn("w:eastAsia"), "宋体")
    rPr1.append(rFonts1)
    sz1 = OxmlElement("w:sz")
    sz1.set(qn("w:val"), "18")  # 9pt 小五
    rPr1.append(sz1)
    r1.append(rPr1)
    t1 = OxmlElement("w:t")
    t1.text = text
    t1.set(qn("xml:space"), "preserve")
    r1.append(t1)
    p.append(r1)

    fn.append(p)
    fn_root.append(fn)


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
    refs_text: str | None = None,
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
    # 1. 参考文献清单:导出场景直接使用存储的编号清单(与正文 [N] 对齐);
    # 生成场景用 citeproc 按引用顺序渲染 [1..N]
    if not refs_text:
        refs_text = render_reference_list(papers)

    # 2. 把 references 拆成 list[FootnoteItem](逐条剥 DOI/PubMed 链接,
    #    脚注与参考文献表两出口同源,见 _strip_doi_marks 注释)
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
        fn_items.append(FootnoteItem(index=idx, text=_strip_doi_marks(body)))

    # 3. 创建 Document
    doc = Document()

    # 默认排版(学位论文规格):中文宋体、英文 Times New Roman、小四、1.5 倍行距
    style = doc.styles["Normal"]
    style.font.name = "Times New Roman"
    style.font.size = Pt(12)  # 小四
    style.element.rPr.rFonts.set(qn("w:eastAsia"), "宋体")
    style.paragraph_format.line_spacing = 1.5

    # 4. 确保 footnotes 部件存在,拿到可复用的已解析根节点
    _fn_part, fn_root = _ensure_footnotes_part(doc)

    # 5. 写入脚注定义(批量追加到内存树,最后统一序列化)
    for fn in fn_items:
        _add_footnote_definition(fn_root, fn_id=fn.index, text=fn.text)

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
            run.font.size = Pt(12)  # 小四
            run._element.get_or_add_rPr().rFonts.set(qn("w:eastAsia"), "宋体")
            # v9.6:把段落中所有 [N] 都替换为脚注引用(此前只替换第一个)
            if _replace_anchors_with_footnote_refs(p, max_index=len(fn_items)):
                body_paragraphs += 1

    # 7. 末尾添加参考文献列表段
    doc.add_heading("参考文献", level=1)
    for fn in fn_items:
        p = doc.add_paragraph()
        run = p.add_run(f"[{fn.index}] {fn.text}")
        run.font.name = "Times New Roman"
        run.font.size = Pt(12)  # 小四
        run._element.get_or_add_rPr().rFonts.set(qn("w:eastAsia"), "宋体")

    # 8. 排版兜底:全部非标题段落 1.5 倍行距;所有 run 中文宋体/西文 Times
    _apply_thesis_typography(doc)
    # 把内存中的 footnotes 树序列化写回部件
    fn_part = None
    for rel in doc.part.rels.values():
        if rel.reltype.endswith("/footnotes"):
            fn_part = rel.target_part
            break
    if fn_part is not None:
        fn_part._blob = etree.tostring(
            fn_root, xml_declaration=True, encoding="UTF-8", standalone=True
        )
    doc.save(out_path)

    return DocxBuildResult(
        out_path=out_path,
        footnote_count=len(fn_items),
        reference_count=len(fn_items),
        body_paragraphs=body_paragraphs,
    )


def _apply_thesis_typography(doc) -> None:
    """学位论文排版兜底:非标题段落 1.5 倍行距;全部 run 中文宋体、
    西文 Times New Roman、小四。标题沿用 Word 内置标题样式字号,
    但中文字体仍统一为宋体。"""
    for paragraph in doc.paragraphs:
        style_name = (paragraph.style.name or "") if paragraph.style is not None else ""
        is_heading = style_name.startswith(("Heading", "Title"))
        if not is_heading:
            paragraph.paragraph_format.line_spacing = 1.5
        for run in paragraph.runs:
            run.font.name = "Times New Roman"
            run.font.size = Pt(12)
            rPr = run._element.get_or_add_rPr()
            rFonts = rPr.rFonts
            if rFonts is None:
                rFonts = OxmlElement("w:rFonts")
                rPr.append(rFonts)
            rFonts.set(qn("w:eastAsia"), "宋体")


__all__ = ["render_docx_with_footnotes", "DocxBuildResult", "FootnoteItem"]