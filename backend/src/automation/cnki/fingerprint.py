#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""浏览器指纹模拟：UA / 请求头特征的会话级随机化。

反爬原理：所有会话用同一个 UA 长期高频访问，是指纹封禁的典型画像。
本模块维护一组**内部自洽**的浏览器指纹档案（UA 版本 ↔ sec-ch-ua 头严格配对，
Firefox 档案则不带任何 Client Hints 头），会话启动时随机挑一个并**全程保持**
——UA 在会话中途突变本身就是强烈的风控信号，所以随机化发生在会话级而非请求级。

与 config.yaml 的关系：
- ``http.fingerprint.enabled`` 门控（默认开启）；关闭时退回配置文件里的固定指纹，
  行为与改造前完全一致；
- ``http.fingerprint.pool`` 允许运维追加自定义档案（诊断某指纹被拉黑时下架即可）。

本模块不 import crawler（无循环依赖），也不触碰网络——只产出头字典。
"""

from __future__ import annotations

import random
import threading
from dataclasses import dataclass, field


# ========================== 指纹档案 ==========================
@dataclass
class BrowserFingerprint:
    """一份内部自洽的浏览器指纹（头与头之间不能矛盾，否则一眼假）。

    firefox 档案：sec_ch_ua / platform / mobile 置空串，apply 时跳过这些头。
    """

    user_agent: str
    sec_ch_ua: str = ""
    sec_ch_ua_platform: str = ""
    sec_ch_ua_mobile: str = ""
    accept_language: str = "zh-CN,zh;q=0.9"
    # 标识（面板展示用）：如 "chrome133-win"
    name: str = "custom"
    # 从配置 http 段继承的附加字段（uniplatform 等与浏览器无关，直接透传）
    extras: dict = field(default_factory=dict)

    def headers(self) -> dict:
        """生成该指纹对应的完整请求头字典。"""
        h = {
            "User-Agent": self.user_agent,
            "Accept-Language": self.accept_language,
        }
        if self.sec_ch_ua:
            h["sec-ch-ua"] = self.sec_ch_ua
        if self.sec_ch_ua_platform:
            h["sec-ch-ua-platform"] = self.sec_ch_ua_platform
        if self.sec_ch_ua_mobile:
            h["sec-ch-ua-mobile"] = self.sec_ch_ua_mobile
        h.update(self.extras)
        return h

    def apply_to(self, session) -> None:
        """把指纹应用到 requests.Session（替换同名头，保留其余）。"""
        session.headers.update(self.headers())


# ========================== 内置指纹池 ==========================
# 全部为桌面端真实指纹（知网检索端点无移动版，移动 UA 反而异常）。
# config 中为老版本 UA 的加权保留：第一个位置留给"配置指纹"，被选中概率最高。
_ACCEPT_LANGUAGES = (
    "zh-CN,zh;q=0.9",
    "zh-CN,zh;q=0.9,en;q=0.8",
    "zh-CN,zh;q=0.8,en-US;q=0.6,en;q=0.4",
)

BUILTIN_FINGERPRINTS: list[BrowserFingerprint] = [
    BrowserFingerprint(
        name="chrome133-win",
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
        sec_ch_ua='"Not(A:Brand";v="99", "Google Chrome";v="133", "Chromium";v="133"',
        sec_ch_ua_platform='"Windows"',
        sec_ch_ua_mobile="?0",
    ),
    BrowserFingerprint(
        name="chrome131-win",
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        sec_ch_ua='"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
        sec_ch_ua_platform='"Windows"',
        sec_ch_ua_mobile="?0",
    ),
    BrowserFingerprint(
        name="edge131-win",
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
        sec_ch_ua='"Microsoft Edge";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
        sec_ch_ua_platform='"Windows"',
        sec_ch_ua_mobile="?0",
    ),
    BrowserFingerprint(
        name="chrome133-mac",
        user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
        sec_ch_ua='"Not(A:Brand";v="99", "Google Chrome";v="133", "Chromium";v="133"',
        sec_ch_ua_platform='"macOS"',
        sec_ch_ua_mobile="?0",
    ),
    # Firefox 没有 Client Hints：三个 sec-ch 头留空，headers() 自动跳过
    BrowserFingerprint(
        name="firefox135-win",
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:135.0) Gecko/20100101 Firefox/135.0",
        sec_ch_ua="",
        sec_ch_ua_platform="",
        sec_ch_ua_mobile="",
        accept_language="zh-CN,zh;q=0.9,en;q=0.5",
    ),
]


def _from_config(cfg_http: dict) -> BrowserFingerprint:
    """把 config.yaml http 段转成指纹档案（作为池首、高权重兜底）。"""
    return BrowserFingerprint(
        name="config-default",
        user_agent=cfg_http.get("user_agent", ""),
        sec_ch_ua=cfg_http.get("sec_ch_ua", ""),
        sec_ch_ua_platform=cfg_http.get("sec_ch_ua_platform", ""),
        sec_ch_ua_mobile=cfg_http.get("sec_ch_ua_mobile", "?0"),
        accept_language=cfg_http.get("accept_language", "zh-CN,zh;q=0.9"),
        extras={"uniplatform": cfg_http["uniplatform"]} if cfg_http.get("uniplatform") else {},
    )


def _normalize_pool_entry(entry: dict) -> BrowserFingerprint | None:
    """校验运维自定义档案：缺 UA 的条目直接丢弃（防止写出空 UA 头）。"""
    if not isinstance(entry, dict) or not entry.get("user_agent"):
        return None
    return BrowserFingerprint(
        name=entry.get("name") or "custom",
        user_agent=entry["user_agent"],
        sec_ch_ua=entry.get("sec_ch_ua", ""),
        sec_ch_ua_platform=entry.get("sec_ch_ua_platform", ""),
        sec_ch_ua_mobile=entry.get("sec_ch_ua_mobile", ""),
        accept_language=entry.get("accept_language", "zh-CN,zh;q=0.9"),
    )


# ========================== 会话级指纹管理 ==========================
_current: BrowserFingerprint | None = None
_FP_LOCK = threading.Lock()


def init_fingerprint(cfg_http: dict, rng=None) -> BrowserFingerprint:
    """会话启动时挑选指纹（crawler.init() 与热重载时调用）。

    挑选规则：
    - enabled=False → 固定用配置指纹（行为与改造前一致）
    - enabled=True  → 池 = [配置指纹(权重2)] + 内置池 + 自定义池，
      配置指纹权重高——多数会话长得像"老用户"，少数会话换新衣
    """
    global _current
    rng = rng or random.random
    fp_cfg = cfg_http.get("fingerprint") or {}
    base = _from_config(cfg_http)

    if not fp_cfg.get("enabled", True):
        with _FP_LOCK:
            _current = base
        return base

    pool: list[tuple[BrowserFingerprint, float]] = [(base, 2.0)]
    for fp in BUILTIN_FINGERPRINTS:
        pool.append((fp, 1.0))
    for entry in fp_cfg.get("pool") or []:
        fp = _normalize_pool_entry(entry)
        if fp is not None:
            pool.append((fp, 1.0))

    total = sum(w for _, w in pool)
    point = rng() * total
    chosen = pool[-1][0]
    acc = 0.0
    for fp, w in pool:
        acc += w
        if point <= acc:
            chosen = fp
            break
    # 每次会话随机微调 Accept-Language 权重串（进一步打散指纹聚合度）
    if chosen.name != "firefox135-win":
        chosen = BrowserFingerprint(
            name=chosen.name,
            user_agent=chosen.user_agent,
            sec_ch_ua=chosen.sec_ch_ua,
            sec_ch_ua_platform=chosen.sec_ch_ua_platform,
            sec_ch_ua_mobile=chosen.sec_ch_ua_mobile,
            accept_language=random.choice(_ACCEPT_LANGUAGES),
            extras=dict(chosen.extras),
        )
    with _FP_LOCK:
        _current = chosen
    return chosen


def get_current() -> BrowserFingerprint | None:
    """当前会话指纹（init_fingerprint 之前为 None）。"""
    with _FP_LOCK:
        return _current


def current_headers() -> dict:
    """当前指纹的请求头（未初始化时返回空 dict，调用方自行兜底）。"""
    fp = get_current()
    return fp.headers() if fp else {}
