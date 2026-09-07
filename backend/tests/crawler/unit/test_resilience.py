# -*- coding: utf-8 -*-
"""单元测试:稳定性基石(分层分类/阶梯退避/断路器三态/告警节流/统一重试入口)。

要点:
- 时钟/随机源/睡眠全部注入假实现:零等待、断言精确
- 断路器名称用 ut- 前缀,避免与其他测试共享的具名断路器互相污染
"""
import requests
import pytest

from automation.cnki import resilience
from automation.cnki.resilience import (
    AlertManager,
    CircuitBreaker,
    CircuitOpenError,
    ErrorClass,
    RetryPolicy,
    classify_exception,
    resilient_call,
)


class FakeClock:
    """可控时钟:手动推进,断路器冷却与告警节流零等待验证。"""

    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _rng_zero(_low, _high):
    """零抖动随机源:退避值 = 退避表精确值,便于断言。"""
    return 0.0


def _http_error(status):
    """构造带响应状态码的 HTTPError(分类规则依赖 response.status_code)。"""
    resp = requests.Response()
    resp.status_code = status
    return requests.exceptions.HTTPError(f"HTTP {status}", response=resp)


# ========================== 异常分类 ==========================
@pytest.mark.parametrize("exc,expected", [
    (CircuitOpenError("x", 1.0), ErrorClass.FATAL),
    (_http_error(429), ErrorClass.RATE_LIMITED),
    (_http_error(503), ErrorClass.RATE_LIMITED),
    (_http_error(500), ErrorClass.TRANSIENT),
    (_http_error(599), ErrorClass.TRANSIENT),
    (_http_error(404), ErrorClass.FATAL),
    (_http_error(400), ErrorClass.FATAL),
    (requests.exceptions.Timeout(), ErrorClass.TRANSIENT),
    (requests.exceptions.ConnectionError(), ErrorClass.TRANSIENT),
    (requests.exceptions.ChunkedEncodingError(), ErrorClass.TRANSIENT),
    (ValueError("业务错误"), ErrorClass.FATAL),
])
def test_classify_exception_builtin_rules(exc, expected):
    assert classify_exception(exc) is expected


class _MyRiskError(RuntimeError):
    """业务自定义异常:验证分类钩子优先于内置规则。"""


def test_classify_exception_registered_hook_priority():
    def _hook(exc):
        return ErrorClass.BLOCKED if isinstance(exc, _MyRiskError) else None

    resilience.register_exception_classifier(_hook)
    assert classify_exception(_MyRiskError("风控")) is ErrorClass.BLOCKED
    # 钩子不认识的异常走内置规则
    assert classify_exception(requests.exceptions.Timeout()) is ErrorClass.TRANSIENT


# ========================== 分层重试策略 ==========================
def test_retry_policy_default_retries():
    policy = RetryPolicy()
    assert policy.retries_for(ErrorClass.TRANSIENT) == 3
    assert policy.retries_for(ErrorClass.RATE_LIMITED) == 2
    assert policy.retries_for(ErrorClass.BLOCKED) == 0    # 风控重试无意义
    assert policy.retries_for(ErrorClass.FATAL) == 0      # 致命立即上抛


def test_retry_policy_delay_backoff_table():
    policy = RetryPolicy()
    # TRANSIENT 阶梯:1/2/4s,超出表长钳制到末位
    assert policy.delay_for(ErrorClass.TRANSIENT, 0, rng=_rng_zero) == 1.0
    assert policy.delay_for(ErrorClass.TRANSIENT, 1, rng=_rng_zero) == 2.0
    assert policy.delay_for(ErrorClass.TRANSIENT, 2, rng=_rng_zero) == 4.0
    assert policy.delay_for(ErrorClass.TRANSIENT, 9, rng=_rng_zero) == 4.0
    # RATE_LIMITED 阶梯:15/45s(跨限流窗口)
    assert policy.delay_for(ErrorClass.RATE_LIMITED, 0, rng=_rng_zero) == 15.0
    assert policy.delay_for(ErrorClass.RATE_LIMITED, 1, rng=_rng_zero) == 45.0


def test_retry_policy_delay_jitter_and_fallback_table():
    policy = RetryPolicy()
    # 抖动 ±20%:rng 恒返 +0.2 → 基准值 ×1.2
    assert policy.delay_for(ErrorClass.TRANSIENT, 0, rng=lambda a, b: 0.2) == pytest.approx(1.2)
    # 缺退避表的层级:按指数 2^n 兜底(自定义 BLOCKED 允许 2 次重试)
    custom = RetryPolicy(max_retries={ErrorClass.BLOCKED: 2})
    assert custom.delay_for(ErrorClass.BLOCKED, 0, rng=_rng_zero) == 1.0
    assert custom.delay_for(ErrorClass.BLOCKED, 1, rng=_rng_zero) == 2.0


# ========================== 断路器三态 ==========================
def test_breaker_opens_after_consecutive_failures():
    clock = FakeClock()
    breaker = CircuitBreaker(name="ut-b1", failure_threshold=3, recovery_timeout=60.0, clock=clock)
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state == CircuitBreaker.CLOSED
    breaker.record_failure()  # 达到阈值 → open
    assert breaker.state == CircuitBreaker.OPEN
    with pytest.raises(CircuitOpenError):
        breaker.allow()
    assert breaker.cooldown_left() == pytest.approx(60.0)


def test_breaker_half_open_probe_success_closes():
    clock = FakeClock()
    breaker = CircuitBreaker(name="ut-b2", failure_threshold=1, recovery_timeout=60.0, clock=clock)
    breaker.record_failure()
    assert breaker.state == CircuitBreaker.OPEN
    clock.advance(59.9)
    assert breaker.state == CircuitBreaker.OPEN      # 冷却未满
    clock.advance(0.2)
    assert breaker.state == CircuitBreaker.HALF_OPEN  # 惰性迁移
    assert breaker.allow() is True                    # 放行探测请求
    breaker.record_success()
    assert breaker.state == CircuitBreaker.CLOSED
    assert breaker.cooldown_left() == 0.0


def test_breaker_half_open_probe_failure_reopens():
    clock = FakeClock()
    breaker = CircuitBreaker(name="ut-b3", failure_threshold=2, recovery_timeout=30.0, clock=clock)
    breaker.record_failure()
    breaker.record_failure()
    clock.advance(30.0)
    assert breaker.state == CircuitBreaker.HALF_OPEN
    breaker.record_failure()  # 探测失败 → 立即重新 open(不等阈值)
    assert breaker.state == CircuitBreaker.OPEN
    clock.advance(30.0)       # 再冷却满 → 可再次探测
    assert breaker.allow() is True


def test_breaker_reset_manual():
    breaker = CircuitBreaker(name="ut-b4", failure_threshold=1, clock=FakeClock())
    breaker.record_failure()
    assert breaker.state == CircuitBreaker.OPEN
    breaker.reset()
    assert breaker.state == CircuitBreaker.CLOSED
    assert breaker.allow() is True


def test_get_breaker_shared_by_name():
    b1 = resilience.get_breaker("ut-shared")
    b2 = resilience.get_breaker("ut-shared")
    assert b1 is b2
    names = [item["name"] for item in resilience.breakers_snapshot()]
    assert "ut-shared" in names


# ========================== 告警中心 ==========================
def test_alert_manager_throttle_and_count():
    clock = FakeClock()
    pushed = []
    manager = AlertManager(cooldown=100.0, clock=clock)
    manager.alert("限流告警", sender=pushed.append)
    manager.alert("限流告警", detail="第二次", sender=pushed.append)  # 节流窗口内:不推送
    assert len(pushed) == 1
    buf = manager.snapshot()
    assert len(buf) == 1
    assert buf[0]["count"] == 2                 # 频次持续累加
    assert buf[0]["detail"] == "第二次"          # 新详情覆盖
    # 跨过节流窗口:再次推送,计数从 1 重新开始
    clock.advance(101.0)
    manager.alert("限流告警", sender=pushed.append)
    assert len(pushed) == 2
    buf = manager.snapshot()
    assert len(buf) == 2                        # 新旧两条独立记录
    assert buf[0]["ts"] == pytest.approx(1101.0)  # snapshot 新→旧
    assert buf[0]["count"] == 1


def test_alert_manager_push_false_and_sender_error_swallowed():
    pushed = []
    manager = AlertManager(cooldown=100.0, clock=FakeClock())

    def _boom(_alert):
        raise RuntimeError("webhook 挂了")

    manager.alert("仅记录", push=False, sender=pushed.append)   # push=False 不推送
    manager.alert("发送异常", sender=_boom)                     # sender 抛错被吞掉
    assert pushed == []
    assert len(manager.snapshot()) == 2


# ========================== 统一分层重试入口 ==========================
def test_resilient_call_success_first_try():
    breaker = CircuitBreaker(name="ut-rc1", clock=FakeClock())
    assert resilient_call(lambda a, b: a + b, args=(1, 2), breaker=breaker) == 3
    assert breaker.state == CircuitBreaker.CLOSED


def test_resilient_call_transient_retry_then_success():
    sleeps = []
    retries = []
    attempts = {"n": 0}

    def _flaky():
        attempts["n"] += 1
        if attempts["n"] <= 2:
            raise requests.exceptions.Timeout()
        return "ok"

    result = resilient_call(
        _flaky,
        sleep_fn=sleeps.append,
        on_retry=lambda cls, attempt, exc, wait: retries.append((cls, attempt)),
    )
    assert result == "ok"
    assert attempts["n"] == 3
    assert len(sleeps) == 2                       # 两次退避
    assert retries == [(ErrorClass.TRANSIENT, 1), (ErrorClass.TRANSIENT, 2)]


def test_resilient_call_gives_up_after_retries_exhausted():
    sleeps = []
    give_ups = []
    policy = RetryPolicy(max_retries={ErrorClass.TRANSIENT: 1})
    always_fail = requests.exceptions.ConnectionError()

    def _on_give_up(exc, cls):
        give_ups.append((exc, cls))

    with pytest.raises(requests.exceptions.ConnectionError):
        resilient_call(
            lambda: (_ for _ in ()).throw(always_fail),
            policy=policy,
            sleep_fn=sleeps.append,
            on_give_up=_on_give_up,
        )
    assert len(sleeps) == 1                       # 重试 1 次后退避了 1 次
    assert give_ups == [(always_fail, ErrorClass.TRANSIENT)]


def test_resilient_call_fatal_no_retry():
    sleeps = []
    give_ups = []
    with pytest.raises(ValueError):
        resilient_call(
            lambda: (_ for _ in ()).throw(ValueError("参数错误")),
            sleep_fn=sleeps.append,
            on_give_up=lambda exc, cls: give_ups.append(cls),
        )
    assert sleeps == []                           # FATAL 零重试零退避
    assert give_ups == [ErrorClass.FATAL]


def test_resilient_call_breaker_trips_and_blocks():
    clock = FakeClock()
    breaker = CircuitBreaker(name="ut-rc2", failure_threshold=1, recovery_timeout=60.0, clock=clock)
    called = {"n": 0}

    def _boom():
        called["n"] += 1
        raise ValueError("FATAL")

    with pytest.raises(ValueError):
        resilient_call(_boom, breaker=breaker)
    assert breaker.state == CircuitBreaker.OPEN   # FATAL 也计入断路器连败
    called["n"] = 0
    with pytest.raises(CircuitOpenError):
        resilient_call(lambda: "ok", breaker=breaker)   # open 态:请求根本不发出
    assert called["n"] == 0


# ========================== 全局告警单例 ==========================
def test_alert_manager_singleton():
    resilience.reset_alert_manager()
    m1 = resilience.get_alert_manager()
    m2 = resilience.get_alert_manager()
    assert m1 is m2
    resilience.reset_alert_manager()
    assert resilience.get_alert_manager() is not m1
