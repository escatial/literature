#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""fingerprint 单元测试：指纹池加权选择 / 配置指纹兜底 / 条目校验 / 会话应用。

测试手法：
- rng 注入常数 → 加权选择完全确定性（总权重 7.0 = 配置 2.0 + 内置 5×1.0）；
- monkeypatch 模块级 _current → 会话级状态隔离，零全局污染；
- 非 firefox 指纹选中后会用 random.choice 重建 Accept-Language（不可控），
  故断言只看 UA 与 name，不看 accept_language。
"""
import pytest
import requests

from automation.cnki import fingerprint as fp


@pytest.fixture(autouse=True)
def _clean_current(monkeypatch):
    """每个用例前后都把会话级当前指纹清空（monkeypatch 结束自动恢复原值）。"""
    monkeypatch.setattr(fp, "_current", None)
    yield


# ========================== 指纹档案头生成 ==========================
def test_headers_includes_client_hints_and_extras():
    """chrome 系档案：UA + Client Hints 三件套 + extras 透传齐全。"""
    f = fp.BrowserFingerprint(
        user_agent="ua-x", sec_ch_ua='"A"', sec_ch_ua_platform='"Windows"',
        sec_ch_ua_mobile="?0", extras={"uniplatform": "CNKI"},
    )
    h = f.headers()
    assert h["User-Agent"] == "ua-x"
    assert h["sec-ch-ua"] == '"A"'
    assert h["sec-ch-ua-platform"] == '"Windows"'
    assert h["sec-ch-ua-mobile"] == "?0"
    assert h["uniplatform"] == "CNKI"
    assert "Accept-Language" in h


def test_headers_firefox_has_no_client_hints():
    """firefox 档案：三个 sec-ch 头留空 → headers() 自动跳过。"""
    h = fp.BrowserFingerprint(user_agent="ff-ua").headers()
    assert h["User-Agent"] == "ff-ua"
    assert "sec-ch-ua" not in h
    assert "sec-ch-ua-platform" not in h
    assert "sec-ch-ua-mobile" not in h


def test_apply_to_updates_session_and_keeps_other_headers():
    """apply_to 只覆盖指纹涉及的头，会话既有头保留。"""
    s = requests.Session()
    s.headers["X-Keep"] = "1"
    fp.BrowserFingerprint(user_agent="ua-apply").apply_to(s)
    assert s.headers["User-Agent"] == "ua-apply"
    assert s.headers["X-Keep"] == "1"


# ========================== 配置指纹 / 条目校验 ==========================
def test_from_config_defaults():
    """_from_config：sec_ch_ua_mobile 默认 "?0"，无 uniplatform 不造 extras 键。"""
    f = fp._from_config({"user_agent": "cfg-ua"})
    assert f.name == "config-default"
    assert f.user_agent == "cfg-ua"
    assert f.sec_ch_ua_mobile == "?0"
    assert f.extras == {}


def test_from_config_uniplatform_passthrough():
    f = fp._from_config({"user_agent": "u", "uniplatform": "CNKI"})
    assert f.extras == {"uniplatform": "CNKI"}


def test_normalize_pool_entry_rejects_bad_entries():
    """运维自定义档案校验：非 dict / 缺 UA 一律丢弃，防止写出空 UA 头。"""
    assert fp._normalize_pool_entry({}) is None
    assert fp._normalize_pool_entry("not-a-dict") is None
    assert fp._normalize_pool_entry({"name": "no-ua"}) is None


def test_normalize_pool_entry_accepts_valid():
    f = fp._normalize_pool_entry({"user_agent": "ops-ua", "sec_ch_ua": '"O"'})
    assert f is not None
    assert f.user_agent == "ops-ua"
    assert f.name == "custom"          # 未提供 name 时用默认标识
    assert f.sec_ch_ua == '"O"'


# ========================== init_fingerprint：开关与池选择 ==========================
def test_init_disabled_returns_config_fingerprint():
    """enabled=False：固定配置指纹（行为与改造前一致），rng 不参与。"""
    cfg = {"user_agent": "cfg-ua", "fingerprint": {"enabled": False}}
    f = fp.init_fingerprint(cfg, rng=lambda: 0.99)
    assert f.name == "config-default"
    assert f.user_agent == "cfg-ua"
    assert fp.get_current() is f
    assert fp.current_headers()["User-Agent"] == "cfg-ua"


def test_current_headers_empty_before_init():
    """未初始化时 current_headers 返回空 dict（调用方自行兜底）。"""
    assert fp.current_headers() == {}


def test_init_weighted_pool_zero_point_picks_config():
    """rng=0.0 → point=0 落在累计 2.0 的配置指纹区段（权重最高）。"""
    cfg = {"user_agent": "cfg-ua", "fingerprint": {"enabled": True}}
    f = fp.init_fingerprint(cfg, rng=lambda: 0.0)
    assert f.name == "config-default"
    assert f.user_agent == "cfg-ua"


def test_init_weighted_pool_mid_point_picks_builtin():
    """rng=0.5 → point=3.5：累计 2/3/4/5/6/7 → 命中 chrome131-win。"""
    cfg = {"user_agent": "cfg-ua", "fingerprint": {"enabled": True}}
    f = fp.init_fingerprint(cfg, rng=lambda: 0.5)
    assert f.name == "chrome131-win"
    assert "Chrome/131" in f.user_agent


def test_init_weighted_pool_high_point_picks_firefox_no_rebuild():
    """rng=0.999 → point≈6.99 → 末位 firefox；firefox 不重建（accept_language 固定）。"""
    cfg = {"user_agent": "cfg-ua", "fingerprint": {"enabled": True}}
    f = fp.init_fingerprint(cfg, rng=lambda: 0.999)
    assert f.name == "firefox135-win"
    assert "Firefox/135" in f.user_agent
    assert f.accept_language == "zh-CN,zh;q=0.9,en;q=0.5"
    assert "sec-ch-ua" not in f.headers()


def test_init_custom_pool_invalid_dropped_and_valid_selected():
    """自定义池：缺 UA 条目被丢弃（总权重 7+1=8.0），point=7.2 命中自定义条目。"""
    cfg = {
        "user_agent": "cfg-ua",
        "fingerprint": {
            "enabled": True,
            "pool": [
                {"user_agent": "ops-ua", "name": "ops-fp"},
                {"name": "no-ua-dropped"},
            ],
        },
    }
    f = fp.init_fingerprint(cfg, rng=lambda: 0.9)
    assert f.name == "ops-fp"
    assert f.user_agent == "ops-ua"
    assert fp.get_current() is f
