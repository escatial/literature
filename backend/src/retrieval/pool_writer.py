"""文献池写入服务(需求3)。

策略:检索完成时按来源先清空同源历史数据再写入新结果,确保文献池
数据与本次检索结果严格一致;中文/英文分别处理。

来源分组约定:
- 中文:`cnki`(知网自动抓取)
- 英文:`openalex` / `pubmed`
- 手动导入:`user_imported`(需求2 已全站移除,无新增路径,但已存在数据保留)

按 selected 批量 upsert 写入,所有新条目默认 selected=True。
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Iterable

from sqlalchemy import select

from db.models import PaperModel
import db.session as _db_session
from retrieval.provenance import validate_paper_provenance
from retrieval.types import Paper


log = logging.getLogger(__name__)


# 来源分组:检索来源 → 同源待清空/写入的 source 标识列表
_SOURCE_GROUPS: dict[str, list[str]] = {
    "openalex": ["openalex"],
    "pubmed": ["pubmed"],
    "cnki": ["cnki"],
}


def _group_sources(sources: Iterable[str]) -> list[str]:
    """把传入的源展开为「需要清空 + 写入」的 source 列表(去重)。"""
    out: list[str] = []
    for src in sources:
        out.extend(_SOURCE_GROUPS.get(src, [src]))
    # 保序去重
    seen: set[str] = set()
    dedup: list[str] = []
    for s in out:
        if s not in seen:
            seen.add(s)
            dedup.append(s)
    return dedup


def upsert_with_overwrite(
    papers: list[Paper],
    *,
    sources: Iterable[str],
) -> dict[str, int]:
    """先按 sources 清空同源历史,再写入 papers。

    返回 {"cleared": N, "inserted": N, "updated": N, "failed": N},
    仅统计本次操作。
    """
    targets = _group_sources(sources)
    if not targets:
        log.warning("upsert_with_overwrite 未指定来源,拒绝写入以避免误清空")
        return {"cleared": 0, "inserted": 0, "updated": 0, "failed": 0}

    cleared = 0
    inserted = 0
    updated = 0
    failed = 0

    with _db_session.SessionLocal() as db:
        # 1) 清空同源历史
        for src in targets:
            stmt = select(PaperModel).where(PaperModel.source == src)
            rows = list(db.execute(stmt).scalars().all())
            for r in rows:
                db.delete(r)
            cleared += len(rows)
        db.flush()

        # 2) 写入新结果(按 lit_id 幂等)
        # 同一任务内部可能有重复 lit_id(跨源合并去重后),按 lit_id 二次去重
        seen: set[str] = set()
        unique: list[Paper] = []
        for p in papers:
            if p.lit_id in seen:
                continue
            seen.add(p.lit_id)
            unique.append(p)

        for p in unique:
            try:
                src_value = str(p.source.value if hasattr(p.source, "value") else p.source)
                validate_paper_provenance(src_value, p.lit_id, p.source_url)
                meta = {
                    k: v for k, v in p.to_dict().items()
                    if k not in ("lit_id", "created_at", "selected")
                }
                existing = db.get(PaperModel, p.lit_id)
                if existing:
                    for k, v in meta.items():
                        setattr(existing, k, v)
                    updated += 1
                else:
                    db.add(PaperModel(lit_id=p.lit_id, selected=True, **meta))
                    inserted += 1
            except Exception as exc:
                log.warning("写入文献失败 lit_id=%s: %s", p.lit_id, exc)
                failed += 1
        db.commit()

    log.info(
        "upsert_with_overwrite sources=%s cleared=%d inserted=%d updated=%d failed=%d",
        targets, cleared, inserted, updated, failed,
    )
    return {"cleared": cleared, "inserted": inserted, "updated": updated, "failed": failed}


def split_by_source(papers: list[Paper]) -> dict[str, list[Paper]]:
    """按 source 分组(供前端展示各源贡献)。"""
    out: dict[str, list[Paper]] = defaultdict(list)
    for p in papers:
        src = str(p.source.value if hasattr(p.source, "value") else p.source)
        out[src].append(p)
    return dict(out)


__all__ = ["upsert_with_overwrite", "split_by_source"]