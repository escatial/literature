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
from typing import Any, Iterable

from sqlalchemy import select

from db.models import PaperModel
import db.session as _db_session
from retrieval.provenance import validate_paper_provenance
from retrieval.paper_identity import (
    build_identity_key,
    repair_paper_fields,
    validate_paper_identity,
)
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
    pool_task_id: str | None = None,
) -> dict[str, Any]:
    """先按 sources 清空同源历史,再写入 papers。

    v7.1 修复:pool_task_id(前端 X-Task-Id 隔离 ID)非空时,
    - 清空只删「同源且同 task」的历史,不跨任务误删;
    - 写入/更新的每条 paper 都打上 task_id 标签,
      否则文献池按 X-Task-Id 过滤时会显示 0 条。
    pool_task_id 为 None 时保持旧行为(清全部同源,写入不带标签)。

    返回 {"cleared": N, "inserted": N, "updated": N, "failed": N,
          "failed_by_source": {source: N}},
    仅统计本次操作。failed_by_source 供异常源按源展示,
    避免上层把合计失败数复制给每个源(显示 openalex: 10, pubmed: 10 的假象)。
    """
    targets = _group_sources(sources)
    if not targets:
        log.warning("upsert_with_overwrite 未指定来源,拒绝写入以避免误清空")
        return {"cleared": 0, "inserted": 0, "updated": 0, "failed": 0,
                "failed_by_source": {}}
    if not pool_task_id:
        # 防线:pool_task_id 为空说明前端请求没带 X-Task-Id(拦截器断链/旧构建),
        # 写入的文献不挂任务,文献池按任务过滤会显示 0 条 —— 第一时间在日志暴露。
        log.warning(
            "upsert_with_overwrite: pool_task_id 为空(前端未带 X-Task-Id),"
            "本次 %d 篇将不挂任务,文献池会显示 0 条;请检查前端构建与会话链路",
            len(papers),
        )

    cleared = 0
    inserted = 0
    updated = 0
    failed = 0
    # 按源统计写入失败数(校验不通过/落库异常),供上层异常源展示
    failed_by_source: dict[str, int] = defaultdict(int)
    # v9.6:按丢弃原因分类计数——此前只有逐条 warning,任务报告数与实际入库数
    # 不一致时(如无摘要文献被 validate_paper_identity 拒收)用户无从知道差在哪
    failed_reasons: dict[str, int] = defaultdict(int)

    with _db_session.SessionLocal() as db:
        # 1) 清空同源历史(按任务隔离:只清当前 task 的同源数据)
        for src in targets:
            stmt = select(PaperModel).where(PaperModel.source == src)
            if pool_task_id:
                stmt = stmt.where(PaperModel.task_id == pool_task_id)
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

        # v9.6:autoflush=False 下 db.query 查不到本批 pending insert,
        # 同批重复 identity_key 会误走插入分支,靠唯一索引兜底失败即整批
        # 回滚(当次检索全丢)。改用本批内存索引先行判重。
        batch_identity: dict[str, PaperModel] = {}
        for p in unique:
            # source 值在 try 外解析,保证 except 分支也能按源计数
            src_value = str(p.source.value if hasattr(p.source, "value") else p.source)
            try:
                validate_paper_provenance(src_value, p.lit_id, p.source_url)
                meta = repair_paper_fields({
                    k: v for k, v in p.to_dict().items()
                    if k not in ("lit_id", "created_at", "selected")
                })
                validate_paper_identity({"lit_id": p.lit_id, **meta})
                meta["identity_key"] = build_identity_key(
                    source=src_value, title=str(meta.get("title") or ""),
                    authors=list(meta.get("authors") or []),
                    year=int(meta.get("year") or 0), doi=str(meta.get("doi") or ""),
                )
                # v8.1:查重限定在「当前任务」内 —— 同一文献可在不同任务各存一行,
                # 不再把其他任务的行改挂到本任务(那是导致跨任务搬家的根因)。
                # v9.6:先查本批内存索引,miss 再查库(避免 autoflush 盲区)
                existing = batch_identity.get(meta["identity_key"])
                if existing is None:
                    dedup_q = db.query(PaperModel).filter(
                        PaperModel.identity_key == meta["identity_key"]
                    )
                    if pool_task_id:
                        dedup_q = dedup_q.filter(PaperModel.task_id == pool_task_id)
                    else:
                        dedup_q = dedup_q.filter(PaperModel.task_id.is_(None))
                    existing = dedup_q.first()
                if existing is not None:
                    for k, v in meta.items():
                        setattr(existing, k, v)
                    batch_identity[meta["identity_key"]] = existing
                    updated += 1
                else:
                    new_row = PaperModel(
                        lit_id=p.lit_id, selected=True, task_id=pool_task_id, **meta,
                    )
                    db.add(new_row)
                    batch_identity[meta["identity_key"]] = new_row
                    inserted += 1
            except Exception as exc:
                log.warning("写入文献失败 lit_id=%s: %s", p.lit_id, exc)
                failed += 1
                failed_by_source[src_value] += 1
                reason = str(exc).split("，")[0].split(",")[0][:40]
                failed_reasons[reason] += 1
        db.commit()

    log.info(
        "upsert_with_overwrite sources=%s cleared=%d inserted=%d updated=%d failed=%d reasons=%s",
        targets, cleared, inserted, updated, failed, dict(failed_reasons),
    )
    return {"cleared": cleared, "inserted": inserted, "updated": updated,
            "failed": failed, "failed_by_source": dict(failed_by_source),
            "failed_reasons": dict(failed_reasons)}


def split_by_source(papers: list[Paper]) -> dict[str, list[Paper]]:
    """按 source 分组(供前端展示各源贡献)。"""
    out: dict[str, list[Paper]] = defaultdict(list)
    for p in papers:
        src = str(p.source.value if hasattr(p.source, "value") else p.source)
        out[src].append(p)
    return dict(out)


__all__ = ["upsert_with_overwrite", "split_by_source"]
