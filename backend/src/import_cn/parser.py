"""GB/T 7714-2025 期刊论文引文解析器。

支持的期刊类型格式(J 类型,占综述写作 95%):
    作者. 题名[J]. 刊名, 年, 卷(期): 起止页码.

注意:解析器只识别与切分,不构造字段。所有字段值来自用户粘贴的原文。

鲁棒性:
- 知网"查新(引文格式)"复制时常把摘要一起带过来,预处理会先截到"摘要:"之前
- 页码后可容忍跟一个孤立的"."(知网格式)
"""
from __future__ import annotations

import re

from .types import ImportedCitation

# 截断点:遇到摘要行就到此为止(常见的知网粘贴尾巴)
_TRUNCATE_RE = re.compile(
    r"\s*(?:摘\s*要\s*[:：]|【摘要】|\n摘要\s*[:：]?).*$", re.DOTALL
)

# 期刊论文核心模式:作者. 题名[J]. 刊名, 年, <残段?>
PATTERN = re.compile(
    r"^"
    r"(?P<authors>[^.\n]+?)\.\s*"             # 作者(第一个句号前)
    r"(?P<title>[^[\n]+?)\s*"                 # 题名(到 [ 之前)
    r"\[J\]\.\s*"                             # [J]. 标识符
    r"(?P<journal>[^,\n]+?)"                  # 刊名(到 , 前)
    r"\s*,\s*"                                # ,
    r"(?P<year>\d{4})\s*"                     # 4 位年份
    r"(?:"
    r":\s*(?P<rest_inline>\S+)"               # 可选 ", 年份:残段"
    r"|"
    r",\s*(?P<rest>.+?)"                      # 可选 ", 残段"
    r")?\s*\.?\s*$"                           # 结尾
    ,
    re.DOTALL,
)


def _extract_rest(m) -> str | None:
    return m.group("rest") if m.group("rest") is not None else m.group("rest_inline")


def _parse_rest(rest: str | None) -> tuple[str | None, str | None, str | None]:
    """从"37(6):227-229"这样的残段中解析 (volume, issue, pages)。"""
    if not rest:
        return None, None, None
    rest = rest.strip().rstrip(".")
    if not rest:
        return None, None, None
    # 形式1:37(6):227-229  或  37 (6):227-229  (知网常常有空格的格式)
    m = re.match(r"^(?P<v>\d+)\s*\((?P<i>\d+)\)\s*:\s*(?P<p>\S+)$", rest)
    if m:
        return m.group("v"), m.group("i"), m.group("p")
    # 形式2:37:227-229
    m = re.match(r"^(?P<v>\d+)\s*:\s*(?P<p>\S+)$", rest)
    if m:
        return m.group("v"), None, m.group("p")
    # 形式3:仅卷(纯数字)
    if re.match(r"^\d+$", rest):
        return rest, None, None
    # 形式4:仅页码(纯数字含短横)
    if re.match(r"^[-\d]+$", rest):
        return None, None, rest
    return None, None, None


def _preprocess(text: str) -> str:
    """预处理:截掉可能跟随在引文后面的摘要/关键词等尾巴。"""
    text = text.strip()
    # 知网复制带 [N] 编号,剥掉
    text = re.sub(r"^\s*\[\d+\]\s*", "", text)
    # 截到摘要之前
    text = _TRUNCATE_RE.sub("", text)
    return text.strip()


def parse_one(text: str) -> ImportedCitation:
    """解析一条 GB/T 7714-2025 [J] 类型引文。"""
    cleaned = _preprocess(text)
    m = PATTERN.match(cleaned)
    if not m:
        return ImportedCitation(
            raw_text=text,
            authors="", title="", journal="",
            year=0, volume=None, issue=None, pages=None,
            parsed_ok=False, error="无法匹配 GB/T 7714-2025 [J] 期刊格式",
        )

    volume, issue, pages = _parse_rest(_extract_rest(m))
    return ImportedCitation(
        raw_text=text,
        authors=(m.group("authors") or "").strip(),
        title=(m.group("title") or "").strip(),
        journal=(m.group("journal") or "").strip(),
        year=int(m.group("year")),
        volume=volume,
        issue=issue,
        pages=pages,
        parsed_ok=True,
        error=None,
    )


def parse_batch(lines: list[str]) -> list[ImportedCitation]:
    """批量解析,跳过空行和非引文行(如摘要、空白)。"""
    out: list[ImportedCitation] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        # 跳过非引文行:摘要/关键词/作者简介等(不含 [J] 标记)
        if "[J]" not in line and "[M]" not in line and "[D]" not in line and "[C]" not in line:
            continue
        out.append(parse_one(line))
    return out