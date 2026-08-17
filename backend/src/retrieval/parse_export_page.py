"""知网导出页解析：从粘贴的 HTML 抽取引文 + 摘要。

v4.0 引入。纯函数实现,不依赖 Playwright 或浏览器。
用于"测试当前配置"功能:用户粘贴 HTML -> 后端用 selector 抽取 -> 立即验证配置。
"""
from __future__ import annotations

import re
from typing import Any

from bs4 import BeautifulSoup


INDEX_PATTERN = re.compile(r"^\[\d+\]$")

# v3.0 已验证的 selector 默认值(知网"导出文献"页 GB/T 7714-2025 格式)
DEFAULT_SELECTORS: dict[str, str] = {
    "quote_item": "li:has(span.index)",
    "index_marker": "span.index",
    "quote_split_text": "摘要:",
}


def parse_li_text(text: str, split_text: str = "摘要:") -> dict[str, Any]:
    """从单个条目的纯文本切分 (quote, abstract)。

    Args:
        text: 单条引文的纯文本(含序号 + 引文 + 摘要)
        split_text: 摘要截取符(默认"摘要:",自动 fallback 到"摘要：")

    Returns:
        {"quote": str, "abstract": str | None}
    """
    text = re.sub(r"\s+", " ", text).strip()

    cut = text.find(split_text)
    split_len = len(split_text)
    if cut < 0:
        for fallback in ("摘要:", "摘要："):
            if fallback == split_text:
                continue
            cut = text.find(fallback)
            if cut >= 0:
                split_len = len(fallback)
                break

    if cut < 0:
        return {"quote": text, "abstract": None}

    abstract = text[cut + split_len:]
    if split_text in ("摘要:", "摘要："):
        abstract = abstract.strip()
    return {
        "quote": text[:cut].strip(),
        "abstract": abstract or None,
    }


def parse_export_page_html(
    html: str,
    selectors: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """从知网导出页 HTML 抽取全部 {idx, quote, abstract}。

    纯函数,不依赖 Playwright。可用于:
    1. "测试当前配置":用户粘贴 HTML,立即验证 selector 是否正确
    2. 后端离线测试抽取逻辑
    3. 浏览器自动化失败时的 fallback

    Args:
        html: 完整的导出页 HTML(用户从浏览器"另存为"或"查看源代码"复制)
        selectors: 用户配置(默认 v3.0 已验证的 selector)
            {
                "quote_item": "li:has(span.index)",      # 条目容器
                "index_marker": "span.index",            # 序号元素
                "quote_split_text": "摘要:",             # 摘要截取符
            }

    Returns:
        list[dict]: 每条 {idx: "[N]", quote: "GB/T 7714 引文", abstract: "摘要"}
        idx 为 None 表示没找到序号 span
    """
    if not html or not html.strip():
        return []

    cfg = {**DEFAULT_SELECTORS, **(selectors or {})}

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return []

    try:
        quote_items = soup.select(cfg["quote_item"])
    except Exception:
        return []

    if not quote_items:
        return []

    items: list[dict[str, Any]] = []
    for li in quote_items:
        try:
            idx_el = li.select_one(cfg["index_marker"])
        except Exception:
            idx_el = None
        idx = idx_el.get_text(strip=True) if idx_el else None
        if idx and not INDEX_PATTERN.match(idx):
            idx = None

        text = li.get_text(separator=" ", strip=True)
        text = re.sub(r"\s+", " ", text)

        content_without_index = text[len(idx or ""):].strip()
        if not text or (idx is not None and len(content_without_index) < 4):
            continue

        parsed = parse_li_text(text, cfg.get("quote_split_text", "摘要:"))
        parsed["quote"] = re.sub(r"^(\[\d+\])\s+", r"\1", parsed["quote"])

        items.append({
            "idx": idx,
            "quote": parsed["quote"],
            "abstract": parsed["abstract"],
        })

    return items


def extract_index(text: str) -> str | None:
    """从文本开头提取 [N] 序号(如 '[1]')"""
    text = text.strip()
    m = re.match(r"^\[(\d+)\]", text)
    return f"[{m.group(1)}]" if m else None
