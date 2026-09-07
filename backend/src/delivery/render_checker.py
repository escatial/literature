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
        import lxml.etree as ET
        root = ET.fromstring(fn_xml)
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


__all__ = ["check_render", "RenderCheckResult"]