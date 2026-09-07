"""delivery 包初始化。"""
from delivery.docx_renderer import (
    DocxBuildResult,
    FootnoteItem,
    render_docx_with_footnotes,
)

__all__ = ["render_docx_with_footnotes", "DocxBuildResult", "FootnoteItem"]