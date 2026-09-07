"""统一重试退避工具(方案 §6 "429/超时循环": 指数退避 + 备用源 + 任务续跑)。

设计:
- 单一 retry_with_backoff 函数,适用于所有外部 HTTP/LLM 调用;
- 默认 3 次重试,base=0.5s,exponential,jitter 50%;
- 区分「可重试异常」(429/超时/网络)与「不可重试异常」(4xx 业务错误),后者直接抛;
- 不允许降级为部分成功:单源失败 + 重试用完 → raise(对应方案 "失败整个三库任务")。
"""
from __future__ import annotations

import logging
import random
import time
from typing import Callable, Iterable, TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")

# 可重试异常:由调用方传入
DEFAULT_RETRYABLE: tuple[type[BaseException], ...] = (
    TimeoutError,
    ConnectionError,
)


class RetryExhausted(Exception):
    """重试次数耗尽,最后一次的异常透传。"""

    def __init__(self, attempts: int, last_exc: BaseException):
        super().__init__(f"重试 {attempts} 次后仍失败,最后一次异常:{last_exc!r}")
        self.attempts = attempts
        self.last_exc = last_exc


def retry_with_backoff(
    func: Callable[..., T],
    *args,
    max_attempts: int = 3,
    base_delay: float = 0.5,
    backoff: float = 2.0,
    jitter: float = 0.5,
    retryable: Iterable[type[BaseException]] = DEFAULT_RETRYABLE,
    label: str | None = None,
    **kwargs,
) -> T:
    """用指数退避调用 func(*args, **kwargs)。

    Args:
        func:        待调用函数
        max_attempts: 最大尝试次数(含首次)
        base_delay:  首次重试前的等待秒数
        backoff:     每次退避倍数(2.0 = 指数)
        jitter:      0..1,实际延迟 = sleep * (1 ± jitter/2)
        retryable:   可重试异常类元组
        label:       日志标签
    """
    retryable = tuple(retryable)
    last_exc: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return func(*args, **kwargs)
        except retryable as exc:
            last_exc = exc
            if attempt >= max_attempts:
                log.warning(
                    "[%s] 重试 %d 次后仍失败: %s",
                    label or func.__name__, attempt, exc,
                )
                break
            sleep = base_delay * (backoff ** (attempt - 1))
            sleep *= 1 + (random.random() - 0.5) * jitter
            log.warning(
                "[%s] 第 %d 次失败(%s),退避 %.2fs 后重试",
                label or func.__name__, attempt, exc, sleep,
            )
            time.sleep(sleep)
    raise RetryExhausted(max_attempts, last_exc)  # type: ignore[misc]


__all__ = ["retry_with_backoff", "RetryExhausted"]