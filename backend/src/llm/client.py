"""MiniMax(OpenAI SDK)客户端封装。
- 通过 OpenAI 兼容接口调用 MiniMax-M3
- 带超时 + 重试
- 缺少 API key 时抛清晰错误而非静默失败
"""
from __future__ import annotations

import logging
import os
import re
import time
from typing import Generator

from openai import APIConnectionError, APIError, APITimeoutError, OpenAI

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
        _client = OpenAI(api_key=api_key, base_url=base_url, timeout=300.0)
    return _client


def messages_stream(
    system: str,
    user: str,
    max_tokens: int = 4000,
    model: str | None = None,
    temperature: float | None = None,
) -> Generator[str, None, None]:
    """流式调 MiniMax Chat Completions,逐块返回增量文本。"""
    model = model or os.environ.get("MINIMAX_MODEL", "MiniMax-M3")
    client = get_client()
    kwargs = dict(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        max_completion_tokens=max_tokens,
        stream=True,
    )
    if temperature is not None:
        kwargs["temperature"] = temperature

    for chunk in client.chat.completions.create(**kwargs):
        if not getattr(chunk, "choices", None):
            continue
        delta = chunk.choices[0].delta
        piece = getattr(delta, "content", None)
        if not piece:
            continue
        if isinstance(piece, str):
            yield piece
            continue
        if isinstance(piece, list):
            text_parts: list[str] = []
            for item in piece:
                text = getattr(item, "text", None)
                if text:
                    text_parts.append(str(text))
                elif isinstance(item, dict) and item.get("text"):
                    text_parts.append(str(item["text"]))
            if text_parts:
                yield "".join(text_parts)


def messages_create(
    system: str,
    user: str,
    max_tokens: int = 4000,
    model: str | None = None,
    max_retries: int = 3,
    temperature: float | None = None,
    response_format: dict | None = None,
) -> str:
    """调 MiniMax Chat Completions,带 3 次指数退避重试。
    返回 assistant 消息文本(content 字段)。
    可选 response_format: 传给 OpenAI SDK 用于结构化输出(JSON schema)。
    """
    model = model or os.environ.get("MINIMAX_MODEL", "MiniMax-M3")
    last_err: Exception | None = None
    for attempt in range(max_retries):
        try:
            client = get_client()
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
            if response_format is not None:
                kwargs["response_format"] = response_format
            resp = client.chat.completions.create(**kwargs)
            content = resp.choices[0].message.content or ""
            return normalize_model_output(content)
        except APIConnectionError as e:
            global _client
            last_err = e
            log.warning("LLM 连接错误(第 %s 次): %s", attempt + 1, e)
            if _client is not None:
                try:
                    _client.close()
                except Exception:
                    pass
            _client = None
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
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


def normalize_model_output(text: str) -> str:
    """统一清洗模型输出,供一次性/流式两种调用复用。"""
    cleaned = _strip_code_fence(_strip_think(text))
    return _strip_meta_reasoning_prefix(cleaned)


def _strip_think(text: str) -> str:
    """去掉 <think>...</think> 段(如果 MiniMax 把它塞在 content 里)。

    注意: MiniMax-M3 在长文本场景下经常把**正文主体也写进 think 块**
    (</think> 出现在文末, 或只在块外留个引用标记), 此时直接剥掉会丢正文。
    因此做启发式保护:
      1. 剥 think 后为空 → 正文必在 think 内, 返回去掉标签的全文;
      2. 剥 think 后无中文字符且 think 内明显更长 → 正文主体在 think 内, 回退;
      3. think 内远长于块外(>3 倍且块外 <200 字) → 回退保留 think 内内容。
    """
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
    stripped = "".join(out).strip()
    if "<think>" not in text:
        return stripped
    inner = re.sub(r"</?think>", "", text).strip()
    if not stripped:
        return inner
    if not re.search(r"[\u4e00-\u9fff]", stripped) and len(inner) > len(stripped):
        return inner
    if len(stripped) < 200 and len(inner) > max(300, len(stripped) * 3):
        return inner
    return stripped


def _strip_code_fence(text: str) -> str:
    """去掉模型常见的 ```json ... ``` 围栏, 但绝不截断普通正文。"""
    if not text:
        return text
    t = text.strip()
    if not t.startswith("```"):
        return t
    first_nl = t.find("\n")
    if first_nl != -1:
        t = t[first_nl + 1 :]
    if t.endswith("```"):
        t = t[:-3]
    t = t.strip()
    for opener, closer in (("{", "}"), ("[", "]")):
        start = t.find(opener)
        if start == -1:
            continue
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(t)):
            ch = t[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == opener:
                depth += 1
            elif ch == closer:
                depth -= 1
                if depth == 0:
                    return t[start : i + 1].strip()
    return t


def _strip_meta_reasoning_prefix(text: str) -> str:
    """剥离模型偶发输出的英文自我分析前缀。"""
    if not text:
        return text
    head = text[:400]
    if not re.search(
        r"The user wants me|Let me|I need to|Actually,|Wait -|For citations|Key instructions",
        head,
        re.IGNORECASE,
    ):
        return text

    blocks = [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()]
    if len(blocks) < 2:
        return text

    start_idx = None
    for i, block in enumerate(blocks):
        chinese_chars = len(re.findall(r"[\u4e00-\u9fff]", block))
        if chinese_chars >= 20:
            start_idx = i
            break
    if start_idx is None or start_idx == 0:
        return text
    return "\n\n".join(blocks[start_idx:]).strip()
