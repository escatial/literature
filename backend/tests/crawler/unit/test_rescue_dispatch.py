"""补漏调度(_run_with_rescue)单元测试:失败式子不丢单、轮间冷却、缺口如实返回。

背景(2026-09 生产反馈):旧预检循环里式子命中限流空壳就 continue 换下一条、
连续 3 条失败直接中止——限流期跑完用户拿到的是「静默缺失」甚至 0 篇。
补漏调度把失败式子记入队列,首轮后按 60/120/300s 冷却逐轮重试,直到
补回/达标/轮数耗尽(返回缺口清单)。本文件以脚本化 fetcher + 记录型 sleep
离线确定性重放全部调度路径,不触网。
"""
import pytest

from automation import cnki_adapter
from automation.cnki.crawler import (
    CnkiCaptchaBalanceError,
    CnkiCookieError,
    CnkiServerBusyError,
)


def _item(query: str, i: int) -> dict:
    return {"title": f"{query}-{i}", "url": f"https://kns.cnki.net/kcms2/{query}/{i}"}


def _script_fetcher(script: dict, calls: list):
    """按式子脚本回放的 fetcher。

    脚本值:int=命中条数(含 0)、"busy"=抛限流空壳、"fail"=抛偶发异常、
    Exception 实例=原样抛出(闸口故障);脚本耗尽后默认成功返回 1 条。
    """
    def fetcher(query: str) -> list[dict]:
        calls.append(query)
        seq = script.setdefault(query, [1])
        outcome = seq.pop(0) if seq else 1
        if outcome == "busy":
            raise CnkiServerBusyError("第1页连续 3 次返回'请稍后重试'空壳(知网限流/风控)")
        if outcome == "fail":
            raise RuntimeError("网络抖动")
        if isinstance(outcome, Exception):
            raise outcome
        return [_item(query, i) for i in range(int(outcome))]
    return fetcher


def _run(queries, script, *, target=100, rounds=cnki_adapter._RESCUE_ROUNDS):
    """驱动调度器的标准夹具:返回 (merged, missing, calls, sleeps, logs)。"""
    calls: list[str] = []
    sleeps: list[float] = []
    logs: list[str] = []
    merged, missing = cnki_adapter._run_with_rescue(
        queries,
        _script_fetcher(script, calls),
        emit_fn=logs.append,
        sleep_fn=sleeps.append,
        check_stopped=lambda: None,
        delay_seconds=2.0,
        target_count=target,
        rescue_rounds=rounds,
    )
    return merged, missing, calls, sleeps, logs


def test_first_round_all_success_no_rescue():
    """全部式子首轮成功:无补漏、无缺口,式间仅常规间隔。"""
    merged, missing, calls, sleeps, logs = _run(["q1", "q2", "q3"], {})
    assert missing == []
    assert len(merged) == 3
    assert calls == ["q1", "q2", "q3"]
    assert sleeps == [2.0, 2.0]  # 仅式间 delay_seconds
    assert any("已获取 1 条" in m for m in logs)


def test_busy_query_rescued_next_round():
    """首轮空壳的式子入队,补漏第 1 轮冷却 60s 后重试成功 → 无缺口。"""
    merged, missing, calls, sleeps, logs = _run(["q1", "q2"], {"q2": ["busy", 2]})
    assert missing == []
    assert calls == ["q1", "q2", "q2"]  # q2 首轮失败 + 补漏重试一次
    assert len(merged) == 3  # q1 一条 + q2 两条
    # 式间 delay(q2 前) → 空壳后 30s 冷却 → 补漏轮 1 冷却 60s(按 5s 分段 ×12)
    assert sleeps == [2.0, 30.0] + [5.0] * 12
    assert any("[补全]" in m for m in logs)


def test_three_busy_aborts_first_round_and_unrun_queued():
    """连续 3 条空壳:首轮即停、未跑式子一并入队补漏,一个不丢。"""
    queries = ["q1", "q2", "q3", "q4", "q5"]
    # q2/q3/q4 前 4 次调用全空壳(首轮 1 次 + 补漏 3 轮),q5 无脚本(补漏即成功)
    script = {q: ["busy"] * 4 for q in ("q2", "q3", "q4")}
    merged, missing, calls, sleeps, logs = _run(queries, script)
    assert calls[:4] == ["q1", "q2", "q3", "q4"]  # 首轮止步 q4
    assert calls.count("q5") == 1  # q5 未跑,由补漏轮补跑并成功
    assert missing == ["q2", "q3", "q4"]  # 3 轮耗尽后仍持续空壳的如实返回
    assert len(merged) == 2  # q1 + q5
    # 首轮:q2/q3/q4 前各一次式间 delay,q2/q3 失败后各 30s 冷却,q4 失败即中止(不再冷却)
    # 补漏轮 1 为 4 条(q2/q3/q4/q5),轮 2/3 只剩 3 条(q5 已补回移出);冷却 60/120/300s 全部按 5s 分段
    assert sleeps[:5] == [2.0, 30.0, 2.0, 30.0, 2.0]
    assert sleeps[5:] == ([5.0] * 12 + [2.0] * 3) + ([5.0] * 24 + [2.0] * 2) + ([5.0] * 60 + [2.0] * 2)


def test_exhausted_rounds_keep_missing_and_cooldown_ladder():
    """连续 3 条空壳中止首轮,所有式子持续空壳走满 3 轮冷却阶梯,缺口完整保留。"""
    script = {q: ["busy"] * 4 for q in ("q1", "q2", "q3")}
    merged, missing, calls, sleeps, logs = _run(["q1", "q2", "q3"], script)
    assert missing == ["q1", "q2", "q3"]
    assert merged == {}
    # 首轮:q1 busy 后 30s → 式间 delay → q2 busy 后 30s → 式间 delay → q3 busy 即中止(不再冷却)
    assert sleeps[:4] == [30.0, 2.0, 30.0, 2.0]
    # 三轮冷却 60/120/300s 全部按 5s 分段(12/24/60 段),轮内 3 条重试间各 2 次 delay
    assert sleeps[4:] == ([5.0] * 12 + [2.0] * 2) + ([5.0] * 24 + [2.0] * 2) + ([5.0] * 60 + [2.0] * 2)
    assert any("首轮已暂停" in m for m in logs)


def test_target_reached_stops_and_clears_missing():
    """达标即停:后续式子不再尝试,缺口清空(达标后缺口无意义)。"""
    merged, missing, calls, sleeps, logs = _run(["q1", "q2", "q3"], {}, target=1)
    assert calls == ["q1"]
    assert missing == []
    assert len(merged) == 1
    assert sleeps == []
    assert any("已达目标篇数（1 篇）" in m for m in logs)


def test_general_exception_queued_not_dropped():
    """偶发异常(网络抖动)同样入补漏队列,不静默丢单。"""
    merged, missing, calls, sleeps, _ = _run(["q1"], {"q1": ["fail", 2]})
    assert missing == []
    assert calls == ["q1", "q1"]
    assert len(merged) == 2


def test_gate_failure_propagates_in_first_round():
    """闸口故障(cookie 失效等)首轮即原样上抛,不进补漏。"""
    queries = ["q1"]
    script = {"q1": [CnkiCookieError("cookie 自动续期无效")]}
    calls: list[str] = []
    with pytest.raises(CnkiCookieError):
        cnki_adapter._run_with_rescue(
            queries,
            _script_fetcher(script, calls),
            emit_fn=lambda m: None,
            sleep_fn=lambda s: None,
            check_stopped=lambda: None,
            delay_seconds=2.0,
            target_count=10,
        )
    assert calls == ["q1"]


def test_gate_failure_propagates_in_rescue_round():
    """闸口故障在补漏轮出现(超级鹰余额耗尽)同样原样上抛。"""
    script = {"q1": ["busy", CnkiCaptchaBalanceError("题分不足")]}
    calls: list[str] = []
    with pytest.raises(CnkiCaptchaBalanceError):
        cnki_adapter._run_with_rescue(
            ["q1"],
            _script_fetcher(script, calls),
            emit_fn=lambda m: None,
            sleep_fn=lambda s: None,
            check_stopped=lambda: None,
            delay_seconds=2.0,
            target_count=10,
        )
    assert calls == ["q1", "q1"]  # 首轮空壳入队 + 补漏重试时抛闸口


def test_cooldown_sleep_chunked():
    """长冷却按 5s 分段睡眠,保证停止请求秒级可见。"""
    sleeps: list[float] = []
    cnki_adapter._sleep_in_chunks(sleeps.append, lambda: None, 60.0, chunk=5.0)
    assert sleeps == [5.0] * 12
    # 不足一段的余量单独成段
    sleeps2: list[float] = []
    cnki_adapter._sleep_in_chunks(sleeps2.append, lambda: None, 12.0, chunk=5.0)
    assert sleeps2 == [5.0, 5.0, 2.0]


def test_cooldown_sleep_stoppable():
    """分段睡眠中停止请求立即传播,不再睡满整段。"""
    sleeps: list[float] = []
    counter = {"n": 0}

    def check_stopped():
        counter["n"] += 1
        if counter["n"] == 2:
            raise ValueError("用户已手动停止")

    with pytest.raises(ValueError, match="用户已手动停止"):
        cnki_adapter._sleep_in_chunks(sleeps.append, check_stopped, 60.0, chunk=5.0)
    assert sleeps == [5.0]  # 只睡了第一段就被停止打断


# ---------- v9.8 列表阶段进度回调 ----------

def test_progress_fn_reports_cumulative_found_per_absorb():
    """列表阶段进度:每式吸收后上报(累计条目, 目标),前端进度条不再冻结。"""
    calls: list[str] = []
    progress: list[tuple[int, int]] = []
    merged, missing = cnki_adapter._run_with_rescue(
        ["q1", "q2"],
        _script_fetcher({"q2": [2]}, calls),
        emit_fn=lambda m: None,
        sleep_fn=lambda s: None,
        check_stopped=lambda: None,
        delay_seconds=0.0,
        target_count=100,
        rescue_rounds=0,
        progress_fn=lambda done, total: progress.append((done, total)),
    )
    assert missing == []
    assert len(merged) == 3
    # q1 吸收 1 条 → (1,100);q2 吸收 2 条 → (3,100)
    assert progress == [(1, 100), (3, 100)]


def test_progress_fn_failure_never_breaks_retrieval():
    """进度回调抛异常必须被吞掉 —— 进度上报绝不影响检索主流程。"""
    calls: list[str] = []

    def boom(done, total):
        raise RuntimeError("SSE 队列瞬断")

    merged, missing = cnki_adapter._run_with_rescue(
        ["q1"],
        _script_fetcher({}, calls),
        emit_fn=lambda m: None,
        sleep_fn=lambda s: None,
        check_stopped=lambda: None,
        delay_seconds=0.0,
        target_count=100,
        rescue_rounds=0,
        progress_fn=boom,
    )
    assert missing == []
    assert len(merged) == 1
