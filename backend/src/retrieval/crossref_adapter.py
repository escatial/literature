"""CrossRef 适配器。仅作为:
1) OpenAlex 缺字段时的二次拿字段
2) DOI 反查论文完整元数据

只读取平台字段,绝不构造任何字段。
"""
from __future__ import annotations

import logging
import os

import httpx

from .types import Paper, Source
from .openalex_adapter import _make_lit_id

log = logging.getLogger(__name__)

DEFAULT_MAILTO = os.getenv("OPENALEX_MAILTO", "your-email@example.com")


class CrossRefAdapter:
    BASE = "https://api.crossref.org/works"

    def __init__(self, mailto: str | None = None, timeout: float = 20.0):
        self.mailto = mailto or DEFAULT_MAILTO
        self.timeout = timeout

    def by_doi(self, doi: str) -> Paper | None:
        """按 DOI 取一条论文。返回 None 表示未找到或请求失败。"""
        if not doi:
            return None
        url = f"{self.BASE}/{doi}"
        params = {"mailto": self.mailto}
        with httpx.Client(timeout=self.timeout) as client:
            try:
                resp = client.get(url, params=params)
                if resp.status_code == 404:
                    return None
                resp.raise_for_status()
                return self._parse(resp.json().get("message", {}))
            except httpx.HTTPError as e:
                log.warning("CrossRef 失败: %s", e)
                return None

    def _parse(self, item: dict) -> Paper:
        """只读字段,缺字段保持 None。"""
        title = (item.get("title") or [""])[0]
        # CrossRef 作者列表是 {family, given} 结构
        authors = [
            f"{a.get('family', '')}, {a.get('given', '')}".strip(", ")
            for a in item.get("author", [])
        ]

        # 年份可能在 issued.date-parts[0][0]
        issued = item.get("issued") or {}
        date_parts = issued.get("date-parts") or [[None]]
        year_raw = date_parts[0][0] if date_parts and date_parts[0] else None
        try:
            year = int(year_raw) if year_raw else 0
        except (ValueError, TypeError):
            year = 0

        # 期刊层级不在 CrossRef 标准字段里,留 None 让 CrossRef 查询
        # 后续可以挂 CrossRef API 的 funder/publisher 等信息

        return Paper(
            lit_id=_make_lit_id(title, item.get("DOI")),
            source=Source.CROSSREF,
            title=title,
            authors=authors,
            journal=(item.get("container-title") or [""])[0],
            year=year,
            volume=item.get("volume"),
            issue=item.get("issue"),
            pages=item.get("page"),
            doi=item.get("DOI"),
            source_url=item.get("URL", ""),
        )
