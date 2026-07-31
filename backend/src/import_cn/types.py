"""中文导入相关类型。"""
from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass
class ImportedCitation:
    """用户批量粘贴 GB/T 7714 引文,解析后归一。

    所有字段值都来自用户粘贴的原文(或解析切分后的子串),
    工具不构造任何字段。
    """
    raw_text: str
    authors: str
    title: str
    journal: str
    year: int
    volume: str | None
    issue: str | None
    pages: str | None
    parsed_ok: bool
    error: str | None

    def to_dict(self) -> dict:
        return asdict(self)
