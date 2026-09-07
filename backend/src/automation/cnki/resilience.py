#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""稳定性基石：分层异常重试 + 断路器 + 异常告警推送。

设计约束：
- 本模块**不 import crawler**（crawler 反向依赖本模块），避免循环导入；
  知网业务异常的分类通过 :func:`register_exception_classifier` 注册钩子接入。
- 所有休眠经由可注入的 ``sleep_fn``（默认 ``time.sleep``），
  crawler 注入 ``sleep_jitter``（带抖动），单测注入记录器以实现零等待。
- 时间源 ``clock`` 可注入（默认 ``time.monotonic``），单测可加速断路器冷却。

四个错误层级：
- TRANSIENT    网络波动/超时/5xx     —— 短退避快速重试（1/2/4s + 抖动）
- RATE_LIMITED 限流/服务繁忙(429/503) —— 长退避跨限流窗口（15/45s）
- BLOCKED      风控拦截/cookie 失效   —— 重试无意义，交上层处置（换代理/换 cookie）
- FATAL        业务致命/参数校验失败  —— 立即上抛，禁止重试

断路器三态：
- closed   正常放行；连续失败达阈值 → open
- open     直接拒绝（抛 CircuitOpenError），冷却 recovery_timeout 后 → half-open
- half-open 放行单个探测请求；成功 → closed，失败 → 重新 open
"""

from __future__ import annotations

import enum
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

import requests

# ========================== 错误层级 ==========================
class ErrorClass(enum.Enum):
    """异常分层：决定重试策略（次数/退避曲线）。"""

    TRANSIENT = "transient"          # 网络波动：短退避快速重试
    RATE_LIMITED = "rate_limited"    # 临时限流：长退避跨窗口
    BLOCKED = "blocked"              # 风控/封禁：重试无意义，交上层
    FATAL = "fatal"                  # 致命错误：立即上抛


class CircuitOpenError(RuntimeError):
    """断路器处于 open 态时抛出：请求尚未发出即被拒绝，防止无效堆积。"""

    def __init__(self, name: str, cooldown_left: float):
        self.name = name
        self.cooldown_left = cooldown_left
        super().__init__(
            f"断路器[{name}]已熔断，冷却剩余 {cooldown_left:.0f}s，拒绝本次请求"
        )


# ========================== 异常分类器 ==========================
# 业务侧注册的额外分类钩子（crawler 集成时注册知网异常映射）
_EXTRA_CLASSIFIERS: list[Callable[[BaseException], "ErrorClass | None"]] = []
_CLASSIFIER_LOCK = threading.Lock()


def register_exception_classifier(fn: Callable[[BaseException], "ErrorClass | None"]) -> None:
    """注册业务异常分类钩子；fn 返回 ErrorClass 或 None（表示不认识，交给内置分类）。

    线程安全；重复注册同一函数会被去重。
    """
    with _CLASSIFIER_LOCK:
        if fn not in _EXTRA_CLASSIFIERS:
            _EXTRA_CLASSIFIERS.append(fn)


def classify_exception(exc: BaseException) -> ErrorClass:
    """把任意异常归入四个错误层级（先业务钩子，后内置规则）。

    内置规则：
    - CircuitOpenError                 → FATAL（断路器拒绝，禁止再重试）
    - requests Timeout/ConnectionError → TRANSIENT
    - HTTPError 429/503                → RATE_LIMITED
    - HTTPError 其他 5xx               → TRANSIENT
    - HTTPError 4xx（除 429）          → FATAL（参数/权限问题，重试无意义）
    - 其余未知异常                     → FATAL（保守策略：逻辑错误不盲目重试）
    """
    # 业务钩子优先（知网异常的语义只有 crawler 层知道）
    with _CLASSIFIER_LOCK:
        hooks = list(_EXTRA_CLASSIFIERS)
    for fn in hooks:
        try:
            result = fn(exc)
        except Exception:
            result = None
        if result is not None:
            return result

    if isinstance(exc, CircuitOpenError):
        return ErrorClass.FATAL
    if isinstance(exc, requests.exceptions.HTTPError):
        status = exc.response.status_code if exc.response is not None else 0
        if status in (429, 503):
            return ErrorClass.RATE_LIMITED
        if 500 <= status < 600:
            return ErrorClass.TRANSIENT
        return ErrorClass.FATAL
    if isinstance(exc, (
        requests.exceptions.Timeout,
        requests.exceptions.ConnectionError,
        requests.exceptions.ChunkedEncodingError,
    )):
        return ErrorClass.TRANSIENT
    return ErrorClass.FATAL


# ========================== 分层重试策略 ==========================
@dataclass
class RetryPolicy:
    """各错误层级的阶梯重试参数（次数 + 退避秒数表 + 抖动幅度）。

    默认值按知网风控特性校准：
    - TRANSIENT  3 次，1/2/4s —— 网络抖动秒级自愈，重了反而添堵
    - RATE_LIMITED 2 次，15/45s —— 知网限流窗口为分钟级，短退避无效
    - BLOCKED/FATAL 0 次 —— 语义上重试不可能变好
    """

    max_retries: dict = field(default_factory=lambda: {
        ErrorClass.TRANSIENT: 3,
        ErrorClass.RATE_LIMITED: 2,
        ErrorClass.BLOCKED: 0,
        ErrorClass.FATAL: 0,
    })
    backoff_table: dict = field(default_factory=lambda: {
        ErrorClass.TRANSIENT: [1.0, 2.0, 4.0],
        ErrorClass.RATE_LIMITED: [15.0, 45.0],
    })
    jitter: float = 0.2  # 退避 ±20% 抖动，打散重试同步性

    def retries_for(self, error_class: ErrorClass) -> int:
        """该层级允许的最大重试次数（不含首次请求）。"""
        return int(self.max_retries.get(error_class, 0))

    def delay_for(self, error_class: ErrorClass, attempt: int, rng=None) -> float:
        """第 attempt 次重试前的等待秒数（指数表 + 抖动；缺表时按指数 2^n 兜底）。

        :param rng: 可注入随机源（单测确定性）；默认 random.uniform
        """
        import random as _random
        rng = rng or _random.uniform
        table = self.backoff_table.get(error_class) or [
            float(2 ** i) for i in range(int(self.retries_for(error_class)))
        ]
        idx = min(attempt, len(table) - 1)
        base = float(table[idx])
        if self.jitter > 0:
            base *= 1.0 + rng(-self.jitter, self.jitter)
        return max(base, 0.0)


# ========================== 断路器 ==========================
class CircuitBreaker:
    """三态断路器：防止对持续故障的目标站点堆积无效请求。

    典型集成：crawler 的统一请求入口在发请求前调 :meth:`allow`，
    成功/失败后调 :meth:`record_success` / :meth:`record_failure`。
    线程安全（adapter 多线程并发抓详情页共享同一实例）。
    """

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

    def __init__(
        self,
        name: str = "default",
        failure_threshold: int = 5,
        recovery_timeout: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.name = name
        self.failure_threshold = max(int(failure_threshold), 1)
        self.recovery_timeout = max(float(recovery_timeout), 1.0)
        self._clock = clock
        self._lock = threading.Lock()
        self._state = self.CLOSED
        self._consecutive_failures = 0
        self._opened_at = 0.0

    # ---- 状态查询 ----
    @property
    def state(self) -> str:
        """当前状态（open 冷却期满时惰性迁移到 half_open）。"""
        with self._lock:
            self._lazy_half_open()
            return self._state

    def cooldown_left(self) -> float:
        """open 态剩余冷却秒数（供日志/面板展示；非 open 态返回 0）。"""
        with self._lock:
            if self._state != self.OPEN:
                return 0.0
            return max(0.0, self.recovery_timeout - (self._clock() - self._opened_at))

    def _lazy_half_open(self) -> None:
        """内部方法（须持锁调用）：open 冷却期满 → half_open，放行探测。"""
        if self._state == self.OPEN and (
            self._clock() - self._opened_at >= self.recovery_timeout
        ):
            self._state = self.HALF_OPEN

    # ---- 状态迁移 ----
    def allow(self) -> bool:
        """是否放行本次请求。open 且未冷却完 → 抛 CircuitOpenError。"""
        with self._lock:
            self._lazy_half_open()
            if self._state == self.OPEN:
                # 注意：已持锁，不能调 cooldown_left()（非重入锁会死锁），就地计算
                left = max(0.0, self.recovery_timeout - (self._clock() - self._opened_at))
                raise CircuitOpenError(self.name, left)
            return True

    def record_success(self) -> None:
        """请求成功：half_open 探测成功 → closed；closed 态清零失败计数。"""
        with self._lock:
            self._consecutive_failures = 0
            self._state = self.CLOSED

    def record_failure(self) -> None:
        """请求失败：closed 连续失败达阈值 → open；half_open 探测失败 → 重新 open。"""
        with self._lock:
            self._consecutive_failures += 1
            if self._state == self.HALF_OPEN or (
                self._consecutive_failures >= self.failure_threshold
            ):
                self._state = self.OPEN
                self._opened_at = self._clock()

    def reset(self) -> None:
        """手动复位到 closed（如更换代理池/cookie 后调用）。"""
        with self._lock:
            self._state = self.CLOSED
            self._consecutive_failures = 0
            self._opened_at = 0.0


# ========================== 告警推送 ==========================
@dataclass
class Alert:
    """单条告警（环形缓冲项，前端监控面板直接消费）。"""

    title: str
    level: str          # info / warning / critical
    detail: str
    ts: float           # unix 时间戳
    count: int = 1      # 节流窗口内同类告警的累计次数
    key: str = ""       # 节流键（默认 = title）


class AlertManager:
    """异常告警中心：环形缓冲（前端拉取）+ webhook 推送 + 同类节流去重。

    节流语义：同类告警（key 相同）在 cooldown 秒内只推送一次 webhook，
    但缓冲中的 count 持续累加——运维既能看到"发生了"，也能看到"发生了多少次"。
    """

    def __init__(
        self,
        cooldown: float = 300.0,
        buffer_size: int = 200,
        clock: Callable[[], float] = time.time,
    ):
        self.cooldown = float(cooldown)
        self._clock = clock
        self._buffer: deque[Alert] = deque(maxlen=int(buffer_size))
        self._last_sent: dict[str, float] = {}
        self._lock = threading.Lock()

    def alert(
        self,
        title: str,
        level: str = "warning",
        detail: str = "",
        push: bool = True,
        sender: "Callable[[Alert], None] | None" = None,
    ) -> Alert:
        """记录并（可选）推送一条告警。

        :param sender: webhook 发送函数（注入便于单测）；默认 POST 到
            ``alert_webhook_url`` 环境变量指向的地址，未配置则跳过推送。
        :param push: False 时仅落缓冲（如高频 INFO 级事件）。
        """
        now = self._clock()
        key = title
        with self._lock:
            # 同类告警在缓冲中就地累加（保留首次时间戳，count 反映真实频次）
            for existing in reversed(self._buffer):
                if existing.key == key and now - existing.ts < self.cooldown:
                    existing.count += 1
                    existing.detail = detail or existing.detail
                    throttled = True
                    break
            else:
                self._buffer.append(Alert(
                    title=title, level=level, detail=detail, ts=now, key=key,
                ))
                throttled = False
            should_push = push and (not throttled)
            if not throttled:
                self._last_sent[key] = now

        if should_push:
            alert_obj = self._find(key)
            self._do_push(alert_obj, sender)
        return self._find(key)

    def _find(self, key: str) -> Alert:
        """按节流键查找最近的缓冲项（找不到返回占位，理论上不会发生）。"""
        with self._lock:
            for existing in reversed(self._buffer):
                if existing.key == key:
                    return existing
        return Alert(title=key, level="info", detail="", ts=self._clock())

    @staticmethod
    def _default_sender(alert: Alert) -> None:
        """默认 webhook 发送：POST JSON 到 CNKI_ALERT_WEBHOOK_URL。"""
        import json as _json
        import os as _os
        import urllib.request as _request

        url = _os.environ.get("CNKI_ALERT_WEBHOOK_URL", "").strip()
        if not url:
            return
        payload = _json.dumps({
            "title": alert.title,
            "level": alert.level,
            "detail": alert.detail,
            "count": alert.count,
            "ts": alert.ts,
        }).encode("utf-8")
        req = _request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        # webhook 失败不影响主流程：告警是旁路，不能反噬爬取
        try:
            _request.urlopen(req, timeout=5)
        except Exception:
            pass

    def _do_push(self, alert: Alert, sender) -> None:
        sender = sender or self._default_sender
        try:
            sender(alert)
        except Exception:
            pass

    def snapshot(self) -> list[dict]:
        """导出告警缓冲（新→旧），供监控 API 直接序列化。"""
        with self._lock:
            items = list(self._buffer)
        items.reverse()
        return [
            {
                "title": a.title, "level": a.level, "detail": a.detail,
                "ts": a.ts, "count": a.count,
            }
            for a in items
        ]


# 模块级默认告警中心（monitor 与 crawler 共享同一实例，面板才能看到全量告警）
_default_alert_manager: AlertManager | None = None
_ALERT_SINGLETON_LOCK = threading.Lock()


def get_alert_manager() -> AlertManager:
    """获取全局告警中心（懒初始化，进程内单例）。"""
    global _default_alert_manager
    with _ALERT_SINGLETON_LOCK:
        if _default_alert_manager is None:
            _default_alert_manager = AlertManager()
        return _default_alert_manager


def reset_alert_manager() -> None:
    """重置全局告警中心（仅测试用）。"""
    global _default_alert_manager
    with _ALERT_SINGLETON_LOCK:
        _default_alert_manager = None


# ========================== 统一分层重试入口 ==========================
def resilient_call(
    fn: Callable,
    *,
    args: tuple = (),
    kwargs: dict | None = None,
    policy: RetryPolicy | None = None,
    breaker: CircuitBreaker | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    on_retry: "Callable[[ErrorClass, int, BaseException, float], None] | None" = None,
    on_give_up: "Callable[[BaseException, ErrorClass], None] | None" = None,
    op_name: str = "操作",
):
    """带断路器与分层重试的统一调用入口。

    执行序：断路器 allow → 执行 fn → 成功 record_success 返回；
    失败 → 分类 → 按层级的退避表重试；耗尽 → on_give_up 后上抛原始异常。

    :param on_retry: 重试回调 (error_class, attempt, exc, wait_seconds)，crawler
        用它接 emit_log / throttle_hit；不传则静默重试
    :param on_give_up: 重试耗尽回调 (exc, error_class)，crawler 用它发告警
    :raises CircuitOpenError: 断路器 open 且未冷却完
    :raises 原始异常: 重试耗尽或 FATAL/BLOCKED（原样上抛，保留语义）
    """
    kwargs = kwargs or {}
    policy = policy or RetryPolicy()
    attempt = 0
    while True:
        if breaker is not None:
            breaker.allow()  # open 态直接抛 CircuitOpenError，请求不发出
        try:
            result = fn(*args, **kwargs)
        except Exception as exc:
            error_class = classify_exception(exc)
            max_retries = policy.retries_for(error_class)
            # 断路器计数（open/half_open 迁移在这里发生）
            if breaker is not None:
                breaker.record_failure()
            if attempt >= max_retries:
                if on_give_up is not None:
                    on_give_up(exc, error_class)
                raise
            wait = policy.delay_for(error_class, attempt)
            if on_retry is not None:
                on_retry(error_class, attempt + 1, exc, wait)
            if wait > 0:
                sleep_fn(wait)
            attempt += 1
        else:
            if breaker is not None:
                breaker.record_success()
            return result


# ========================== 断路器注册表 ==========================
# 按名字共享断路器（如 "search" / "detail" / "gbt"），面板可按域查看熔断状态
_breakers: dict[str, CircuitBreaker] = {}
_BREAKER_LOCK = threading.Lock()


def get_breaker(
    name: str,
    failure_threshold: int = 5,
    recovery_timeout: float = 60.0,
) -> CircuitBreaker:
    """获取（或创建）具名断路器——同名字共享实例，保证状态全局一致。"""
    with _BREAKER_LOCK:
        if name not in _breakers:
            _breakers[name] = CircuitBreaker(
                name=name,
                failure_threshold=failure_threshold,
                recovery_timeout=recovery_timeout,
            )
        return _breakers[name]


def breakers_snapshot() -> list[dict]:
    """导出所有断路器状态（监控面板用）。"""
    with _BREAKER_LOCK:
        items = list(_breakers.items())
    return [
        {
            "name": b.name,
            "state": b.state,
            "consecutive_failures": b._consecutive_failures,
            "cooldown_left": round(b.cooldown_left(), 1),
        }
        for _, b in items
    ]
