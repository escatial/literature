# -*- coding: utf-8 -*-
"""运行时 LLM 覆盖的请求侧 SSRF 二次校验(Batch B3)。

背景:api/auth.save_llm_config 保存侧已做 validate_external_url 校验,
但运行时覆盖是模块级 set_runtime_llm —— 测试/管理脚本/未来管理端点
可绕过保存侧直接注入内网地址。所有 chat/stream 调用必经
resolve_provider,在此做第二道防线(TOCTOU 防御的请求侧半环)。

残余风险(已列遗留项):校验通过与真实建连之间存在 DNS rebinding 窗口;
彻底方案是 transport 层 pin 已校验 IP。
"""
import pytest

from llm import client as llm_client


def setup_function(_):
    llm_client.clear_runtime_llm()


def teardown_function(_):
    llm_client.clear_runtime_llm()


def _inject(base_url: str) -> None:
    llm_client.set_runtime_llm(
        kind="openai", base_url=base_url,
        api_key="sk-test-" + "a" * 16, model="test-model",
    )


def test_loopback_override_rejected_at_resolve(monkeypatch):
    """绕过保存侧直接注入环回地址:resolve_provider 必须拒绝。"""
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    _inject("http://127.0.0.1:9000/v1")
    with pytest.raises(RuntimeError) as ei:
        llm_client.resolve_provider(None)
    assert "内网" in str(ei.value) or "环回" in str(ei.value) or "拒绝" in str(ei.value)


def test_private_cidr_override_rejected_at_resolve(monkeypatch):
    """注入私有网段字面 IP:同样被请求侧拦截。"""
    _inject("http://192.168.1.10:8000/v1")
    with pytest.raises(RuntimeError):
        llm_client.resolve_provider(None)


def test_metadata_endpoint_hostname_rejected(monkeypatch):
    """本地/内部主机名(元数据端点惯用名)被拒。"""
    _inject("http://metadata.internal:80/v1")
    with pytest.raises(RuntimeError):
        llm_client.resolve_provider(None)


def test_public_override_passes_resolve(monkeypatch):
    """公网地址:请求侧校验放行,解析结果与注入一致。"""
    _inject("https://llm.example.com/v1")
    rp = llm_client.resolve_provider(None)
    assert rp.base_url == "https://llm.example.com/v1"
    assert rp.mode == "chat"


# ========================== 出站请求 guard(纵深防御层) ==========================

def _random_key() -> str:
    import secrets
    return "sk-test-" + secrets.token_hex(16)


def test_guard_rejects_loopback_url():
    """出站 guard:环回地址直接拒绝。"""
    from llm.client import _guard_outbound_url
    with pytest.raises(RuntimeError) as ei:
        _guard_outbound_url("http://127.0.0.1:9000/v1/messages")
    assert "拒绝" in str(ei.value)


def test_guard_allows_public_gateway_url():
    """公网网关(含 api.minimax.cn 真实网关)放行;DNS 暂不可达时沿用
    既有 NXDOMAIN 放行语义——两条路径都不得抛错。"""
    from llm.client import _guard_outbound_url
    url = _guard_outbound_url("https://api.minimax.cn/anthropic/v1/messages")
    assert url == "https://api.minimax.cn/anthropic/v1/messages"


def test_guard_rejects_link_local_metadata():
    """云元数据端点(169.254.169.254,link-local)被拒。"""
    from llm.client import _guard_outbound_url
    with pytest.raises(RuntimeError):
        _guard_outbound_url("http://169.254.169.254/latest/meta-data/")


def test_anthropic_request_rejects_internal_base(monkeypatch):
    """绕过 resolve_provider 直接注入内网 base_url:httpx.post 触达前被拦。"""
    import httpx

    import llm.client as lc

    def _boom(*args, **kwargs):
        raise AssertionError("httpx.post 不应被触达")

    monkeypatch.setattr(httpx, "post", _boom)
    with pytest.raises(RuntimeError):
        lc._anthropic_request("http://127.0.0.1:9000", _random_key(), {}, timeout=1.0)


def test_anthropic_stream_rejects_internal_base(monkeypatch):
    """流式同径:内网 base_url 在建流前被拦。"""
    import httpx

    import llm.client as lc

    def _boom(*args, **kwargs):
        raise AssertionError("httpx.stream 不应被触达")

    monkeypatch.setattr(httpx, "stream", _boom)
    with pytest.raises(RuntimeError):
        list(lc._anthropic_stream("http://192.168.1.5:8000", _random_key(), {}, timeout=1.0))


def test_fetch_models_for_rejects_internal_base(monkeypatch):
    """fetch_models_for:内网 base_url 在 urlopen 触达前被拦。"""
    import urllib.request

    import llm.client as lc

    def _boom(*args, **kwargs):
        raise AssertionError("urlopen 不应被触达")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    with pytest.raises(RuntimeError):
        lc.fetch_models_for("openai", "http://127.0.0.1:8000", _random_key())
