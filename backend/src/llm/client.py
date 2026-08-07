"""MiniMax(OpenAI SDK)客户端封装。

- 通过 OpenAI 兼容接口调用 MiniMax-M3
- 带超时/重试
- 缺失 API key 时抛清晰错误而非默默失败
"""
from __future__ import annotations

import logging
import os
import time

from openai import APIError, APITimeoutError, OpenAI

log = logging.getLogger(__name__)

_client: OpenAI | None = None


def get_client() -> OpenAI:
    """懒加载全局 MiniMax(OpenAI SDK)客户端。"""
    global _client
    if _client is None:
        api_key = os.environ.get("MINIMAX_API_KEY")
        if not api_key:
            raise RuntimeError(
                "缺少 MINIMAX_API_KEY 环境变量。"
                "请在 backend/.env 中配置(参考 .env.example)。"
            )
        base_url = os.environ.get(
            "MINIMAX_BASE_URL", "https://api.minimaxi.com/v1"
        )
        _client = OpenAI(api_key=api_key, base_url=base_url, timeout=60.0)
    return _client


def messages_create(
    system: str,
    user: str,
    max_tokens: int = 4000,
    model: str | None = None,
    max_retries: int = 3,
    temperature: float | None = None,
) -> str:
    """调 MiniMax Chat Completions,带 3 次指数退避重试。

    返回 assistant 消息文本(content 字段)。
    注意:MiniMax-M3 默认开启 thinking,thinking 部分会放在
    reasoning_details 中;如果未启用 reasoning_split,则 content
    字段里会有 <think>...</think> 标签。我们统一剥离 <think> 段。
    """
    client = get_client()
    model = model or os.environ.get("MINIMAX_MODEL", "MiniMax-M3")
    last_err: Exception | None = None
    for attempt in range(max_retries):
        try:
            kwargs = dict(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                max_completion_tokens=max_tokens,
            )
            if temperature is not None:
                kwargs["temperature"] = temperature
            resp = client.chat.completions.create(**kwargs)
            content = resp.choices[0].message.content or ""
            return _strip_think(content)
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


def _strip_think(text: str) -> str:
    """去掉 <think>...</think> 段(如果 MiniMax 把它塞在 content 里)。"""
    if not text:
        return text
    out = []
    depth = 0
    i = 0
    while i < len(text):
        if text.startswith("<think>", i):
            depth += 1
            i += len("<think>")
            continue
        if text.startswith("</think>", i):
            depth -= 1
            i += len("</think>")
            continue
        if depth == 0:
            out.append(text[i])
        i += 1
    return "".join(out).strip()