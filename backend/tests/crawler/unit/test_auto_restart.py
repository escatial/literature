# -*- coding: utf-8 -*-
"""agent 自愈重启(_run_with_auto_restart)单元测试。

产品语义(2026-09-08 用户需求):本系统是 agent,检索整体失败时必须
自动冷却→重置会话→重启检索,而不是抛错等人。

行为契约:
  - 环境性故障(会话风控/网络/限流)→ 冷却后重启,重启前调用会话重置钩子;
  - 闸口故障(知网改版/超级鹰题分耗尽/cookie 续期无效)与用户停止 → 立即上抛,
    重启只会重复失败或违背用户意图;
  - 重启预算耗尽 → 抛最后一次异常,由外层如实上报;
  - 冷却分段睡眠并检查停止,面板「停止」秒级响应。
"""
import pytest

from automation import cnki_adapter
from automation.cnki.crawler import (
    CnkiCaptchaBalanceError,
    CnkiCookieError,
    CnkiRevisionError,
    CnkiServerBusyError,
    CnkiSessionBlockedError,
)

_Stopped = cnki_adapter._CnkiStopped


def _scripted_inner(calls: list, script: list):
    """按脚本回放的 inner:脚本项为 "ok"(返回 42)或异常实例(抛出)。"""
    def inner():
        calls.append("run")
        outcome = script.pop(0) if script else "ok"
        if outcome == "ok":
            return 42
        raise outcome
    return inner


def _run(inner, *, rounds=2, cooldowns=(111.0, 222.0),
         logs=None, restarts=None, sleeps=None):
    logs = logs if logs is not None else []
    restarts = restarts if restarts is not None else []
    sleeps = sleeps if sleeps is not None else []
    return cnki_adapter._run_with_auto_restart(
        inner,
        rounds=rounds, cooldowns=cooldowns,
        emit_fn=logs.append,
        sleep_fn=lambda s: sleeps.append(s),
        check_stopped=lambda: None,
        before_restart=lambda attempt: restarts.append(attempt),
    )


# ---------- 重启成功路径 ----------

def test_transient_failure_restarts_and_succeeds():
    calls, restarts, sleeps, logs = [], [], [], []
    inner = _scripted_inner(calls, [CnkiSessionBlockedError("零进度空壳(会话风控)"), "ok"])
    assert _run(inner, logs=logs, restarts=restarts, sleeps=sleeps) == 42
    assert calls == ["run", "run"]           # 重启了一次
    assert restarts == [1]                    # 重启钩子带轮次
    assert sum(sleeps) == 111.0               # 第一档冷却(分段合计)
    assert any("自愈" in m for m in logs)     # 用户可见重启日志


def test_generic_network_error_is_restartable():
    calls, restarts, sleeps, logs = [], [], [], []
    inner = _scripted_inner(calls, [ConnectionError("网络中断"), "ok"])
    assert _run(inner, logs=logs, restarts=restarts, sleeps=sleeps) == 42
    assert calls == ["run", "run"]


def test_busy_error_is_restartable():
    calls, restarts, sleeps, logs = [], [], [], []
    inner = _scripted_inner(calls, [CnkiServerBusyError("数据源繁忙"), "ok"])
    assert _run(inner, logs=logs, restarts=restarts, sleeps=sleeps) == 42


# ---------- 不重启路径 ----------

@pytest.mark.parametrize("gate_exc", [
    CnkiRevisionError("知网改版"),
    CnkiCaptchaBalanceError("超级鹰题分不足"),
])
def test_gate_failures_raise_immediately(gate_exc):
    calls, restarts, sleeps, logs = [], [], [], []
    inner = _scripted_inner(calls, [gate_exc])
    with pytest.raises(type(gate_exc)):
        _run(inner, logs=logs, restarts=restarts, sleeps=sleeps)
    assert calls == ["run"]     # 没有重启
    assert restarts == [] and sleeps == []


def test_cookie_error_restarts_with_fresh_session():
    """v9.8:游客会话被知网杀掉(CnkiCookieError)是环境性故障 —— 重启的
    第一个动作就是更换会话凭证,恰是解药;静默速败(07:46 事故)不再发生。"""
    calls, restarts, sleeps, logs = [], [], [], []
    inner = _scripted_inner(
        calls, [CnkiCookieError("登录状态已过期，自动恢复未成功"), "ok"],
    )
    assert _run(inner, logs=logs, restarts=restarts, sleeps=sleeps) == 42
    assert calls == ["run", "run"]
    assert restarts == [1]  # 重启钩子(换会话凭证)被调用


def test_user_stop_raises_immediately():
    calls, restarts, sleeps, logs = [], [], [], []
    inner = _scripted_inner(calls, [_Stopped("用户已手动停止")])
    with pytest.raises(_Stopped):
        _run(inner, logs=logs, restarts=restarts, sleeps=sleeps)
    assert calls == ["run"] and restarts == [] and sleeps == []


def test_stop_during_cooldown_aborts_restart():
    """冷却分段里用户点停止:立即退出,不再发起重启。"""
    calls: list = []
    restarts: list = []
    logs: list = []
    stops = [_Stopped("用户已手动停止")]

    def inner():
        calls.append("run")
        raise CnkiSessionBlockedError("会话风控")

    with pytest.raises(_Stopped):
        cnki_adapter._run_with_auto_restart(
            inner, rounds=2, cooldowns=(111.0,),
            emit_fn=logs.append,
            sleep_fn=lambda s: (_ for _ in ()).throw(stops.pop(0)) if stops else None,
            check_stopped=lambda: None,
            before_restart=lambda a: restarts.append(a),
        )
    assert calls == ["run"] and restarts == []   # 冷却中停止:未重启


def test_budget_exhausted_raises_last_error():
    calls, restarts, sleeps, logs = [], [], [], []
    inner = _scripted_inner(
        calls,
        [CnkiSessionBlockedError("a"), CnkiServerBusyError("b"),
         CnkiSessionBlockedError("第 3 次尝试仍被风控")],
    )
    with pytest.raises(CnkiSessionBlockedError) as ei:
        _run(inner, logs=logs, restarts=restarts, sleeps=sleeps)
    assert "第 3 次" in str(ei.value)
    assert calls == ["run"] * 3               # 总尝试 = 1 + 2 轮重启
    assert restarts == [1, 2]
    assert sum(sleeps) == 111.0 + 222.0       # 两档冷却都用了


# ---------- v9.8 长等待上报(wait_fn) ----------

def test_wait_fn_called_before_restart_cooldown():
    waits: list[tuple[float, str]] = []
    calls: list[str] = []
    inner = _scripted_inner(calls, [CnkiSessionBlockedError("风控"), "ok"])
    cnki_adapter._run_with_auto_restart(
        inner, rounds=2, cooldowns=(120.0,),
        emit_fn=lambda m: None,
        sleep_fn=lambda s: None,
        check_stopped=lambda: None,
        before_restart=lambda a: None,
        wait_fn=lambda sec, reason: waits.append((sec, reason)),
    )
    assert waits == [(120.0, "自愈冷却")]


def test_crawler_notify_wait_threshold_and_callback(monkeypatch):
    from automation.cnki import crawler as cr
    seen: list[tuple[float, str]] = []
    monkeypatch.setattr(cr._wait_local, "callback", lambda s, r: seen.append((s, r)), raising=False)
    cr.notify_wait(5, "太短不上报")     # <10s 不打扰
    cr.notify_wait(15, "退避")
    assert seen == [(15, "退避")]
    # 回调抛异常绝不影响主流程
    monkeypatch.setattr(cr._wait_local, "callback", lambda s, r: (_ for _ in ()).throw(RuntimeError("x")), raising=False)
    cr.notify_wait(20, "异常也要吞")
