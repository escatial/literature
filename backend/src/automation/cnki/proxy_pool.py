#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""代理 IP 池调度：可用性自动校验、健康评分、动态轮换。

三种工作模式（config.yaml → proxy.mode，默认 off 保持直连行为不变）：
- off      直连，池不启用（现有部署/全部现有测试的默认路径）
- on       所有请求经代理池轮换出口
- failover 平时直连；直连请求被限流/风控时，失败重试自动切到代理出口

可用性校验：后台巡检线程按 check_interval 周期用 check_url 探测每个代理
（连通性 + 延迟），巡检与评分手册：
- 探测成功    score +5（封顶 100），记录延迟
- 请求失败    score −10，连续失败 ≥3 进入冷却（cooldown 秒内不参与轮换）
- score 跌破 threshold 的代理被剔除出轮换（保留在池里等人工处置）

本模块不 import crawler；网络探测函数 ``checker`` 可注入（单测零网络）。
"""

from __future__ import annotations

import os
import random
import threading
import time
from dataclasses import dataclass, field


# ========================== 单个代理 ==========================
@dataclass
class ProxyEntry:
    """池内一个代理节点及其运行时健康状态。"""

    url: str
    score: float = 50.0          # 健康分：0 起步，成功+5 / 失败-10
    fail_count: int = 0          # 累计失败次数（观测用）
    consecutive_failures: int = 0
    success_count: int = 0
    last_latency: float = 0.0    # 最近一次探测延迟(秒)
    cooldown_until: float = 0.0  # 冷却截止（clock 单调时刻）
    last_check_ts: float = 0.0   # 最近探测时间(unix，面板展示)
    alive: bool = True           # 最近探测是否连通
    tags: dict = field(default_factory=dict)

    def available(self, now: float, threshold: float) -> bool:
        """是否可参与轮换：未冷却 且 分数未跌破阈值。"""
        return self.alive and self.score >= threshold and now >= self.cooldown_until


# ========================== 代理池 ==========================
class ProxyPool:
    """线程安全的代理池：加载 → 巡检 → 评分 → 轮换。

    :param checker: 探测函数 ``checker(proxy_url) -> (ok: bool, latency: float)``；
        默认实现用 requests 经该代理 GET check_url。单测注入假探测。
    :param clock: 单调时钟（冷却计算用；单测可注入假时钟）
    """

    check_url_default = "https://kns.cnki.net/kns8s/AdvSearch"

    def __init__(
        self,
        check_url: str = "https://kns.cnki.net/kns8s/AdvSearch",
        check_timeout: float = 8.0,
        cooldown: float = 600.0,
        score_threshold: float = 0.0,
        checker=None,
        clock=time.monotonic,
        rng=None,
    ):
        self.check_url = check_url
        self.check_timeout = float(check_timeout)
        self.cooldown = float(cooldown)
        self.score_threshold = float(score_threshold)
        self._checker = checker or self._default_checker
        self._clock = clock
        self._rng = rng or random.random
        self._entries: dict[str, ProxyEntry] = {}
        self._lock = threading.Lock()
        self._health_thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    # ---- 加载 ----
    def load(self, pool: list, env_key: str = "CNKI_PROXY_LIST") -> int:
        """从配置列表 + 环境变量（逗号分隔）加载代理，返回加载条数。去重合并。"""
        urls: list[str] = []
        for u in pool or []:
            if isinstance(u, str) and u.strip():
                urls.append(u.strip())
        env_val = os.environ.get(env_key, "").strip()
        if env_val:
            urls.extend(x.strip() for x in env_val.split(",") if x.strip())
        added = 0
        with self._lock:
            for url in urls:
                if url not in self._entries:
                    self._entries[url] = ProxyEntry(url=url)
                    added += 1
        return added

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    # ---- 健康检查 ----
    @staticmethod
    def _default_checker(proxy_url: str) -> tuple:
        """默认探测：经代理 GET check_url，返回 (是否连通, 延迟秒)。"""
        import requests as _requests

        start = time.monotonic()
        try:
            resp = _requests.get(
                ProxyPool.check_url_default,
                proxies={"http": proxy_url, "https": proxy_url},
                timeout=8,
            )
            ok = resp.status_code < 500
        except Exception:
            ok = False
        return ok, time.monotonic() - start

    def check_one(self, url: str) -> bool:
        """探测单个代理并更新评分（巡检线程与手动触发共用）。"""
        try:
            ok, latency = self._checker(url)
        except Exception:
            ok, latency = False, 0.0
        with self._lock:
            entry = self._entries.get(url)
            if entry is None:
                return False
            entry.last_check_ts = time.time()
            entry.last_latency = latency
            if ok:
                entry.alive = True
                entry.consecutive_failures = 0
                entry.success_count += 1
                entry.score = min(entry.score + 5.0, 100.0)
            else:
                entry.alive = False
                entry.consecutive_failures += 1
                entry.fail_count += 1
                entry.score -= 10.0
                if entry.consecutive_failures >= 3:
                    entry.cooldown_until = self._clock() + self.cooldown
                    entry.consecutive_failures = 0
        return ok

    def check_all(self, force: bool = False) -> dict:
        """全池巡检（阻塞版）。返回 {url: ok}。force=True 时无视冷却立即探测。"""
        with self._lock:
            urls = list(self._entries.keys())
        results = {}
        for url in urls:
            with self._lock:
                if not force and self._clock() < self._entries[url].cooldown_until:
                    continue
            results[url] = self.check_one(url)
        return results

    def start_health_loop(self, interval: float = 300.0) -> None:
        """启动后台巡检守护线程（7*24 部署时保持池健康）。幂等。"""
        with self._lock:
            if self._health_thread and self._health_thread.is_alive():
                return
            self._stop_event.clear()

            def _loop():
                # 启动先立即巡检一轮，之后按周期巡检（可被 stop 提前打断）
                while not self._stop_event.wait(0.5):
                    self.check_all()
                    if self._stop_event.wait(max(float(interval), 30.0)):
                        break

            self._health_thread = threading.Thread(
                target=_loop, name="proxy-health", daemon=True,
            )
            self._health_thread.start()

    def stop_health_loop(self) -> None:
        """停止后台巡检（进程退出/测试收尾）。"""
        self._stop_event.set()

    # ---- 轮换调度 ----
    def get_proxy(self, exclude: set | None = None) -> str | None:
        """按加权分数挑一个可用代理（分数高者概率大）；无可用返回 None。

        :param exclude: 排除集合（本轮请求已失败过的代理，实现轮换跳过）
        """
        exclude = exclude or set()
        now = self._clock()
        with self._lock:
            candidates = [
                e for e in self._entries.values()
                if e.available(now, self.score_threshold) and e.url not in exclude
            ]
        if not candidates:
            return None
        # 加权随机：score<0 视为 1（最低保留权重），避免劣质代理完全饿死
        weights = [max(e.score, 0.0) + 1.0 for e in candidates]
        total = sum(weights)
        point = self._rng() * total
        acc = 0.0
        chosen = candidates[-1]
        for e, w in zip(candidates, weights):
            acc += w
            if point <= acc:
                chosen = e
                break
        return chosen.url

    def report_success(self, url: str, latency: float = 0.0) -> None:
        """业务请求经该代理成功：加分校准（巡检分数之外的真实业务反馈）。"""
        with self._lock:
            entry = self._entries.get(url)
            if entry is None:
                return
            entry.alive = True
            entry.consecutive_failures = 0
            entry.success_count += 1
            entry.last_latency = latency or entry.last_latency
            entry.score = min(entry.score + 5.0, 100.0)

    def report_failure(self, url: str) -> None:
        """业务请求经该代理失败：减分；连续失败 3 次进入冷却。"""
        with self._lock:
            entry = self._entries.get(url)
            if entry is None:
                return
            entry.fail_count += 1
            entry.consecutive_failures += 1
            entry.score -= 10.0
            if entry.consecutive_failures >= 3:
                entry.cooldown_until = self._clock() + self.cooldown
                entry.consecutive_failures = 0

    def mark_cooldown(self, url: str, seconds: float | None = None) -> None:
        """手动冷却（如代理返回 407/被封时立即拉黑一段时间）。"""
        with self._lock:
            entry = self._entries.get(url)
            if entry is not None:
                entry.cooldown_until = self._clock() + (
                    self.cooldown if seconds is None else float(seconds)
                )

    def reset(self) -> None:
        """清空池（热重载配置时调用）。"""
        with self._lock:
            self._entries.clear()

    # ---- 面板导出 ----
    def snapshot(self) -> list[dict]:
        """导出全池状态（监控 API 直接序列化；敏感 URL 打码用户名密码）。"""
        with self._lock:
            items = sorted(self._entries.values(), key=lambda e: -e.score)
        now = self._clock()
        out = []
        for e in items:
            out.append({
                "url": _mask_proxy_url(e.url),
                "score": round(e.score, 1),
                "alive": e.alive,
                "available": e.available(now, self.score_threshold),
                "success_count": e.success_count,
                "fail_count": e.fail_count,
                "last_latency": round(e.last_latency, 3),
                "cooldown_left": round(max(0.0, e.cooldown_until - now), 1),
                "last_check_ts": e.last_check_ts,
            })
        return out


def _mask_proxy_url(url: str) -> str:
    """打码代理 URL 中的凭据：scheme://user:pass@host:port → scheme://***@host:port。"""
    try:
        from urllib.parse import urlsplit, urlunsplit

        parts = urlsplit(url)
        if parts.username:
            netloc = f"***@{parts.hostname}"
            if parts.port:
                netloc += f":{parts.port}"
            return urlunsplit((parts.scheme, netloc, parts.path, parts.query, ""))
    except Exception:
        pass
    return url


# ========================== 模块级单例 ==========================
_pool: ProxyPool | None = None
_POOL_LOCK = threading.Lock()


def init_proxy_pool(proxy_cfg: dict) -> ProxyPool:
    """按配置段构建/重建全局代理池（crawler.init() 调用）。

    :param proxy_cfg: config.yaml 的 proxy 段
    :return: 全局池实例（mode=off 时也返回实例，但 caller 应检查 mode）
    """
    global _pool
    with _POOL_LOCK:
        # v9.6:热重载先停旧池巡检线程——此前直接覆盖全局引用,旧线程仍在
        # 后台 check_all,线程数随 crawler.init() 重载次数无界增长
        if _pool is not None:
            _pool.stop_health_loop()
        _pool = ProxyPool(
            check_url=proxy_cfg.get("check_url", "https://kns.cnki.net/kns8s/AdvSearch"),
            check_timeout=float(proxy_cfg.get("check_timeout", 8.0)),
            cooldown=float(proxy_cfg.get("cooldown", 600.0)),
            score_threshold=float(proxy_cfg.get("score_threshold", 0.0)),
        )
        _pool.load(proxy_cfg.get("pool") or [])
        if proxy_cfg.get("mode") == "on":
            _pool.start_health_loop(float(proxy_cfg.get("check_interval", 300.0)))
        return _pool


def get_proxy_pool() -> ProxyPool | None:
    """全局池（未初始化返回 None；测试隔离用 reset_proxy_pool）。"""
    with _POOL_LOCK:
        return _pool


def proxy_mode(proxy_cfg: dict) -> str:
    """解析生效模式：配置 > 环境变量 CNKI_PROXY_MODE > 默认 off。"""
    mode = os.environ.get("CNKI_PROXY_MODE", "").strip().lower()
    if not mode:
        mode = str((proxy_cfg or {}).get("mode", "off")).strip().lower()
    return mode if mode in ("off", "on", "failover") else "off"


def reset_proxy_pool() -> None:
    """重置全局池（仅测试用）。"""
    global _pool
    with _POOL_LOCK:
        if _pool is not None:
            _pool.stop_health_loop()
        _pool = None
