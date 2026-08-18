"""CNKI 数据源实现 —— 基于嵌入的 HTTP 爬虫(automation/cnki,不再依赖 Playwright)。

设计:
- 包一层,不重写:adapter 直接驱动嵌入爬虫的列表/摘要/验证码链路;
- build_sub_query 透传字符串(LLM 给的 3 条之一);
- execute_async 通过 asyncio.Queue 桥接 adapter 的事件流,完成后从 DB 读回本次入库结果。
"""
from __future__ import annotations

import asyncio
import logging
import os

from automation.cnki_adapter import run_cnki_full_auto
from retrieval.sources.base import AcademicSource, SourcePage
from retrieval.types import Paper, Source

log = logging.getLogger(__name__)


class CNKISource:
    """CNKI 数据源:HTTP 爬虫版,验证码由超级鹰自动接管。

    凭据来自包内 config.yaml 或 CJY_USER/CJY_PASS/CJY_SOFT_ID 环境变量。
    """
    name = "cnki"

    def __init__(self, soft_id: str | None = None, max_pages: int = 10):
        self.soft_id = soft_id or os.getenv("CJY_SOFT_ID", os.getenv("CHAOJIYING_SOFT_ID", ""))
        self.max_pages = max_pages

    # === AcademicSource 协议 ===

    def build_sub_query(self, query_string: str) -> dict:
        return {"query": query_string}

    def execute(self, query: dict, page: int, per_page: int) -> SourcePage:
        """CNKI 不分 API 页,走 execute_async 流。"""
        raise NotImplementedError(
            "CNKISource.execute 走 execute_async 流,请用 RetrievalController.run_async"
        )

    async def execute_async(self, query_string: str, topic: str,
                            on_event=None) -> list[Paper]:
        """异步跑 CNKI 爬虫,返回本次入库的 Paper 列表。"""
        queue: asyncio.Queue = asyncio.Queue()

        async def _emit_bridge():
            while True:
                evt = await queue.get()
                if evt is None:
                    return
                if on_event:
                    try:
                        on_event(evt)
                    except Exception as e:
                        log.warning("CNKI 事件回调失败: %s", e)

        bridge = asyncio.create_task(_emit_bridge())
        try:
            result = await run_cnki_full_auto(
                topic=topic,
                queue=queue,
                max_pages=self.max_pages,
                db_type="cnki",
            )
            log.info("CNKI 检索结束: %s", result)
        finally:
            await queue.put(None)
        await bridge

        # 从 DB 读最近 10 分钟内入库的 CNKI paper,作为本次检索产物
        return self._load_recent_from_db()

    def fetch_abstract_if_missing(self, paper: Paper) -> Paper | None:
        """爬虫抓取时已带摘要;无摘要视为不可补全。"""
        return paper if paper.abstract else None

    def fetch_references(self, paper: Paper, depth: int = 1) -> list[Paper]:
        return []

    def health_check(self) -> bool:
        try:
            from automation.cnki_adapter import check_cookies_health
            return check_cookies_health().get("ok", False)
        except Exception:
            return False

    # === 内部 ===

    def _load_recent_from_db(self) -> list[Paper]:
        """从 DB 读最近 10 分钟内入库的 CNKI paper,作为本次检索产物。"""
        from datetime import datetime, timedelta, timezone

        from db.models import PaperModel
        from db.session import SessionLocal

        cutoff = datetime.now(timezone.utc) - timedelta(minutes=10)
        with SessionLocal() as db:
            rows = (
                db.query(PaperModel)
                .filter(PaperModel.source == "cnki")
                .filter(PaperModel.created_at >= cutoff)
                .all()
            )
            return [
                Paper(
                    lit_id=r.lit_id, source=Source(r.source),
                    title=r.title, authors=r.authors or [],
                    journal=r.journal or "", year=r.year or 0,
                    volume=r.volume, issue=r.issue, pages=r.pages,
                    abstract=r.abstract_text or r.abstract,
                    doi=r.doi, source_url=r.source_url or "",
                    cited_by_count=r.cited_by_count or 0,
                )
                for r in rows
            ]


__all__ = ["CNKISource"]
