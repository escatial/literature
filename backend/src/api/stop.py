"""统一停止接口:一次调用同时停止知网爬虫任务与英文检索任务。

实现:后台任务都跑在线程里,线程不可强杀,只能置位取消标志,
各循环入口检查到标志后尽快退出并落库为失败("用户已手动停止")。
"""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from api.cnki import stop_cnki_tasks
from retrieval.task_service import stop_tasks

router = APIRouter(prefix="/retrieval", tags=["retrieval-stop"])


class StopRequest(BaseModel):
    task_ids: list[str] | None = None


class StopResponse(BaseModel):
    stopped: list[str]


@router.post("/stop", response_model=StopResponse)
def stop_retrieval(req: StopRequest | None = None):
    """停止任务。task_ids 传空(或不传)则停止所有运行中的知网/英文检索任务。"""
    ids = req.task_ids if req else None
    stopped_en = stop_tasks(ids)
    stopped_cnki = stop_cnki_tasks(ids)
    return StopResponse(stopped=stopped_en + stopped_cnki)
