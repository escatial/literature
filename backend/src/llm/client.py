"""多 Provider LLM 客户端封装。

支持 MiniMax / DeepSeek / GPT(OpenAI Compatible)。
默认 provider 由 LLM_PROVIDER 环境变量决定,调用方也可显式传入 provider。
"""
from __future__ import annotations

import json
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
    mode: str  # "chat" 或 "responses"


@dataclass(frozen=True)
class ResolvedProvider:
    id: str
    label: str
    api_key: str
    base_url: str
    model: str
    mode: str


# Provider 注册表:只声明「读哪些 env 变量」,不含任何 base_url / model 默认值。

PROVIDER_SPECS: dict[str, ProviderSpec] = {
    "minimax": ProviderSpec(
        id="minimax",
        label="MiniMax",
        env_key="MINIMAX_API_KEY",
        env_base="MINIMAX_BASE_URL",
        env_model="MINIMAX_MODEL",
        mode="chat",
    ),
    "deepseek": ProviderSpec(
        id="deepseek",
        label="DeepSeek",
        env_key="DEEPSEEK_API_KEY",
        env_base="DEEPSEEK_BASE_URL",
        env_model="DEEPSEEK_MODEL",
        mode="chat",
    ),
    "gpt": ProviderSpec(
        id="gpt",
        label="GPT",
        env_key="GPT_API_KEY",
        env_base="GPT_BASE_URL",
        env_model="GPT_MODEL",
        mode="responses",
    ),
}

_clients: dict[str, OpenAI] = {}

# v7.1 默认锁定只用 minimax。
# - deepseek / gpt 的配置(MINIMAX_BASE_URL/GPT_BASE_URL/DEEPSEEK_BASE_URL 等)保留,
#   不再被自动 fallback 使用;若用户希望切回,可在 .env 设置
#   LLM_FALLBACK_ORDER=minimax,deepseek(逗号分隔)显式启用。
DEFAULT_FALLBACK_ORDER: tuple[str, ...] = ("minimax",)
# 触发起切换的瞬时错误白名单:鉴权失败 / 限流 / 网关错误 / 请求超时等
# 不把"模型能力不够 / JSON 截断 / 内容审查拒绝"纳入,这类问题换 provider 也没用。
_FALLBACK_TRIGGERS = (
    APIConnectionError,
    APITimeoutError,
)


def _normalize_env_once() -> None:
    """进程启动时执行一次 .env 兜底。

    背景:uvicorn --reload 时,reload 子进程继承主进程的 os.environ,不会再重新
    load_dotenv。如果 .env 写了 LLM_FALLBACK_ORDER=deepseek,gpt,minimax 之类
    的"非 minimax 优先"顺序,reload 后旧 env 还在 → 前端 tooltip 看到的轮换
    顺序仍然是旧的。

    兜底策略:
      1. 强制 LLM_PROVIDER=minimax(忽略 env 残留,锁定默认)
      2. 强制清空 LLM_FALLBACK_ORDER(走 DEFAULT_FALLBACK_ORDER = ("minimax",))
      3. 若用户主动要启用降级,在 main.py 之外显式设 env 即可;
         这里只是兜底,正常用户不应被这一行影响。
    """
    provider = os.environ.get("LLM_PROVIDER", "").strip().lower()
    fb = os.environ.get("LLM_FALLBACK_ORDER", "").strip()
    if provider != "minimax":
        log.warning(
            "LLM_PROVIDER=%r 被强制改为 minimax(避免其他 provider 抢占默认)",
            provider,
        )
        os.environ["LLM_PROVIDER"] = "minimax"
    if fb:
        log.warning(
            "LLM_FALLBACK_ORDER=%r 被强制清空(默认仅 minimax;需降级时改成 minimax,deepseek)",
            fb,
        )
        os.environ["LLM_FALLBACK_ORDER"] = ""


_normalize_env_once()


def get_default_provider() -> str:
    return os.environ.get("LLM_PROVIDER", "minimax").strip().lower() or "minimax"


def get_fallback_order() -> tuple[str, ...]:
    """读取降级顺序,默认仅 minimax(锁定单 provider)。

    若 .env 中显式设置了 `LLM_FALLBACK_ORDER`(逗号分隔),仍可启用 deepseek / gpt;
    例如 `LLM_FALLBACK_ORDER=minimax,deepseek` 允许 minimax 失败后降级到 deepseek。
    """
    raw = os.environ.get("LLM_FALLBACK_ORDER", "").strip()
    if not raw:
        return DEFAULT_FALLBACK_ORDER
    ordered = [token.strip().lower() for token in raw.split(",") if token.strip()]
    invalid = [token for token in ordered if token not in PROVIDER_SPECS]
    if invalid:
        log.warning("LLM_FALLBACK_ORDER 含未知 provider: %s,使用默认顺序", invalid)
        return DEFAULT_FALLBACK_ORDER
    return tuple(ordered)


def _is_retryable_error(exc: BaseException) -> bool:
    """判断异常是否触发 provider 切换。

    OpenAI 兼容网关的鉴权/限流通常以 APIError(携带 status_code)或
    APIConnectionError / APITimeoutError 抛出;401/403/408/429/5xx 一律算瞬时。
    """
    if isinstance(exc, _FALLBACK_TRIGGERS):
        return True
    if isinstance(exc, APIError):
        status = getattr(exc, "status_code", None)
        if status in (401, 403, 408, 429):
            return True
        # 5xx 也算瞬时
        if isinstance(status, int) and 500 <= status < 600:
            return True
        # 部分兼容网关把鉴权错误塞进 message 而不是 status_code
        message = str(exc).lower()
        if any(token in message for token in (
            "authentication fails",
            "invalid api key",
            "invalid_request_error",
            "insufficient balance",
            "rate limit",
        )):
            return True
    return False


def _provider_has_key(provider_id: str) -> bool:
    spec = PROVIDER_SPECS.get(provider_id)
    if spec is None:
        return False
    api_key = os.environ.get(spec.env_key) or ""
    if provider_id == "gpt" and not api_key:
        api_key = os.environ.get("OPENAI_API_KEY") or ""
    return bool(api_key)


def select_providers(requested: str | None) -> tuple[str, ...]:
    """根据请求的 provider 决定实际尝试顺序。

    - 显式传 provider: 仅用该 provider,不降级。
    - 传 None 或 "auto": 按 fallback 顺序筛掉未配置 key 的 provider。
    """
    if requested and requested.strip().lower() != "auto":
        normalized = requested.strip().lower()
        if normalized not in PROVIDER_SPECS:
            raise ValueError(f"不支持的 LLM provider: {normalized},可用: {', '.join(PROVIDER_SPECS)}")
        return (normalized,)
    order = [pid for pid in get_fallback_order() if _provider_has_key(pid)]
    if not order:
        # 兜底:把所有 PROVIDER_SPECS 里配了 key 的都试一遍
        order = [pid for pid in PROVIDER_SPECS if _provider_has_key(pid)]
    if not order:
        raise RuntimeError(
            "所有 LLM provider 都缺少 API key,请在 backend/.env 中至少配置一组"
        )
    return tuple(order)


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

    # base_url / model 无代码兜底:配置唯一来源是 backend/.env,
    # 缺失时显式报错,避免静默回落到与实际环境不符的值。
    base_url = (os.environ.get(spec.env_base) or "").strip()
    if not base_url:
        raise RuntimeError(
            f"缺少 {spec.env_base} 环境变量,请在 backend/.env 中配置。"
        )
    model = (os.environ.get(spec.env_model) or "").strip()
    if not model:
        raise RuntimeError(
            f"缺少 {spec.env_model} 环境变量,请在 backend/.env 中配置。"
        )
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
    """给前端/API 展示可选 provider,不返回 API key。

    `fallback_order` 字段告诉前端当前后端的轮换顺序(便于运维展示)。

    v7.1:新增 `is_active_fallback` 字段 — 只有出现在当前 fallback_order 里的
    provider 才会被默认流程调用;其他 provider(配了 key 但 env 没启用)只展示
    「配置保留」,不参与自动降级。
    """
    default_id = get_default_provider()
    fallback_order = get_fallback_order()
    out = []
    for spec in PROVIDER_SPECS.values():
        api_key = os.environ.get(spec.env_key) or ""
        if spec.id == "gpt" and not api_key:
            api_key = os.environ.get("OPENAI_API_KEY") or ""
        in_fallback = spec.id in fallback_order
        out.append({
            "id": spec.id,
            "label": spec.label,
            "model": os.environ.get(spec.env_model) or "",
            "mode": spec.mode,
            "available": bool(api_key),
            "is_default": spec.id == default_id,
            # v7.1:是否在当前轮换顺序里(默认流程会用到)
            "is_active_fallback": in_fallback,
            "fallback_rank": fallback_order.index(spec.id)
                if in_fallback else len(fallback_order),
        })
    out.sort(key=lambda row: row["fallback_rank"])
    return out


# === 运行时健康态缓存(供前端 dashboard 看 "现在用的是哪一级") ===
# 只在内存里,生命周期 = 进程;不持久化(进程重启即归零)。
_LLM_HEALTH: dict[str, dict] = {}
_HEALTH_TTL_SECONDS = 300  # 最近一次状态 5 分钟内都算"新鲜"


def _record_provider_status(provider_id: str, *, success: bool, error: str | None) -> None:
    """由 _create_with_provider / _stream_with_resolved 在调用结束时回调写入。"""
    import time as _t
    _LLM_HEALTH[provider_id] = {
        "last_success": _t.time() if success else _LLM_HEALTH.get(provider_id, {}).get("last_success", 0.0),
        "last_failure": 0.0 if success else _t.time(),
        "last_error": None if success else (error or "unknown"),
        "consecutive_failures": 0 if success else _LLM_HEALTH.get(provider_id, {}).get("consecutive_failures", 0) + 1,
    }


def get_provider_health() -> dict:
    """返回每个 provider 的最近一次调用状态 + 真正生效的回退顺序(已过滤未配 key 的)。"""
    import time as _t
    out: dict = {
        "active_fallback_order": list(select_providers(None)),
        "default": get_default_provider(),
        "providers": {},
    }
    for pid in PROVIDER_SPECS:
        h = _LLM_HEALTH.get(pid)
        if h is None:
            out["providers"][pid] = {
                "called": False,
                "healthy": None,
                "last_success_age_s": None,
                "last_error": None,
            }
            continue
        age_success = (None if not h.get("last_success") else
                       round(_t.time() - h["last_success"], 1))
        out["providers"][pid] = {
            "called": True,
            "healthy": h.get("last_success", 0.0) >= h.get("last_failure", 0.0),
            "last_success_age_s": age_success,
            "consecutive_failures": h.get("consecutive_failures", 0),
            "last_error": h.get("last_error"),
        }
    return out


def _chat_messages(system: str, user: str) -> list[dict]:
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _item_text(item: object) -> str | None:
    """取 reasoning_details 单项的 text 字段(兼容 dict 和对象两种形态)。"""
    if isinstance(item, dict):
        text = item.get("text")
    else:
        text = getattr(item, "text", None)
    return text if isinstance(text, str) else None


def _extract_reasoning_text(message: object) -> str:
    """提取 reasoning_split 模式下的思考文本(reasoning_details 优先,reasoning_content 兜底)。"""
    details = getattr(message, "reasoning_details", None)
    if details:
        parts = [t for t in (_item_text(i) for i in details) if t]
        if parts:
            return "".join(parts)
    rc = getattr(message, "reasoning_content", None)
    return rc if isinstance(rc, str) else ""


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
    """流式调用 LLM,逐块返回增量文本。

    按 `select_providers` 给出的顺序尝试 provider: 第一个能产出首段文本的
    provider 胜出;首个 provider 在还没产生任何文本前就抛鉴权/超时/网关错误时
    自动换到下一级。一旦开始产出文本,后续就不再切 provider(流式内切会污染下游)。
    """
    candidates = select_providers(provider)

    fallback_used: list[str] = []
    last_err: Exception | None = None
    for pid in candidates:
        try:
            resolved = resolve_provider(pid)
            yielded_any = False
            for piece in _stream_with_resolved(
                resolved, system=system, user=user,
                max_tokens=max_tokens, model=model, temperature=temperature,
            ):
                yielded_any = True
                yield piece
            if pid != candidates[0] and fallback_used:
                log.info("流式调用从 %s 回退到 %s", candidates[0], pid)
            return
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            if _is_retryable_error(exc):
                log.warning("LLM provider %s 流式失败,准备切换下一级: %s", pid, exc)
                fallback_used.append(pid)
                _close_client(pid, 300.0)
                continue
            # 非瞬时错误(模型能力/JSON 截断等)直接抛出,不做无效切换
            raise

    log.error("LLM 流式调用全部 provider 失败: %s", last_err)
    raise RuntimeError(
        f"LLM 流式调用失败({', '.join(candidates)}): {last_err}"
    )


def _stream_with_resolved(
    resolved: ResolvedProvider,
    *,
    system: str,
    user: str,
    max_tokens: int,
    model: str | None,
    temperature: float | None,
) -> Generator[str, None, None]:
    """单个 provider 的流式调用。"""
    model = model or resolved.model
    client = get_client(provider=resolved.id)
    emitted_any = False
    try:
        if resolved.mode == "responses":
            kwargs = dict(
                model=model,
                input=_responses_input(system, user),
                max_output_tokens=max_tokens,
                stream=True,
            )
            if temperature is not None:
                kwargs["temperature"] = temperature
            for event in client.responses.create(**kwargs):
                event_type = getattr(event, "type", "") or ""
                delta = getattr(event, "delta", None)
                if event_type.startswith("response.output_text") and isinstance(delta, str):
                    if delta:
                        emitted_any = True
                        yield delta
                elif event_type == "response.completed" and not emitted_any:
                    response = getattr(event, "response", None)
                    text = getattr(response, "output_text", "") or ""
                    if text:
                        emitted_any = True
                        yield text
            _record_provider_status(resolved.id, success=emitted_any, error=None)
            return

        kwargs = dict(
            model=model,
            messages=_chat_messages(system, user),
            max_tokens=max_tokens,
            stream=True,
        )
        if temperature is not None:
            kwargs["temperature"] = temperature
        if resolved.id == "minimax":
            # 官方 reasoning_split:思考内容分离到 reasoning_details,content 只留正文
            kwargs["extra_body"] = {"reasoning_split": True}

        # reasoning_split 下思考增量落在 delta.reasoning_details;缓冲不输出,
        # 仅在 content 全空(模型把正文整体写进 think 块的已知怪癖)时兜底
        reasoning_buffer = ""
        for chunk in client.chat.completions.create(**kwargs):
            if not getattr(chunk, "choices", None):
                continue
            delta = chunk.choices[0].delta
            details = getattr(delta, "reasoning_details", None)
            if details:
                reasoning_buffer += "".join(
                    t for t in (_item_text(i) for i in details) if t
                )
            piece = getattr(delta, "content", None)
            if not piece:
                continue
            if isinstance(piece, str):
                emitted_any = True
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
                    emitted_any = True
                    yield "".join(text_parts)
        if not emitted_any and reasoning_buffer.strip():
            # 兜底:等价于 _strip_think 的「剥完为空 → 正文必在 think 内」
            yield re.sub(r"</?think>", "", reasoning_buffer).strip()
        _record_provider_status(resolved.id, success=emitted_any or True, error=None)
    except Exception as exc:
        # 流式尚未产出 token 即失败,把错误写入健康态并向上抛
        if not emitted_any:
            _record_provider_status(resolved.id, success=False, error=str(exc))
        raise


def _create_with_provider(
    *,
    resolved: ResolvedProvider,
    system: str,
    user: str,
    max_tokens: int,
    model: str | None,
    temperature: float | None,
    response_format: dict | None,
    timeout: float,
    max_retries: int,
) -> str:
    """在单一 provider 上做指数退避重试,鉴权类错误直接抛给外层切换。"""
    model = model or resolved.model
    last_err: Exception | None = None

    # DeepSeek / OpenAI Compatible 平台在使用 response_format=json_object 时
    # 强制 system/user prompt 包含字面 "json",这里只在显式传入的分支补上。
    if response_format is not None:
        rf_kind = response_format.get("type") if isinstance(response_format, dict) else None
        if rf_kind in ("json_object", "json_schema"):
            system_aug = "\n\n(请以合法 JSON 格式输出,且仅输出 JSON,不要任何前后解释文字。)"
            if isinstance(system, str) and "json" not in system.lower():
                system = system + system_aug
            if isinstance(user, str) and "json" not in user.lower():
                user = user + "\n\n请用合法 JSON 格式返回结果。"

    for attempt in range(max_retries):
        try:
            client = get_client(provider=resolved.id, timeout=timeout)
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
                if resolved.id == "minimax":
                    # 官方 reasoning_split:思考内容分离到 reasoning_details,content 只留正文
                    kwargs["extra_body"] = {"reasoning_split": True}
                resp = client.chat.completions.create(**kwargs)
                message = resp.choices[0].message
                content = getattr(message, "content", None) or ""
                if not content.strip():
                    # M3 偶发把正文主体写进 think 块;reasoning_split 下它落在
                    # reasoning_details,此时用它兜底(等价 _strip_think 的回退分支)
                    reasoning = _extract_reasoning_text(message)
                    if reasoning.strip():
                        content = re.sub(r"</?think>", "", reasoning).strip()
            _record_provider_status(resolved.id, success=True, error=None)
            return content
        except APIConnectionError as e:
            last_err = e
            log.warning("LLM 连接错误(provider=%s 第 %s 次): %s", resolved.id, attempt + 1, e)
            _close_client(resolved.id, timeout)
            if _is_retryable_error(e) and attempt < max_retries - 1:
                time.sleep(2 ** attempt)
                continue
            if _is_retryable_error(e):
                # 已经到最大重试,让外层决定是否切换 provider
                _record_provider_status(resolved.id, success=False, error=str(e))
                raise
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
        except APITimeoutError as e:
            last_err = e
            log.warning("LLM 超时(provider=%s 第 %s 次): %s", resolved.id, attempt + 1, e)
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
                continue
            _record_provider_status(resolved.id, success=False, error=str(e))
            raise
        except APIError as e:
            last_err = e
            log.warning("LLM API 错误(provider=%s 第 %s 次): %s", resolved.id, attempt + 1, e)
            if _is_retryable_error(e):
                # 鉴权 / 限流 / 5xx — 让上层立即切换 provider
                _record_provider_status(resolved.id, success=False, error=str(e))
                raise
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)

    # 重试次数耗尽(非异常路径走到这里):把所有 catch 都没截到的"罕见无异常"
    # 也照样视为失败回写一条健康态
    if last_err is not None:
        _record_provider_status(resolved.id, success=False, error=str(last_err))
    log.error("provider %s 重试 %s 次仍失败: %s", resolved.id, max_retries, last_err)
    raise last_err  # type: ignore[misc]


def _create_fallback(
    system: str, user: str, candidates: tuple[str, ...],
    **create_kwargs,
) -> str:
    """在候选 provider 上依次 try,直到其中一个成功。

    candidates 长度 = 1 表示用户显式指定 provider,此时不跨 provider 切换,
    任何错误都直接抛给调用方。
    """
    last_err: Exception | None = None
    explicit = len(candidates) == 1
    for index, pid in enumerate(candidates):
        try:
            resolved = resolve_provider(pid)
        except RuntimeError as exc:
            last_err = exc
            log.warning("provider %s 无法解析: %s,继续下一级", pid, exc)
            continue
        try:
            content = _create_with_provider(
                resolved=resolved, system=system, user=user, **create_kwargs,
            )
            if index > 0:
                log.info("LLM 调用从首选降级到 %s 完成", pid)
            return content
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            if explicit:
                # 显式 provider 不允许跨 provider 回退
                raise
            if _is_retryable_error(exc):
                log.warning("provider %s 不可用,准备切换: %s", pid, exc)
                continue
            raise
    raise RuntimeError(
        f"LLM 调用所有候选 provider 失败({', '.join(candidates)}): {last_err}"
    )


def messages_create(
    system: str,
    user: str,
    max_tokens: int = 4000,
    model: str | None = None,
    max_retries: int = 1,
    temperature: float | None = None,
    response_format: dict | None = None,
    timeout: float | None = None,
    provider: str | None = None,
) -> str:
    """按 fallback 顺序调用 LLM,任何一级 provider 失败自动降级到下一级。

    默认 max_retries=1(单 provider 不再内部重试):
    - 之前默认 3 次 → 一次不通等 3 次 × 指数退避 ≈ 7s+ 单 provider × 3 provider ≈ 21s+
      + 用户反复触发 → 整个流程体感几十分钟,看不到尽头;
    - 现在改 1 次 → 遇到连接/超时立刻换下一 provider,3 个 provider × 25s timeout
      最坏 ~75s 即可出结果或抛错,前端 scheduleAutoRetry 接管后续重试节奏。
    单 provider 抖动容忍交给外层 fallback 顺序,不再内部重试。

    跨 provider 仅在遇到鉴权/限流/超时/网关错误时切换,模型能力错误
    不切换(节省时间)。
    """
    candidates = select_providers(provider)
    timeout_value = timeout if timeout is not None else 300.0
    return normalize_model_output(_create_fallback(
        system=system, user=user, candidates=candidates,
        max_tokens=max_tokens,
        model=model,
        temperature=temperature,
        response_format=response_format,
        timeout=timeout_value,
        max_retries=max_retries,
    ))


def messages_create_with_tools(
    messages: list[dict],
    tools: list[dict],
    max_tokens: int = 4000,
    temperature: float | None = None,
    provider: str | None = None,
    timeout: float | None = None,
) -> object:
    """带工具(function calling)的一次性调用,供 agent 编排循环使用。

    与 messages_create 的区别:
    - 入参是完整 messages 数组(含 assistant/tool 历史),不是 system+user 两段;
    - 传 tools,返回原始 assistant message(可能带 tool_calls),不做文本清洗
      (清洗会破坏 tool_calls 结构)。
    MiniMax 走 OpenAI 兼容 chat completions,原生支持 tools 参数。
    """
    candidates = select_providers(provider)
    last_err: Exception | None = None
    for pid in candidates:
        try:
            resolved = resolve_provider(pid)
            client = get_client(provider=resolved.id, timeout=timeout or 300.0)
            kwargs: dict = dict(
                model=resolved.model,
                messages=messages,
                tools=tools,
                max_tokens=max_tokens,
            )
            if temperature is not None:
                kwargs["temperature"] = temperature
            if resolved.id == "minimax":
                kwargs["extra_body"] = {"reasoning_split": True}
            resp = client.chat.completions.create(**kwargs)
            _record_provider_status(resolved.id, success=True, error=None)
            return resp.choices[0].message
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            if len(candidates) > 1 and _is_retryable_error(exc):
                log.warning("provider %s tools 调用失败,切换下一级: %s", pid, exc)
                continue
            raise
    raise RuntimeError(f"LLM tools 调用失败({', '.join(candidates)}): {last_err}")


def normalize_model_output(text: str) -> str:
    """统一清洗模型输出,供一次性/流式两种调用复用。"""
    cleaned = _strip_code_fence(_strip_think(text))
    cleaned = _unwrap_json_envelope(cleaned)
    return _strip_meta_reasoning_prefix(cleaned)


def _unwrap_json_envelope(text: str) -> str:
    """拆开模型在 json_object 模式下的多余 JSON 包裹。

    DeepSeek 等 OpenAI Compatible 平台在使用 ``response_format={"type":
    "json_object"}`` 时,即便提示词只要纯正文,模型有时也会把整段文字塞进
    ``{"content": ...}`` / ``{"text": ...}`` 等键里。这里只对**整段**是合法
    JSON object 且单一文本字段占主体(>120 字且占全文 ≥80%)的场景做拆封,
    避免误伤正常 JSON 输出。
    """
    if not text:
        return text
    stripped = text.strip()
    if not stripped.startswith("{") or not stripped.endswith("}"):
        return text
    try:
        parsed = json.loads(stripped)
    except Exception:
        return text
    if not isinstance(parsed, dict) or len(parsed) != 1:
        return text
    only_value = next(iter(parsed.values()))
    if not isinstance(only_value, str):
        return text
    if len(only_value) < 120 or len(only_value) < 0.8 * len(stripped):
        return text
    return only_value


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
