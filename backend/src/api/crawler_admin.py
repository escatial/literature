"""爬虫运维管理 API（需求5：可视化任务监控面板的后端数据源）。

只读为主 + 少量控制动作，全部数据来自 automation/cnki 各子系统的单例：
- 任务：monitor.TaskRegistry（状态/进度/环形日志/质量评分/全链路计数）
- 控制面：任务启停（与业务侧 stop_event 桥接）、运行中参数热调整
- 资源面：动态并发池(scheduler) / 断路器(resilience) / 代理池(proxy_pool) / 告警中心

导入路径说明：统一以「automation.* 优先，src.* 兜底」，与业务链路
（api/cnki.py、retrieval/sources/cnki.py、writing/orchestrator.py、
e2e_run.py）同树。历史教训（2026-09 生产事故）：曾按「生产 src.* 优先、
测试 automation.* 兜底」实现，但 main.py 同时把 backend 根与 src 注入
sys.path——生产里 `import automation` 同样成功，结果面板挂 automation
树、爬虫业务挂 src 树，monitor registry 单例分裂：任务在跑而面板全 0。
双单例的根源是 adapter 的相对导入（.cnki.monitor）跟随导入它的包名解析，
因此所有链路必须收敛到同一个顶层包名；backend/src 不在 sys.path 的
独立脚本才回落 src.*。测试 test_dual_singleton_guard /
test_business_chain_same_tree_as_admin 守护此约束。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

try:  # 测试/独立脚本（backend/src 在 sys.path，顶层 automation 包可导入）
    from automation.cnki import crawler as _crawler
    from automation.cnki import monitor as _monitor
    from automation.cnki import proxy_pool as _proxy_pool
    from automation.cnki import resilience as _resilience
    from automation.cnki import scheduler as _scheduler
except ImportError:  # 生产路径（main.py 注入 backend 根，与 api/cnki.py 同源）
    from src.automation.cnki import crawler as _crawler
    from src.automation.cnki import monitor as _monitor
    from src.automation.cnki import proxy_pool as _proxy_pool
    from src.automation.cnki import resilience as _resilience
    from src.automation.cnki import scheduler as _scheduler

log = logging.getLogger(__name__)

router = APIRouter(prefix="/crawler", tags=["crawler-admin"])


class ParamsUpdateRequest(BaseModel):
    """运行中参数热调整请求体（白名单校验在 registry.update_params 内）。"""

    delay_seconds: float | None = Field(None, gt=0, description="请求基础间隔(秒)")
    max_workers: int | None = Field(None, ge=1, le=16, description="动态池并发上限")
    page_size: int | None = Field(None, ge=10, le=50, description="检索页大小")
    max_per_keyword: int | None = Field(None, ge=1, description="单检索式抓取上限")


def _pool_snapshot() -> dict:
    """动态并发池快照（未初始化时给默认壳，前端渲染不判空）。"""
    pool = _scheduler.get_pool()
    if pool is None:
        return {"initialized": False}
    snap = pool.snapshot()
    snap["initialized"] = True
    return snap


def _proxy_snapshot() -> dict:
    """代理池快照（off 模式无池实例，返回模式即可）。"""
    try:
        # v9.6:读爬虫实际生效的 proxy 配置段——此前恒传空 dict,面板的 mode
        # 永远显示 off(除非恰好设了环境变量)
        mode = _proxy_pool.proxy_mode(_crawler.CONFIG.get("proxy") or {})
    except Exception:
        mode = "off"
    pool = _proxy_pool.get_proxy_pool()
    return {
        "mode": mode,
        "proxies": pool.snapshot() if pool is not None else [],
    }


@router.get("/dashboard")
def dashboard():
    """总览聚合：任务概览 + 动态池 + 断路器 + 代理池 + 最近告警，面板首屏一次拉全。"""
    tasks = _monitor.get_registry().list_tasks()
    running = sum(1 for t in tasks if t["status"] == "running")
    done = sum(1 for t in tasks if t["status"] == "done")
    failed = sum(1 for t in tasks if t["status"] == "failed")
    stopped = sum(1 for t in tasks if t["status"] == "stopped")
    saved_total = sum(t["saved"] for t in tasks)
    return {
        "tasks": {
            "total": len(tasks),
            "running": running,
            "done": done,
            "failed": failed,
            "stopped": stopped,
            "saved_total": saved_total,
            "recent": tasks[:10],
        },
        "pool": _pool_snapshot(),
        "breakers": _resilience.breakers_snapshot(),
        "proxy": _proxy_snapshot(),
        "alerts": _resilience.get_alert_manager().snapshot(),
    }


@router.get("/tasks")
def list_tasks(status: str | None = None):
    """任务列表（新→旧，可按状态过滤）。"""
    return _monitor.get_registry().list_tasks(status=status)


@router.get("/tasks/{task_id}")
def task_detail(task_id: str, logs: bool = True):
    """单任务全量快照（含环形日志/计数器/质量评分）。"""
    snap = _monitor.get_registry().snapshot(task_id, include_logs=logs)
    if snap is None:
        raise HTTPException(404, f"任务不存在: {task_id}")
    return snap


@router.post("/tasks/{task_id}/stop")
def stop_task(task_id: str):
    """停止单个任务（置取消事件；与业务侧 stop_event 桥接，幂等）。"""
    ok = _monitor.get_registry().stop_task(task_id)
    if not ok:
        raise HTTPException(404, f"任务不存在: {task_id}")
    return {"task_id": task_id, "stopped": True}


@router.post("/tasks/stop-all")
def stop_all_tasks():
    """停止全部运行中任务（运维兜底），返回停止数量。"""
    return {"stopped": _monitor.get_registry().stop_all()}


@router.put("/tasks/{task_id}/params")
def update_params(task_id: str, req: ParamsUpdateRequest):
    """运行中参数热调整（白名单外直接 422）。

    生效范围(v9.6):delay_seconds/max_workers/page_size 经钩子即时生效;
    max_per_keyword 是任务启动参数(运行中不可达),仅落账——响应中以
    deferred 如实标注,不再假称全部生效。
    """
    patch = {k: v for k, v in req.model_dump().items() if v is not None}
    if not patch:
        raise HTTPException(422, "空补丁:至少提供一个待调整参数")
    try:
        effective = _monitor.get_registry().update_params(task_id, patch)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    applied = [k for k in patch if k != "max_per_keyword"]
    deferred = [k for k in patch if k == "max_per_keyword"]
    return {"task_id": task_id, "params": effective, "applied": applied, "deferred": deferred}


@router.get("/alerts")
def alerts():
    """告警中心环形缓冲（含节流合并后的推送计数）。"""
    return {"alerts": _resilience.get_alert_manager().snapshot()}


@router.get("/proxies")
def proxies():
    """代理池快照（URL 已打码，健康分/冷却状态可见）。"""
    return _proxy_snapshot()


@router.get("/breakers")
def breakers():
    """断路器状态表（closed/open/half_open + 连败计数）。"""
    return {"breakers": _resilience.breakers_snapshot()}


@router.post("/breakers/{name}/reset")
def reset_breaker(name: str):
    """手动复位断路器（open 卡死时的运维动作）。"""
    breaker = _resilience.get_breaker(name)
    breaker.reset()
    return {"name": name, "reset": True}


@router.get("/pool")
def pool_status():
    """动态并发池状态（当前并发/期望并发/延迟与失败率窗口）。"""
    return _pool_snapshot()
