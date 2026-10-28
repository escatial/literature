"""统一检索历史 API(需求4 + B6 数据归属)。"""
from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Request

from db.schemas import RetrievalHistoryOut
from retrieval.history_service import (
    _HISTORY_KEEP,
    delete_history_record,
    get_history,
    list_recent,
    restore_to_pool,
)
from retrieval.task_service import create_task_v2

router = APIRouter(prefix="/retrieval/history", tags=["retrieval-history"])


def _to_out(row) -> RetrievalHistoryOut:
    """list_recent/record_history/get_history 均返回 dict。

    dict 中的 owner_user_id(B6)是服务端授权字段,RetrievalHistoryOut
    为显式字段白名单,不会序列化外泄。
    """
    return RetrievalHistoryOut(**row)


def _request_user(request: Request | None):
    return getattr(request.state, "user", None) if request is not None else None


def _is_admin(user) -> bool:
    return getattr(user, "role", "") == "admin"


def _assert_owner(row: dict | None, user) -> dict:
    """B6:非 admin 只能触达自己名下的历史;不可触达一律 404(防存在性泄漏)。"""
    if row is None:
        raise HTTPException(404, "history not found")
    if user is None or _is_admin(user):
        return row
    if row.get("owner_user_id") != getattr(user, "user_id", None):
        raise HTTPException(404, "history not found")
    return row


@router.get("", response_model=list[RetrievalHistoryOut])
def list_history(limit: int = _HISTORY_KEEP, request: Request = None):
    """获取最近 N 条检索历史(默认 5 条,按时间倒序)。B6:仅本人名下,admin 全量。"""
    user = _request_user(request)
    rows = list_recent(
        limit=limit,
        owner_user_id=getattr(user, "user_id", None),
        is_admin=_is_admin(user),
    )
    return [_to_out(r) for r in rows]


@router.post("/{history_id}/replay", response_model=dict)
def replay_history(history_id: int, request: Request = None):
    """点击历史记录快速重新发起同款检索。

    返回新任务的 task_id 与初始 status。
    """
    user = _request_user(request)
    row = _assert_owner(get_history(history_id), user)
    # history_service.get_history() 返回的是 dict(避免 ORM 实例 detached),
    # 所以走 dict 访问,不要用 row.topic。
    # v9.6:聚合历史 sources 含 cnki,直接透传会被 create_task_v2 的
    # 「必须且仅含 openalex+pubmed」校验拒掉变 500;按英文双库过滤,
    # 全不在白名单/为空时传 None 走默认双库。
    raw_sources = [str(s) for s in (row.get("sources") or [])]
    sources = [s for s in raw_sources if s in ("openalex", "pubmed")] or None
    task = create_task_v2(
        topic=row["topic"],
        sources=sources,
        use_snowball=False,
        # B6:回放产生的新任务归属当前操作者(而非原历史 owner)
        owner_user_id=getattr(user, "user_id", None),
    )
    return {"task_id": task.task_id, "status": task.status}


@router.post("/{history_id}/restore", response_model=dict)
def restore_history(
    history_id: int,
    # v7.1:按前端隔离 ID 恢复,只清/只写当前任务的文献池
    x_task_id: str | None = Header(None, alias="X-Task-Id"),
    request: Request = None,
):
    """把该条历史的文献快照恢复到文献池(先清空池再写入),供「查看」跳转文献池。"""
    _assert_owner(get_history(history_id), _request_user(request))
    try:
        n = restore_to_pool(history_id, pool_task_id=(x_task_id or "").strip() or None)
    except ValueError as e:
        # v9.6:区分「历史不存在」(404) 与「缺任务上下文拒绝执行」(400)
        if "not found" in str(e):
            raise HTTPException(404, str(e))
        raise HTTPException(400, str(e))
    return {"total": n}


@router.delete("/{history_id}", status_code=204)
def delete_history(
    history_id: int,
    # v7.2:透传当前任务 ID,删除历史时把该次检索导入文献池的文献一并删干净
    x_task_id: str | None = Header(None, alias="X-Task-Id"),
    request: Request = None,
):
    """删除一条检索历史(连同其数据库中的快照数据与文献池中的对应文献)。"""
    _assert_owner(get_history(history_id), _request_user(request))
    if not delete_history_record(
        history_id, pool_task_id=(x_task_id or "").strip() or None
    ):
        raise HTTPException(404, f"history {history_id} not found")
