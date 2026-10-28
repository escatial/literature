"""统一停止接口:一次调用同时停止知网爬虫任务与英文检索任务。

实现:后台任务都跑在线程里,线程不可强杀,只能置位取消标志,
各循环入口检查到标志后尽快退出并落库为失败("用户已手动停止")。
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel

from api.cnki import stop_cnki_tasks
from retrieval.task_service import stop_tasks

router = APIRouter(prefix="/retrieval", tags=["retrieval-stop"])


class StopRequest(BaseModel):
    task_ids: list[str] | None = None


class StopResponse(BaseModel):
    stopped: list[str]


@router.post("/stop", response_model=StopResponse)
def stop_retrieval(req: StopRequest | None = None, request: Request = None):
    """停止任务。task_ids 传空(或不传)则停止当前用户名下所有运行中任务。

    B6 用户隔离:非 admin 只能停止自己 owner 的任务;admin 全量。
    知网爬虫任务属管理面(单跑语义),停止仍为全站动作。
    """
    user = getattr(request.state, "user", None) if request is not None else None
    ids = req.task_ids if req else None
    stopped_en = stop_tasks(
        ids,
        user_id=getattr(user, "user_id", None),
        is_admin=getattr(user, "role", "") == "admin",
    )
    stopped_cnki = stop_cnki_tasks(ids)
    return StopResponse(stopped=stopped_en + stopped_cnki)
