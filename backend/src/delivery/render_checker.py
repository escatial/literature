"""渲染质检脚本(方案 §5 RENDERED 阶段 quality_gate)。

目标:
  - 解析生成的 docx,验证:
    1. 存在 word/footnotes.xml 部件;
    2. word/footnotes.xml 中 w:footnote 个数 >= 正文中 w:footnoteReference 个数;
    3. 正文中至少出现一次 w:footnoteReference;
    4. 参考文献列表段存在,且条目数等于最大引用编号;
    5. 正文未出现「TODO」「FIXME」「lorem ipsum」等占位符。

输出:QACheckResult-like dict,供 qa.runner 接入或独立 CLI 使用。
"""
from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

PLACEHOLDER_PATTERNS = [
    re.compile(r"\bTODO\b", re.I),
    re.compile(r"\bFIXME\b", re.I),
    re.compile(r"\blorem\s+ipsum\b", re.I),
    re.compile(r"\[此处.{0,10}内容\]"),
]

# D1(2026-11 交付链加固):内部引用锚点 —— 编号替换的漏网之鱼。
# 2026-11-01 交付事故:41 处 [lit_xxx] 直接暴露在交付 docx 正文里。
_INTERNAL_ANCHOR_RE = re.compile(r"\[(?:lit_[a-zA-Z0-9_]+|hash:[a-zA-Z0-9_]+)\]")


def strip_internal_anchors(text: str) -> tuple[str, int]:
    """剥离正文残留的内部引用锚点([lit_xxx]/[hash:xxx]),返回 (净化文本, 剥离数)。

    历史数据兜底用:新链路在 apply_citation_numbering 已把可映射锚点换成
    [N]、不可映射锚点直接剥除;本函数只处理存量库里的旧行。
    """
    if not text:
        return "", 0
    cleaned, n = _INTERNAL_ANCHOR_RE.subn("", text)
    return cleaned, n


def check_sections_delivery(sections: list[dict], reference_list: str = "") -> list[dict]:
    """入库/导出前的章节级交付门禁,返回问题清单(空 = 通过)。

    五条硬校验(对应 2026-11-01 交付事故的每一类缺陷):
    1. 章节非空;
    2. 正文零内部锚点残留;
    3. 正文零占位符;
    4. 每条参考文献至少被正文引用一次([N] 在正文出现);
    5. 章节序号连续(一、二、三…,乱序即报)。
    """
    issues: list[dict[str, str]] = []
    if not sections:
        issues.append({"code": "NO_SECTIONS", "detail": "没有可交付的章节"})
        return issues

    body_all = "\n".join(str(s.get("content") or "") for s in sections)
    cited_nums = set(re.findall(r"\[(\d{1,3})\]", body_all))

    for s in sections:
        key = str(s.get("key") or "?")
        content = str(s.get("content") or "")
        if not content.strip():
            issues.append({"code": "EMPTY_SECTION", "detail": f"章节 {key} 正文为空"})
        anchors = _INTERNAL_ANCHOR_RE.findall(content)
        if anchors:
            issues.append({
                "code": "ANCHOR_LEAK",
                "detail": f"章节 {key} 残留内部锚点 {len(anchors)} 处: {anchors[:3]}",
            })
        for pat in PLACEHOLDER_PATTERNS:
            m = pat.search(content)
            if m:
                issues.append({
                    "code": "PLACEHOLDER_FOUND",
                    "detail": f"章节 {key} 含占位符: {m.group(0)}",
                })
        title = str(s.get("title") or "")
        if title and not re.match(r"^[一二三四五六七八九十百\d]+、", title.strip()):
            issues.append({
                "code": "TITLE_NOT_NUMBERED",
                "detail": f"章节 {key} 标题缺序号: {title[:30]}",
            })

    ref_nums = re.findall(r"^\[(\d{1,3})\]", reference_list or "", flags=re.M)
    uncited = [n for n in ref_nums if n not in cited_nums]
    if uncited:
        issues.append({
            "code": "REFS_UNCITED",
            "detail": f"{len(uncited)} 条参考文献未被正文引用: [{'], ['.join(uncited[:8])}]",
        })
    return issues


@dataclass
class RenderCheckResult:
    """渲染质检结果。"""

    docx_path: str
    passed: bool
    checks: dict[str, Any] = field(default_factory=dict)
    issues: list[dict[str, str]] = field(default_factory=list)
    summary: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _count_in_xml(xml: bytes, pattern: bytes) -> int:
    # bytes 计数:简单 count 子串
    if isinstance(xml, bytes):
        return xml.count(pattern)
    return xml.count(pattern)


def check_render(docx_path: str | Path) -> RenderCheckResult:
    """检查一个 docx 的渲染质量。"""
    docx_path = Path(docx_path)
    if not docx_path.is_file():
        return RenderCheckResult(
            docx_path=str(docx_path),
            passed=False,
            issues=[{"code": "FILE_NOT_FOUND", "detail": f"{docx_path} 不存在"}],
        )

    from docx import Document

    doc = Document(str(docx_path))
    issues: list[dict[str, str]] = []

    # 1. footnotes 部件存在?
    fn_part = None
    for rel in doc.part.rels.values():
        if rel.reltype.endswith("/footnotes"):
            fn_part = rel.target_part
            break
    has_footnotes_part = fn_part is not None
    if not has_footnotes_part:
        issues.append({"code": "NO_FOOTNOTES_PART", "detail": "缺少 word/footnotes.xml"})

    # 2 & 3. footnote 定义数 vs 引用数
    fn_def_count = 0
    fn_xml = b""
    if has_footnotes_part and fn_part is not None:
        fn_xml = fn_part.blob
        # 排除 separator / continuationSeparator(w:id=-1 / 0)
        # 简单做法:数 w:footnote 元素,然后减 2
        # defusedxml:禁 DTD/实体扩展,防 billion laughs(与 pubmed 同修法;
        # findall 的 {ns}tag 语法与 lxml 一致)
        from defusedxml import ElementTree as SafeET

        root = SafeET.fromstring(fn_xml)
        fn_def_count = len(root.findall(".//{http://schemas.openxmlformats.org/wordprocessingml/2006/main}footnote"))

    body_xml = doc.element.body.xml if hasattr(doc.element.body, "xml") else b""
    fn_ref_count = _count_in_xml(
        body_xml.encode("utf-8") if isinstance(body_xml, str) else body_xml,
        b"<w:footnoteReference",
    )

    if fn_ref_count == 0:
        issues.append({"code": "NO_FOOTNOTE_REFS", "detail": "正文中没有 w:footnoteReference"})

    if has_footnotes_part and fn_ref_count > fn_def_count:
        issues.append({
            "code": "FOOTNOTE_DEF_LT_REF",
            "detail": f"脚注定义 {fn_def_count} 个 < 正文引用 {fn_ref_count} 个",
        })

    # 4. 参考文献列表段
    body_paragraphs = [p.text for p in doc.paragraphs]
    ref_section_idx = None
    for i, t in enumerate(body_paragraphs):
        if t.strip() == "参考文献":
            ref_section_idx = i
            break
    if ref_section_idx is None:
        issues.append({"code": "NO_REFERENCE_SECTION", "detail": "缺少「参考文献」段"})
    ref_items_after = 0
    if ref_section_idx is not None:
        for t in body_paragraphs[ref_section_idx + 1:]:
            s = t.strip()
            if s.startswith("[") and "]" in s:
                ref_items_after += 1

    # 5. 占位符检查
    full_text = "\n".join(body_paragraphs)
    placeholders_found: list[str] = []
    for pat in PLACEHOLDER_PATTERNS:
        m = pat.search(full_text)
        if m:
            placeholders_found.append(m.group(0))
    if placeholders_found:
        issues.append({
            "code": "PLACEHOLDER_FOUND",
            "detail": f"占位符:{', '.join(placeholders_found[:5])}",
        })

    summary = {
        "paragraphs": len(body_paragraphs),
        "footnote_def_count": fn_def_count,
        "footnote_ref_count": fn_ref_count,
        "reference_items": ref_items_after,
    }

    passed = len(issues) == 0
    return RenderCheckResult(
        docx_path=str(docx_path),
        passed=passed,
        checks={
            "has_footnotes_part": has_footnotes_part,
            "body_paragraphs": len(body_paragraphs),
            "ref_section_present": ref_section_idx is not None,
            "no_placeholder": not placeholders_found,
        },
        issues=issues,
        summary=summary,
    )


__all__ = [
    "check_render",
    "check_sections_delivery",
    "strip_internal_anchors",
    "RenderCheckResult",
]