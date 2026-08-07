"""从数据库检索结果 HTML 中提取文献条目(供 remote browser 检索后入库用)。

设计原则:不做复杂 DOM 解析,只匹配常见文本模式,方便在已知数据库中复用。
提取失败也不报错,返回空列表;前端可以提示用户改用「粘贴引文文本」导入。
"""
from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup  # type: ignore

log = logging.getLogger(__name__)


def extract_papers_from_html(html: str, base_url: str = "", db_type: str = "cnki") -> list[dict[str, Any]]:
    """从数据库结果页 HTML 中抽出文献条目。

    返回结构(paper dict,与后端入库结构一致):
        {lit_id, source, title, authors[], journal, year, source_url, doi?}
    """
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    extractor = _EXTRACTORS.get(db_type, _extract_generic)
    try:
        raw_items = extractor(soup)
    except Exception as e:
        log.warning("提取器崩溃(%s):%s", db_type, e)
        return []

    items = []
    for raw in raw_items:
        normalised = _normalise(raw, base_url)
        if normalised:
            items.append(normalised)
    return items


def make_lit_id(prefix: str, title: str, authors: list[str], year: int | None) -> str:
    """为浏览器检索出的文献生成稳定 lit_id(无 DOI 时使用)。"""
    import hashlib
    raw = f"{title}|{','.join(authors[:2])}|{year or 0}"
    h = hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]
    return f"lit_{prefix}_{h}"


# ─── 各数据库的具体抽取逻辑 ───────────────────────────────

def _extract_cnki(soup: BeautifulSoup) -> list[dict]:
    """知网:综合多套前端框架的选择器,挑题目年份组合作为条目。"""
    items = []
    seen = set()
    # 选择器覆盖 KNB/新版/旧版三种结构
    selectors = [
        "tr.result-table",
        "tr.gray",
        "div.result",
        "div.literature_item",
        "div.docListView",
        "li.literatureItem",
        "li.list_item",
        "div.list_item",
    ]
    for sel in selectors:
        for box in soup.select(sel):
            a = box.select_one("a.fz14, a.lk_biao, a.lk_fz14, a.title, a[href*='detail'], a[href*='KNS']")
            if not a:
                a = box.select_one("a")
            title = a.get_text(strip=True) if a else ""
            if not title or len(title) < 6 or title in seen:
                continue
            year = _extract_year(box)
            if not year:
                continue
            seen.add(title)
            items.append({
                "title": title,
                "authors": _extract_authors(box),
                "journal": _extract_field(box, ["journal", "刊物", "source"]) or "",
                "year": year,
                "source_url": a.get("href", "") if a else "",
            })
            if len(items) >= 50:
                return items
    return items


def _extract_year(node) -> int | None:
    text = node.get_text(" ", strip=True)
    m = re.search(r"\b(19|20)\d{2}\b", text)
    return int(m.group(0)) if m else None


def _extract_field(node, keys: list[str]) -> str | None:
    """按 css 类名关键词或文本片段提取字段。"""
    for k in keys:
        sel = node.select_one(f".{k}, [class*='{k}'], [class*='{k.lower()}']")
        if sel:
            return sel.get_text(strip=True)
    return None


def _extract_cqvip(soup: BeautifulSoup) -> list[dict]:
    """维普:条目在 .article-list / .item,标题在 a.title。"""
    items = []
    for a in soup.select("a.title, .title a, .article-list a"):
        title = a.get_text(strip=True)
        if not title or len(title) < 4:
            continue
        li = a.find_parent("li") or a.find_parent("div")
        text = li.get_text(" ", strip=True) if li else title
        year = None
        ym = re.search(r"\b(19|20)\d{2}\b", text)
        if ym:
            year = int(ym.group(0))
        items.append({
            "title": title,
            "authors": _extract_authors(li),
            "journal": "",
            "year": year,
            "source_url": a.get("href", ""),
        })
    return items[:50]


def _extract_wanfang(soup: BeautifulSoup) -> list[dict]:
    """万方:条目在 .paper-item / .clearfix。"""
    items = []
    for li in soup.select(".paper-item, .normal-list li, .search-result-list li"):
        title_el = li.select_one(".title, .tit, a")
        if not title_el:
            continue
        title = title_el.get_text(strip=True)
        text = li.get_text(" ", strip=True)
        year = None
        ym = re.search(r"\b(19|20)\d{2}\b", text)
        if ym:
            year = int(ym.group(0))
        items.append({
            "title": title,
            "authors": _extract_authors(li),
            "journal": "",
            "year": year,
            "source_url": title_el.get("href", "") if title_el.name == "a" else "",
        })
    return items[:50]


def _extract_generic(soup: BeautifulSoup) -> list[dict]:
    """兜底:扫描所有 a 标签,挑带年份的链接当候选(粗粒度)。"""
    items = []
    for a in soup.find_all("a"):
        title = a.get_text(strip=True)
        if len(title) < 4 or len(title) > 200:
            continue
        parent = a.find_parent(["li", "div", "tr", "article", "section"])
        text = parent.get_text(" ", strip=True) if parent else title
        ym = re.search(r"\b(19|20)\d{2}\b", text)
        if not ym:
            continue
        items.append({
            "title": title,
            "authors": _extract_authors(parent),
            "journal": "",
            "year": int(ym.group(0)),
            "source_url": a.get("href", ""),
        })
    return items[:50]


def _extract_authors(container) -> list[str]:
    if container is None:
        return []
    for a in container.select(".author, .authors, .writer, .au, span[class*='author'], span[class*='writer']"):
        text = a.get_text(" ", strip=True)
        # 作者分隔常是 `,;、` 之一
        parts = [p.strip() for p in re.split(r"[,;、（）()]+", text) if p.strip() and len(p.strip()) < 30]
        return parts[:10]
    return []


# ─── 归一化与入库适配 ───────────────────────────────

def _normalise(raw: dict, base_url: str) -> dict | None:
    title = (raw.get("title") or "").strip()
    if not title or len(title) < 4:
        return None
    return {
        "title": title,
        "authors": raw.get("authors") or [],
        "journal": raw.get("journal") or "",
        "year": int(raw.get("year") or 0) or None,
        "source_url": urljoin(base_url, raw.get("source_url") or "") if raw.get("source_url") else "",
    }


_EXTRACTORS = {
    "cnki": _extract_cnki,
    "cqvip": _extract_cqvip,
    "wanfang": _extract_wanfang,
}
