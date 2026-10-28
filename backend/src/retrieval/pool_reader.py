"""文献池读取器(C6 从 api/papers.py 机械移动 + 新增按 lit_ids 引用取数)。

papers.py 保留同名 re-export 以兼容既有调用点;本模块是阶段2断点恢复
取数的唯一入口 —— plan 快照只存 lit_ids 引用,papers 表是唯一真源。
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import PaperModel


def apply_owner_scope(q, user):
    """B6 数据归属过滤:admin 直通,普通用户仅见自己的行。

    与 X-Task-Id 双层正交 —— task_id 是业务域隔离,owner_user_id 是安全域。
    兼容 sqlalchemy Query(.filter)与 Select(.where)两种形态;
    user 为 None(测试/内部直调上下文)时直通,鉴权由中间件保证。
    """
    if user is None or getattr(user, "role", "") == "admin":
        return q
    cond = PaperModel.owner_user_id == getattr(user, "user_id", None)
    if hasattr(q, "where"):
        return q.where(cond)
    return q.filter(cond)


def _row_to_paper(row: PaperModel):
    """ORM 行 → 写作侧 Paper(与 api/papers 历史实现逐字段一致)。"""
    from retrieval.types import Paper as RetrievalPaper
    from retrieval.types import normalize_source

    return RetrievalPaper(
        lit_id=row.lit_id,
        source=normalize_source(row.source or "user_imported"),
        title=row.title or "",
        authors=list(row.authors or []),
        journal=row.journal or "",
        year=row.year or 0,
        volume=row.volume,
        issue=row.issue,
        pages=row.pages,
        abstract=row.abstract or "",
        doi=row.doi,
        source_url=row.source_url or "",
        raw_citation=row.raw_citation,
    )


def _task_papers_as_retrieval(db: Session, task_id: str, user=None) -> list:
    """读取任务文献池(selected)并转换为写作侧 Paper 对象。B6:按 owner 过滤。"""
    rows = (
        db.execute(
            apply_owner_scope(
                select(PaperModel)
                .where(PaperModel.task_id == task_id, PaperModel.selected == 1)
                .order_by(PaperModel.id),
                user,
            )
        )
        .scalars()
        .all()
    )
    return [_row_to_paper(r) for r in rows]


def read_pool_papers_by_ids(
    db: Session,
    task_id: str,
    lit_ids: list[str],
    user=None,
) -> list:
    """按 lit_ids 引用回表取全字段(C6 阶段2取数),保持 screened_lit_ids 顺序。

    - 限定当前 task + owner 归属;池中不存在的引用静默跳过(由调用方判空);
    - 不要求 selected=True:快照恢复的池子是规划时确认过的全集,勾选状态
      是后续用户操作,不应影响断点恢复的取数完整性。
    """
    if not lit_ids:
        return []
    unique_ids = list(dict.fromkeys(str(i) for i in lit_ids if i))
    rows = (
        db.execute(
            apply_owner_scope(
                select(PaperModel).where(
                    PaperModel.task_id == task_id,
                    PaperModel.lit_id.in_(unique_ids),
                ),
                user,
            )
        )
        .scalars()
        .all()
    )
    by_id = {r.lit_id: r for r in rows}
    return [_row_to_paper(by_id[i]) for i in unique_ids if i in by_id]


__all__ = [
    "apply_owner_scope",
    "_task_papers_as_retrieval",
    "read_pool_papers_by_ids",
]
