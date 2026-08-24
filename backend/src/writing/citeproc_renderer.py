"""GB/T 7714-2025 引文渲染器(用 citeproc-py 规则引擎,不是字段拼装)。

输入:Paper 对象(title / authors / journal / year / DOI / volume / issue / pages)
输出:GB/T 7714-2025 字符串(顺序编码制 / 著者-出版年制)
- 不是 Python 字段拼接,是按 .csl 模板规则填充。
- 数据来源:OpenAlex / PubMed 真实 API,LLM 零介入。
- 单条渲染只输出"无编号"条目(剥离 CSL 样式自带的 [N]),编号由
  render_reference_list 统一按引用顺序分配,避免逐条渲染时编号全部为 [1]。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from citeproc import CitationStylesStyle, CitationStylesBibliography
from citeproc import Citation, CitationItem, formatter as _csl_formatter
from citeproc.source.json import CiteProcJSON

from retrieval.types import Paper, Source

# CSL 顺序编码制样式在 bibliography 条目前输出 [N];单条渲染时编号恒为 1,
# 这里剥掉,统一交给 render_reference_list 按引用顺序编号。
_CITE_NUM_PREFIX_RE = re.compile(r"^\[\d+\]\s*")


# 真实路径:backend/csl_styles/(从 backend/src/writing/citeproc_renderer.py 向上两级)
_CSL_STYLES_DIR = Path(__file__).resolve().parent.parent.parent / "csl_styles"

# style 加载缓存(避免每个 paper 重新 parse XML)
_STYLE_CACHE: dict[str, "CitationStylesStyle"] = {}


def _get_style(style_id: str) -> "CitationStylesStyle":
    """加载并缓存 GB/T 7714-2025 / 2025 CSL style。

    style_id 形如 "china-national-standard-gb-t-7714-2025-numeric"。
    """
    if style_id in _STYLE_CACHE:
        return _STYLE_CACHE[style_id]
    path = _CSL_STYLES_DIR / f"{style_id}.csl"
    if not path.is_file():
        raise FileNotFoundError(
            f"CSL style 文件不存在: {path}\n"
            f"请把 GB/T 7714 的 .csl 放到 backend/csl_styles/ 目录"
        )
    st = CitationStylesStyle(str(style_path := path), validate=False)
    _STYLE_CACHE[style_id] = st
    return st


def _parse_author_name(name: str) -> dict:
    """'Jingjing Sun' -> {family: 'Sun', given: 'Jingjing'}

    CSL 要求拆 family / given。英文按最后一个空格拆;中文整段作 family。
    """
    name = (name or "").strip()
    if not name:
        return {"family": ""}
    if " " not in name:
        return {"family": name}
    parts = name.rsplit(" ", 1)
    return {"family": parts[1], "given": parts[0]}


def paper_to_csl_item(paper: Paper) -> dict:
    """Paper 对象 -> CSL JSON item dict(citeproc-py 输入格式)。

    字段名是 CSL 标准,这里只做"数据格式翻译",不做字符串拼接。
    """
    item: dict = {
        "id": paper.lit_id,
        "type": "article-journal",
        "title": paper.title or "",
    }
    authors = [_parse_author_name(a) for a in (paper.authors or []) if a]
    if authors:
        item["author"] = authors
    if paper.year and paper.year > 0:
        item["issued"] = {"date-parts": [[int(paper.year)]]}
    if paper.journal:
        item["container-title"] = paper.journal
    if paper.volume:
        item["volume"] = paper.volume
    if paper.issue:
        item["issue"] = paper.issue
    if paper.pages:
        item["page"] = paper.pages
    if paper.doi:
        item["DOI"] = paper.doi
    # OpenAlex URL is provenance, not the publication access URL; keep DOI only.
    if paper.source_url and paper.source != Source.OPENALEX:
        item["URL"] = paper.source_url
    return item


def format_citation_via_citeproc(
    paper: Paper,
    style_id: str = "china-national-standard-gb-t-7714-2025-numeric",
) -> str:
    """Paper -> GB/T 7714 字符串(citeproc-py 规则引擎按 .csl 模板渲染)。

    返回无编号条目(剥离 [N] 前缀),编号由 render_reference_list 统一分配,
    保证全文参考文献编号连续且与正文数字锚点一一对应。
    """
    csl_item = paper_to_csl_item(paper)
    style = _get_style(style_id)
    bib_source = CiteProcJSON([csl_item])
    bib = CitationStylesBibliography(style, bib_source, _csl_formatter.plain)
    cite = Citation([CitationItem(csl_item["id"])])
    bib.register(cite)
    items = bib.bibliography()
    # citeproc-py 的 plain formatter 在 lxml 上返回 MixedString(继承 str 但带
    # __html__),正则不能直接处理它,先强制转回纯 str。
    rendered = str(items[0]) if items else ""
    return _CITE_NUM_PREFIX_RE.sub("", rendered).strip()


def format_citation_safe(
    paper: Paper,
    style_id: str = "china-national-standard-gb-t-7714-2025-numeric",
) -> Optional[str]:
    """兼容旧调用方的安全包装;失败时返回 None,不生成拼接引文。"""
    try:
        return format_citation_via_citeproc(paper, style_id=style_id)
    except Exception:
        return None




