"""多 Provider LLM 客户端封装。

支持 MiniMax / DeepSeek / GPT(OpenAI Compatible)。
默认 provider 由 LLM_PROVIDER 环境变量决定,调用方也可显式传入 provider。
"""
from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Generator

from openai import APIConnectionError, APIError, APITimeoutError, OpenAI

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProviderSpec:
    id: str
    label: str
    env_key: str
    env_base: str
    env_model: str
    default_base: str
    default_model: str
    mode: str  # "chat" 或 "responses"


@dataclass(frozen=True)
class ResolvedProvider:
    id: str
    label: str
    api_key: str
    base_url: str
    model: str
    mode: str


PROVIDER_SPECS: dict[str, ProviderSpec] = {
    "minimax": ProviderSpec(
        id="minimax",
        label="MiniMax",
        env_key="MINIMAX_API_KEY",
        env_base="MINIMAX_BASE_URL",
        env_model="MINIMAX_MODEL",
        default_base="https://api.minimaxi.com/v1",
        default_model="MiniMax-M3",
        mode="chat",
    ),
    "deepseek": ProviderSpec(
        id="deepseek",
        label="DeepSeek",
        env_key="DEEPSEEK_API_KEY",
        env_base="DEEPSEEK_BASE_URL",
        env_model="DEEPSEEK_MODEL",
        default_base="https://api.deepseek.com/v1",
        default_model="deepseek-chat",
        mode="chat",
    ),
    "gpt": ProviderSpec(
        id="gpt",
        label="GPT",
        env_key="GPT_API_KEY",
        env_base="GPT_BASE_URL",
        env_model="GPT_MODEL",
        default_base="https://max.jojocode.com/v1",
        default_model="gpt-4o-mini",
        mode="responses",
    ),
}

_clients: dict[str, OpenAI] = {}


def get_default_provider() -> str:
    return os.environ.get("LLM_PROVIDER", "minimax").strip().lower() or "minimax"


def resolve_provider(provider: str | None = None) -> ResolvedProvider:
    """读取 provider 配置,校验 API key,并返回统一配置。"""
    provider_id = (provider or get_default_provider()).strip().lower()
    spec = PROVIDER_SPECS.get(provider_id)
    if spec is None:
        supported = ", ".join(PROVIDER_SPECS)
        raise ValueError(f"不支持的 LLM provider: {provider_id},可用: {supported}")

    api_key = os.environ.get(spec.env_key) or ""
    if provider_id == "gpt" and not api_key:
        api_key = os.environ.get("OPENAI_API_KEY") or ""
    if not api_key:
        raise RuntimeError(f"缺少 {spec.env_key} 环境变量,请在 backend/.env 中配置。")

    base_url = os.environ.get(spec.env_base) or spec.default_base
    model = os.environ.get(spec.env_model) or spec.default_model
    return ResolvedProvider(
        id=provider_id,
        label=spec.label,
        api_key=api_key,
        base_url=base_url.rstrip("/"),
        model=model,
        mode=spec.mode,
    )


def _client_key(provider_id: str, timeout: float) -> str:
    return f"{provider_id}:{timeout}"


def _close_client(provider_id: str, timeout: float) -> None:
    key = _client_key(provider_id, timeout)
    client = _clients.pop(key, None)
    if client is not None:
        try:
            client.close()
        except Exception:
            pass


def get_client(provider: str | None = None, timeout: float | None = None) -> OpenAI:
    """懒加载指定 provider 的 OpenAI 客户端。"""
    resolved = resolve_provider(provider)
    timeout_value = timeout if timeout is not None else 300.0
    key = _client_key(resolved.id, timeout_value)
    if key not in _clients:
        _clients[key] = OpenAI(
            api_key=resolved.api_key,
            base_url=resolved.base_url,
            timeout=timeout_value,
        )
    return _clients[key]


def list_llm_providers() -> list[dict]:
    """给前端/API 展示可选 provider,不返回 API key。"""
    default_id = get_default_provider()
    out = []
    for spec in PROVIDER_SPECS.values():
        api_key = os.environ.get(spec.env_key) or ""
        if spec.id == "gpt" and not api_key:
            api_key = os.environ.get("OPENAI_API_KEY") or ""
        out.append({
            "id": spec.id,
            "label": spec.label,
            "model": os.environ.get(spec.env_model) or spec.default_model,
            "mode": spec.mode,
            "available": bool(api_key),
            "is_default": spec.id == default_id,
        })
    return out


def _chat_messages(system: str, user: str) -> list[dict]:
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _responses_input(system: str, user: str) -> list[dict]:
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def messages_stream(
    system: str,
    user: str,
    max_tokens: int = 4000,
    model: str | None = None,
    temperature: float | None = None,
    provider: str | None = None,
) -> Generator[str, None, None]:
    """流式调用 LLM,逐块返回增量文本。"""
    resolved = resolve_provider(provider)
    model = model or resolved.model
    client = get_client(provider=resolved.id)

    if resolved.mode == "responses":
        kwargs = dict(
            model=model,
            input=_responses_input(system, user),
            max_output_tokens=max_tokens,
            stream=True,
        )
        if temperature is not None:
            kwargs["temperature"] = temperature
        emitted_text = False
        for event in client.responses.create(**kwargs):
            event_type = getattr(event, "type", "") or ""
            delta = getattr(event, "delta", None)
            if event_type.startswith("response.output_text") and isinstance(delta, str):
                if delta:
                    emitted_text = True
                    yield delta
            elif event_type == "response.completed" and not emitted_text:
                response = getattr(event, "response", None)
                text = getattr(response, "output_text", "") or ""
                if text:
                    yield text
        return

    kwargs = dict(
        model=model,
        messages=_chat_messages(system, user),
        max_tokens=max_tokens,
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
    timeout: float | None = None,
    provider: str | None = None,
) -> str:
    """调 LLM Chat/Responses API,带 3 次指数退避重试。"""
    resolved = resolve_provider(provider)
    model = model or resolved.model
    last_err: Exception | None = None
    timeout_value = timeout if timeout is not None else 300.0

    for attempt in range(max_retries):
        try:
            client = get_client(provider=resolved.id, timeout=timeout_value)
            if resolved.mode == "responses":
                kwargs = dict(
                    model=model,
                    input=_responses_input(system, user),
                    max_output_tokens=max_tokens,
                )
                if temperature is not None:
                    kwargs["temperature"] = temperature
                if response_format and response_format.get("type") == "json_object":
                    kwargs["text"] = {"format": {"type": "json_object"}}
                resp = client.responses.create(**kwargs)
                content = getattr(resp, "output_text", "") or ""
            else:
                kwargs = dict(
                    model=model,
                    messages=_chat_messages(system, user),
                    max_tokens=max_tokens,
                )
                if temperature is not None:
                    kwargs["temperature"] = temperature
                if response_format is not None:
                    kwargs["response_format"] = response_format
                resp = client.chat.completions.create(**kwargs)
                content = resp.choices[0].message.content or ""
            return normalize_model_output(content)
        except APIConnectionError as e:
            last_err = e
            log.warning("LLM 连接错误(第 %s 次): %s", attempt + 1, e)
            _close_client(resolved.id, timeout_value)
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
        except APITimeoutError as e:
            last_err = e
            log.warning("LLM 超时(第 %s 次): %s", attempt + 1, e)
            if attempt < max_retries - 1:
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
