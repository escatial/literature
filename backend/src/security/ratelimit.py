"""登录失败限流(进程内,2026-10-27 Batch B2)。

策略:同一 (ip, username) 在 10 分钟窗口内失败 ≥5 次 → 锁定 15 分钟;
成功登录立即清零该键的失败计数。

边界说明:
- 本实现为**单进程内存态**(模块级 dict + threading.Lock)。本后端的
  部署形态是单进程 uvicorn(C1 起写作流已按 ContextVar 在 worker 线程
  绑定/复位 LLM 配置,同用户可并行多条写作流,但服务进程仍是单个 ——
  内存态与部署模型匹配);若未来多进程/多实例部署,
  必须外置到共享存储(如 Redis),否则各进程各数各的,限流形同虚设。
- env AUTH_RATELIMIT_DISABLED=1 整体关闭(本机调试/测试用;
  tests/conftest.py 的 autouse fixture 默认关闭,限流专项测试自行开启)。
"""
from __future__ import annotations

import os
import threading
import time

# 失败阈值 / 计数窗口 / 锁定时长(秒)
FAILURE_THRESHOLD = 5
WINDOW_SECONDS = 600.0        # 10 分钟滑动窗口
LOCK_SECONDS = 900.0          # 锁定 15 分钟

# (ip, username) -> {"fails": int, "window_start": float, "lock_until": float}
_records: dict[tuple[str, str], dict] = {}
_lock = threading.Lock()


def _disabled() -> bool:
    return os.environ.get("AUTH_RATELIMIT_DISABLED", "").strip() == "1"


def _entry(now: float) -> dict:
    return {"fails": 0, "window_start": now, "lock_until": 0.0}


def is_locked(ip: str, username: str) -> tuple[bool, int]:
    """查询是否处于锁定态;返回 (是否锁定, 剩余秒数)。窗口过期自动解锁。"""
    if _disabled():
        return False, 0
    key = (ip, (username or "").strip())
    now = time.monotonic()
    with _lock:
        rec = _records.get(key)
        if rec is None:
            return False, 0
        if rec["lock_until"] > now:
            return True, int(rec["lock_until"] - now) + 1
        # 锁定已过期:连同失败计数一并复位(进入新窗口)
        if rec["lock_until"] and rec["lock_until"] <= now:
            _records.pop(key, None)
        return False, 0


def register_failure(ip: str, username: str) -> None:
    """记录一次登录失败;达到阈值即进入锁定。"""
    if _disabled():
        return
    key = (ip, (username or "").strip())
    now = time.monotonic()
    with _lock:
        rec = _records.get(key)
        if rec is None or now - rec["window_start"] > WINDOW_SECONDS:
            # 无记录或窗口滑出:开新窗口(保留可能仍生效的锁定语义由
            # lock_until 独立承载,这里重开计数不影响未到期的锁)
            old_lock = rec["lock_until"] if rec else 0.0
            rec = _entry(now)
            rec["lock_until"] = old_lock
            _records[key] = rec
        rec["fails"] += 1
        if rec["fails"] >= FAILURE_THRESHOLD:
            rec["lock_until"] = now + LOCK_SECONDS


def register_success(ip: str, username: str) -> None:
    """登录成功:清零该键(保留未到期的锁?不清 —— 锁定期间正确密码也 429,
    成功清零只可能发生在未锁定时,故直接整键删除)。"""
    if _disabled():
        return
    key = (ip, (username or "").strip())
    with _lock:
        _records.pop(key, None)


def reset() -> None:
    """测试/运维清理:清空全部计数。"""
    with _lock:
        _records.clear()
