# -*- coding: utf-8 -*-
"""单元测试:代理 IP 池(加载去重/巡检评分/冷却/加权轮换/快照打码/模式解析)。"""
import pytest

from automation.cnki import proxy_pool
from automation.cnki.proxy_pool import ProxyEntry, ProxyPool, _mask_proxy_url, proxy_mode


class FakeClock:
    """可控单调时钟:冷却逻辑零等待验证。"""

    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _ok_checker(url):
    return True, 0.2


def _fail_checker(url):
    return False, 0.0


def _new_pool(checker, clock=None, **kwargs):
    clock = clock or FakeClock()
    pool = ProxyPool(checker=checker, clock=clock, **kwargs)
    return pool, clock


# ========================== 加载与去重 ==========================
def test_load_merges_config_and_env_with_dedup(monkeypatch):
    pool, _ = _new_pool(_ok_checker)
    monkeypatch.setenv("CNKI_PROXY_LIST", "http://b:2, http://c:3 ,,")
    added = pool.load(["http://a:1", "http://b:2"])     # b 重复:只加载一次
    assert added == 3
    assert len(pool) == 3
    added2 = pool.load(["http://a:1"])                  # 全部已存在
    assert added2 == 0


def test_load_empty_env(monkeypatch):
    monkeypatch.delenv("CNKI_PROXY_LIST", raising=False)
    pool, _ = _new_pool(_ok_checker)
    assert pool.load(["http://a:1"]) == 1


# ========================== 巡检与评分 ==========================
def test_check_one_success_scores_up():
    pool, _ = _new_pool(_ok_checker)
    pool.load(["http://a:1"])
    assert pool.check_one("http://a:1") is True
    entry = pool._entries["http://a:1"]
    assert entry.score == 55.0                          # 50 + 5
    assert entry.alive is True
    assert entry.success_count == 1
    assert entry.last_latency == pytest.approx(0.2)


def test_check_one_failures_trigger_cooldown():
    pool, clock = _new_pool(_fail_checker)
    pool.load(["http://a:1"])
    pool.check_one("http://a:1")
    pool.check_one("http://a:1")
    entry = pool._entries["http://a:1"]
    assert (entry.alive, entry.score, entry.consecutive_failures) == (False, 30.0, 2)
    pool.check_one("http://a:1")                        # 连败达 3 → 冷却并清零连败
    assert entry.consecutive_failures == 0
    assert entry.cooldown_until == pytest.approx(clock.now + 600.0)
    assert entry.available(clock.now, 0.0) is False     # 冷却期不可轮换
    clock.advance(601.0)
    assert entry.available(clock.now, 0.0) is False     # 期满仅重获巡检资格:alive 仍 False
    pool._checker = _ok_checker                         # 换健康探测
    assert pool.check_one("http://a:1") is True         # 探测成功 → 复活
    assert (entry.alive, entry.score) == (True, 25.0)   # 20 + 5
    assert entry.available(clock.now, 100.0) is False   # 分数跌破阈值仍剔除


def test_check_all_skips_cooldown_unless_force():
    calls = []

    def _recording(url):
        calls.append(url)
        return True, 0.1

    pool, clock = _new_pool(_recording)
    pool.load(["http://a:1", "http://b:2"])
    pool.mark_cooldown("http://a:1")                    # a 进入冷却
    results = pool.check_all()
    assert set(results.keys()) == {"http://b:2"}        # 冷却中的跳过
    results = pool.check_all(force=True)
    assert set(results.keys()) == {"http://a:1", "http://b:2"}


# ========================== 轮换调度 ==========================
def test_get_proxy_skips_unavailable_and_respects_exclude():
    pool, clock = _new_pool(_ok_checker)
    pool.load(["http://a:1", "http://b:2"])
    pool.mark_cooldown("http://b:2")
    assert pool.get_proxy() == "http://a:1"             # 只剩 a 可用
    assert pool.get_proxy(exclude={"http://a:1"}) is None
    clock.advance(601.0)
    assert pool.get_proxy(exclude={"http://a:1"}) == "http://b:2"   # b 冷却结束回到池


def test_get_proxy_none_when_pool_empty():
    pool, _ = _new_pool(_ok_checker)
    assert pool.get_proxy() is None


def test_report_success_caps_score_at_100():
    pool, _ = _new_pool(_ok_checker)
    pool.load(["http://a:1"])
    pool._entries["http://a:1"].score = 98.0
    pool.report_success("http://a:1", latency=0.3)
    entry = pool._entries["http://a:1"]
    assert entry.score == 100.0                         # 封顶
    assert entry.last_latency == pytest.approx(0.3)


def test_report_failure_three_strikes_cooldown():
    pool, clock = _new_pool(_ok_checker)
    pool.load(["http://a:1"])
    for _ in range(3):
        pool.report_failure("http://a:1")
    entry = pool._entries["http://a:1"]
    assert entry.score == 20.0                          # 50 - 10*3
    assert clock.now < entry.cooldown_until             # 已进冷却
    assert pool.get_proxy() is None                     # 唯一代理冷却中 → 无可用


def test_mark_cooldown_manual_seconds():
    pool, clock = _new_pool(_ok_checker)
    pool.load(["http://a:1"])
    pool.mark_cooldown("http://a:1", seconds=30)
    assert pool.get_proxy() is None
    clock.advance(31.0)
    assert pool.get_proxy() == "http://a:1"


# ========================== 快照与打码 ==========================
def test_snapshot_sorted_and_masked():
    pool, _ = _new_pool(_ok_checker)
    pool.load(["http://user:pass@1.2.3.4:8080", "http://5.6.7.8:90"])
    pool.report_success("http://5.6.7.8:90")            # b 加分 → 排前
    snap = pool.snapshot()
    assert snap[0]["url"] == "http://5.6.7.8:90"        # 按分数降序
    assert snap[1]["url"] == "http://***@1.2.3.4:8080"  # 凭据打码
    assert snap[1]["score"] == 50.0
    assert set(snap[0].keys()) == {
        "url", "score", "alive", "available", "success_count",
        "fail_count", "last_latency", "cooldown_left", "last_check_ts",
    }


def test_mask_proxy_url():
    assert _mask_proxy_url("http://u:p@host:1") == "http://***@host:1"
    assert _mask_proxy_url("socks5://u@host") == "socks5://***@host"
    assert _mask_proxy_url("http://host:8080") == "http://host:8080"   # 无凭据原样
    assert _mask_proxy_url("::::") == "::::"                            # 非法输入不炸


# ========================== 模式解析与全局单例 ==========================
def test_proxy_mode_priority(monkeypatch):
    monkeypatch.delenv("CNKI_PROXY_MODE", raising=False)
    assert proxy_mode({}) == "off"                      # 默认
    assert proxy_mode({"mode": "weird"}) == "off"       # 无环境变量时非法值回落 off
    assert proxy_mode({"mode": "on"}) == "on"           # 配置生效
    assert proxy_mode({"mode": "failover"}) == "failover"
    monkeypatch.setenv("CNKI_PROXY_MODE", "failover")   # 环境变量 > 一切配置
    assert proxy_mode({"mode": "on"}) == "failover"
    assert proxy_mode({"mode": "weird"}) == "failover"  # env 合法时覆盖非法配置
    monkeypatch.setenv("CNKI_PROXY_MODE", "weird")      # env 非法 → off(不回落配置)
    assert proxy_mode({"mode": "on"}) == "off"


def test_init_proxy_pool_starts_loop_only_in_on_mode(monkeypatch):
    calls = []
    monkeypatch.setattr(ProxyPool, "start_health_loop", lambda self, interval=300.0: calls.append(interval))
    proxy_pool.reset_proxy_pool()
    pool = proxy_pool.init_proxy_pool({"mode": "off", "pool": ["http://a:1"]})
    assert calls == []                                  # off:不启巡检线程
    assert len(pool) == 1
    assert proxy_pool.get_proxy_pool() is pool
    proxy_pool.reset_proxy_pool()
    assert proxy_pool.get_proxy_pool() is None
    proxy_pool.init_proxy_pool({"mode": "on", "check_interval": 30, "pool": []})
    assert calls == [30]                                # on:按配置周期启动


def test_proxy_entry_available_semantics():
    entry = ProxyEntry(url="http://a:1", score=10.0, cooldown_until=100.0)
    assert entry.available(now=99.0, threshold=5.0) is False    # 冷却中
    assert entry.available(now=100.0, threshold=5.0) is True    # 恰好到期(边界含等号)
    assert entry.available(now=50.0, threshold=5.0) is False    # 冷却中
    assert entry.available(now=101.0, threshold=10.0) is True   # 分数恰达阈值
    assert entry.available(now=101.0, threshold=10.1) is False  # 分数跌破阈值
    entry.alive = False
    assert entry.available(now=101.0, threshold=5.0) is False   # 探测失败剔除
