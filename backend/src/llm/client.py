"""Anthropic 客户端封装。

- 简化消息创建 API
- 带超时/重试
- 缺失 API key 时抛清晰错误而非默默失败
"""
from __future__ import annotations

import logging
import os
import time

from anthropic import APIError, APITimeoutError, Anthropic

log = logging.getLogger(__name__)

_client: Anthropic | None = None


def get_client() -> Anthropic:
    """懒加载全局 Anthropic 客户端。"""
    global _client
    if _client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError(
                "缺少 ANTHROPIC_API_KEY 环境变量。"
                "请在 backend/.env 中配置(参考 .env.example)。"
            )
        _client = Anthropic(api_key=api_key, timeout=60.0)
    return _client


def messages_create(
    system: str,
    user: str,
    max_tokens: int = 4000,
    model: str | None = None,
    max_retries: int = 3,
) -> str:
    """调 Anthropic Messages API,带 3 次指数退避重试。

    返回首个文本块的内容(纯字符串)。
    """
    client = get_client()
    model = model or os.environ.get("MODEL_NAME", "claude-3-5-sonnet-20241022")
    last_err: Exception | None = None
    for attempt in range(max_retries):
        try:
            resp = client.messages.create(
                model=model,
                system=system,
                messages=[{"role": "user", "content": user}],
                max_tokens=max_tokens,
            )
            # 兼容返回内容是分段的情况
            text_parts = [
                block.text for block in resp.content if getattr(block, "type", None) == "text"
            ]
            return "".join(text_parts)
        except APITimeoutError as e:
            last_err = e
            log.warning("LLM 超时(第 %s 次): %s", attempt + 1, e)
            time.sleep(2 ** attempt)
        except APIError as e:
            last_err = e
            log.warning("LLM API 错误(第 %s 次): %s", attempt + 1, e)
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
    log.error("LLM 调用全部重试失败: %s", last_err)
    raise last_err  # type: ignore[misc]
