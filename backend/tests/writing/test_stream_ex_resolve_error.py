# -*- coding: utf-8 -*-
"""messages_stream_ex 错误透传回归(2026-10-27 yielded_any 修复)。

历史 bug:yielded_any 在 try 块内、resolve_provider 之后才初始化;
resolve_provider 抛 RuntimeError(真实场景:用户保存了 base_url/api_key
但未选模型,apply_user_llm 激活运行时覆盖后 resolve_provider 报
"未指定模型")时,except 分支访问未绑定的 yielded_any 抛
UnboundLocalError,掩盖了本应直达用户的原始提示。

修复:yielded_any = False 提到 try 之前。本文件同时锁定 v9.6 既有语义:
已产出正文后流中断必须原样抛出,不得切换 provider 重流。

双模块树说明:backend 与 backend/src 同时在 sys.path,`llm.client` 与
`src.llm.client` 是两个独立模块对象;本文件对两棵树同步 monkeypatch,
与生产/其他测试实际经由哪棵树无关。
"""
import pytest

import llm.client as client

try:
    import src.llm.client as _src_client
except ImportError:  # pragma: no cover - 无 src. 前缀导入环境
    _src_client = None


def _patch_both(monkeypatch, name: str, value) -> None:
    """同一属性在 llm.client 与 src.llm.client 两棵树上同步替换。"""
    monkeypatch.setattr(client, name, value)
    if _src_client is not None and _src_client is not client:
        monkeypatch.setattr(_src_client, name, value)


def test_resolve_provider_error_propagates_verbatim(monkeypatch):
    """resolve_provider 抛 RuntimeError 时应得到原始错误而非 UnboundLocalError。"""
    _patch_both(monkeypatch, "select_providers", lambda req: ("openai",))

    def boom(_pid):
        raise RuntimeError("未指定模型:请在「监控与管理 → LLM 设置」中选择模型后保存。")

    _patch_both(monkeypatch, "resolve_provider", boom)

    with pytest.raises(RuntimeError) as ei:
        list(client.messages_stream_ex(system="s", user="u", max_tokens=10))
    assert "未指定模型" in str(ei.value)
    # UnboundLocalError 是 NameError 子类;若修复缺失会以它逃逸
    assert not isinstance(ei.value, NameError)


def test_resolve_provider_error_still_raises_after_partial_stream(monkeypatch):
    """对照语义:v9.6 已产出正文后中断必须原样抛,不切换 provider 重流。"""
    _patch_both(monkeypatch, "select_providers", lambda req: ("openai",))
    _patch_both(
        monkeypatch, "resolve_provider",
        lambda pid: client.ResolvedProvider(
            id=pid, label=pid, api_key="sk-test", base_url="http://stub",
            model="m", mode="chat",
        ),
    )

    from openai import APIConnectionError

    def stream_then_break(**_kw):
        yield "content", "正文第一段"
        raise APIConnectionError(request=None)

    _patch_both(
        monkeypatch, "_stream_with_resolved", lambda *a, **kw: stream_then_break()
    )

    pieces = []
    with pytest.raises(APIConnectionError):
        for kind, piece in client.messages_stream_ex(system="s", user="u", max_tokens=10):
            pieces.append((kind, piece))
    # 正文增量已交到消费方,异常不得被吞、也不得从头重流产生重复正文
    assert ("content", "正文第一段") in pieces
