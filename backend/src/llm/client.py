"""多 Provider LLM 客户端封装。

协议维度双通道:
- openai(OpenAI 兼容 Chat Completions,含 MiniMax 别名;支持 reasoning_split 思考分离);
- anthropic(Anthropic Messages,system 顶层 + x-api-key + thinking/text 双通道 SSE)。

默认 provider 由 LLM_PROVIDER 环境变量决定;用户级运行时覆盖
(set_runtime_llm,kind ∈ openai/anthropic)激活时优先生效。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from contextvars import ContextVar
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
}

_clients: dict[str, OpenAI] = {}
# 记录上一次 messages_create 成功调用的 provider id。主要用于
# 主题阶段的结构化决策日志(如 provider 列、以及该实际落在哪一级),
# 以及 harness 绕圈重试时决定是否切换 provider。这个 contextvar
# 起始为 None; 调用后由 _create_with_provider 在成功分支后设置。测试可以直接
# 调用 set_last_used_provider("minimax") 注入假数据。
last_used_provider: ContextVar[str | None] = ContextVar("last_used_provider", default=None)

def set_last_used_provider(pid: str | None) -> None:
    """Test/observability hook: 手动设置上一次成功调用的 provider。"""
    last_used_provider.set(pid)


# 默认锁定 minimax 单 provider。历史的多级降级(minimax -> deepseek -> gpt)
# 会在首选故障时把请求悄悄换到别的模型，主题/正文质量漂移且排因困难；
# 现在 minimax 故障直接向调用方抛出真实错误，由前端原样展示给用户。
# 如需临时启用其他 provider，在 .env 设置 LLM_FALLBACK_ORDER=minimax,deepseek。
# fallback 链仅在遇到鉴权/限流/网关/超时类瞬时错误时触发，
# 模型能力/JSON 截断类问题不会切换 provider。
DEFAULT_FALLBACK_ORDER: tuple[str, ...] = ("minimax",)
# 触发起切换的瞬时错误白名单:鉴权失败 / 限流 / 网关错误 / 请求超时等
# 不把"模型能力不够 / JSON 截断 / 内容审查拒绝"纳入,这类问题换 provider 也没用。
_FALLBACK_TRIGGERS = (
    APIConnectionError,
    APITimeoutError,
)


def _normalize_env_once() -> None:
    """进程启动时执行一次 .env 兜底。

    v9.6:仅在 LLM_PROVIDER 未设置时补默认 minimax;不再改写用户显式配置。
    此前会强制 LLM_PROVIDER=minimax 并清空 LLM_FALLBACK_ORDER,导致 .env 里
    配置的多 provider 降级永远失效(与 get_fallback_order 的 docstring 矛盾)。
    """
    provider = os.environ.get("LLM_PROVIDER", "").strip().lower()
    if not provider:
        # v9.6:仅在未设置时补默认值;此前无条件强制改写 LLM_PROVIDER 并清空
        # LLM_FALLBACK_ORDER,.env 里配置的多 provider 降级永远失效(死功能,
        # 与 get_fallback_order 的 docstring 直接矛盾)
        log.info("LLM_PROVIDER 未设置,使用默认 minimax")
        os.environ["LLM_PROVIDER"] = "minimax"


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

    - 用户运行时覆盖激活:绕过 env key 检查,直接走覆盖配置(单 provider)。
    - 显式传 provider: 仅用该 provider,不降级。
    - 传 None 或 "auto": 按 fallback 顺序筛掉未配置 key 的 provider。
    """
    if _runtime_llm_active():
        return (get_default_provider(),)
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


# ---- v9.11: 运行时模型选择(从上游获取 + 用户自选) ----
# C1(2026-10-27) contextvar 化:模型覆盖与用户级 provider 覆盖都绑定到
# 当前执行上下文 —— SSE worker 线程内 set、finally reset,天然按请求隔离,
# 多任务并发不再互相串台。ContextVar 在同一线程内随 async 任务复制语义
# 与线程本地语义一致(本工具的调用形态是同步生成器跑在 worker 线程)。

_model_override_ctx: ContextVar[str | None] = ContextVar("model_override", default=None)


def set_model_override(model_id: str | None):
    """运行时覆盖 MINIMAX_MODEL(优先级高于 env)。返回 Token 供 reset。"""
    return _model_override_ctx.set(
        model_id.strip() if model_id and model_id.strip() else None
    )


def reset_model_override(token) -> None:
    """恢复 set_model_override 之前的上下文值(须在同一线程调用)。"""
    _model_override_ctx.reset(token)


def get_model_override() -> str | None:
    return _model_override_ctx.get()


# ---- 按用户运行时覆盖(用户管理:自定义 base_url / api_key / model) ----
_runtime_llm_ctx: ContextVar[dict | None] = ContextVar("runtime_llm", default=None)


def set_runtime_llm(*, kind: str, base_url: str, api_key: str, model: str):
    """启用用户自定义 provider(当前上下文)。kind ∈ openai/anthropic(协议维度);

    历史 kind "minimax"(MiniMax 的 OpenAI 兼容端点)归一化为 openai。
    返回 Token,配套 reset_runtime_llm(token) 恢复。
    """
    normalized = (kind or "openai").strip().lower()
    if normalized == "minimax":
        normalized = "openai"
    return _runtime_llm_ctx.set({
        "kind": normalized,
        "base_url": (base_url or "").strip().rstrip("/"),
        "api_key": (api_key or "").strip(),
        "model": (model or "").strip(),
    })


def reset_runtime_llm(token) -> None:
    """恢复 set_runtime_llm 之前的上下文值(须在同一线程调用)。"""
    _runtime_llm_ctx.reset(token)


def clear_runtime_llm() -> None:
    """兼容入口:在当前上下文清除覆盖(回落系统 .env)。

    与 reset 的区别:clear 是无条件置 None(任意上下文可调,旧测试大量
    依赖);reset 是精确恢复 set 前状态(请求作用域 try/finally 用)。
    """
    _runtime_llm_ctx.set(None)


def get_runtime_llm() -> dict | None:
    rt = _runtime_llm_ctx.get()
    return dict(rt) if rt else None


def _runtime_llm_active() -> bool:
    rt = _runtime_llm_ctx.get()
    return bool(
        rt and rt.get("base_url") and rt.get("api_key")
    )


def _anthropic_url(base_url: str, path: str) -> str:
    """Anthropic 协议 base_url → 完整 URL。path ∈ messages/models。

    官方 SDK 约定 base 不带 /v1(如 https://api.anthropic.com、
    https://api.minimax.cn/anthropic);个别网关自带 /v1 —— 两者都兼容。
    """
    base = (base_url or "").strip().rstrip("/")
    if base.endswith("/v1"):
        return f"{base}/{path}"
    return f"{base}/v1/{path}"


def _anthropic_headers(api_key: str) -> dict:
    return {
        "x-api-key": (api_key or "").strip(),
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }


def _anthropic_text(data: dict) -> str:
    """取 Messages 响应里的全部 text 块。"""
    parts = []
    for block in (data or {}).get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
    return "".join(parts)


def _anthropic_thinking(data: dict) -> str:
    """取 Messages 响应里的全部 thinking 块(正文为空时的兜底来源)。"""
    parts = []
    for block in (data or {}).get("content") or []:
        if isinstance(block, dict) and block.get("type") == "thinking":
            parts.append(str(block.get("thinking") or ""))
    return "".join(parts)


def _anthropic_parse_sse_line(line: str) -> tuple[str, str] | None:
    """解析 Anthropic SSE 的一行 data → (kind, text);非内容增量返回 None。

    kind ∈ reasoning(thinking_delta)/content(text_delta);error 事件直接抛。
    """
    line = line.strip()
    if not line.startswith("data:"):
        return None
    payload = line[5:].strip()
    if not payload or payload == "[DONE]":
        return None
    try:
        evt = json.loads(payload)
    except ValueError:
        return None
    etype = evt.get("type")
    if etype == "content_block_delta":
        delta = evt.get("delta") or {}
        dtype = delta.get("type")
        if dtype == "text_delta":
            text = delta.get("text")
            return ("content", text) if text else None
        if dtype == "thinking_delta":
            text = delta.get("thinking")
            return ("reasoning", text) if text else None
    elif etype == "error":
        err = evt.get("error") or {}
        raise RuntimeError(f"Anthropic 接口流式错误: {err.get('message') or evt}")
    return None


def _assert_public_http_url(url: str) -> None:
    """出站 URL 的词法收窄断言(与 _guard_outbound_url 同规,双保险)。

    scheme 必须 http/https 且 host 存在;host 为本机/内网主机名或
    环回/私网/保留地址字面量时抛 RuntimeError。域名侧 DNS 校验由
    _guard_outbound_url(validate_external_url)负责,本断言保证请求行
    上游的公网语义静态可见。
    """
    import ipaddress
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise RuntimeError(f"LLM 接口地址非法(仅允许公网 http/https): {url!r}")
    host = parsed.hostname.lower()
    if host == "localhost" or host.endswith((".local", ".internal")):
        raise RuntimeError(f"LLM 接口地址被拒绝(仅允许公网 http/https): {host}")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return  # 域名字面量:DNS 侧校验由 _guard_outbound_url 负责
    if (ip.is_loopback or ip.is_private or ip.is_reserved
            or ip.is_link_local or ip.is_multicast or ip.is_unspecified):
        raise RuntimeError(f"LLM 接口地址被拒绝(仅允许公网 http/https): {host}")


def _guard_outbound_url(url: str) -> str:
    """发请求前的 SSRF 兜底校验(与 resolve_provider 的 B3 二次校验同源)。

    纵深防御:即使调用方绕过 resolve_provider 直接以用户可控 base_url
    构造请求,出站前也拦截内网/环回/保留地址。公网域名放行(含
    api.minimax.cn 网关);域名暂不可解析沿用既有 NXDOMAIN 放行语义,
    连接期报真实错误。校验失败按本模块错误契约抛 RuntimeError。
    """
    from security.auth import validate_external_url

    try:
        guarded = validate_external_url(url)
    except ValueError as exc:
        raise RuntimeError(f"LLM 接口地址被拒绝(仅允许公网 http/https): {exc}") from exc
    _assert_public_http_url(guarded)
    return guarded


def _anthropic_request(
    base_url: str, api_key: str, payload: dict, timeout: float,
) -> dict:
    """非流式 Anthropic Messages 调用(httpx 为 openai SDK 传递依赖,零新装包)。

    错误一律 RuntimeError 且带上游原文,直达前端。
    """
    import httpx
    url = _guard_outbound_url(_anthropic_url(base_url, "messages"))
    _assert_public_http_url(url)  # 请求行上游紧跟公网断言(静态可见)
    try:
        resp = httpx.post(_guard_outbound_url(url), headers=_anthropic_headers(api_key), json=payload, timeout=timeout)
    except httpx.HTTPError as exc:
        raise RuntimeError(f"Anthropic 接口连接失败: {exc}") from exc
    if resp.status_code >= 400:
        raise RuntimeError(f"Anthropic 接口错误 {resp.status_code}: {resp.text[:300]}")
    return resp.json()


def _anthropic_stream(
    base_url: str, api_key: str, payload: dict, timeout: float,
) -> Generator[tuple[str, str], None, None]:
    """流式 Anthropic Messages:yield (kind, text),kind ∈ reasoning/content。"""
    import httpx
    url = _guard_outbound_url(_anthropic_url(base_url, "messages"))
    _assert_public_http_url(url)  # 建流行上游紧跟公网断言(静态可见)
    try:
        with httpx.stream(
            "POST", _guard_outbound_url(url), headers=_anthropic_headers(api_key), json=payload, timeout=timeout,
        ) as resp:
            if resp.status_code >= 400:
                body = resp.read().decode("utf-8", "replace")
                raise RuntimeError(f"Anthropic 接口错误 {resp.status_code}: {body[:300]}")
            for line in resp.iter_lines():
                parsed = _anthropic_parse_sse_line(line)
                if parsed is not None:
                    yield parsed
    except RuntimeError:
        raise
    except httpx.HTTPError as exc:
        raise RuntimeError(f"Anthropic 接口连接失败: {exc}") from exc


def fetch_models_for(kind: str, base_url: str, api_key: str, timeout: float = 15.0) -> list[dict]:
    """从上游获取可用模型列表,按协议选端点与鉴权头。

    openai(含 minimax 别名): GET {base}/models + Bearer
    anthropic:               GET {base}/v1/models + x-api-key
    """
    kind = (kind or "openai").strip().lower()
    if kind == "anthropic":
        url = _guard_outbound_url(_anthropic_url(base_url, "models"))
        headers = _anthropic_headers(api_key)
    else:
        url = _guard_outbound_url(f"{(base_url or '').strip().rstrip('/')}/models")
        headers = {"Authorization": f"Bearer {(api_key or '').strip()}"}
    _assert_public_http_url(url)  # 请求行上游紧跟公网断言(静态可见)
    import urllib.request
    req = urllib.request.Request(_guard_outbound_url(url), headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    models = data.get("data") or []
    return [
        {
            "id": m["id"],
            "owned_by": m.get("owned_by") or m.get("display_name") or "",
            "created": m.get("created", 0),
        }
        for m in models if m.get("id")
    ]


def fetch_available_models(provider_id: str = "minimax") -> list[dict]:
    """从上游 API 的 GET /models 端点获取可用模型列表(系统 env 配置)。

    用户运行时覆盖激活时优先用覆盖的 base_url/api_key;否则读系统 env。
    """
    rt = get_runtime_llm()
    if rt and rt.get("base_url") and rt.get("api_key"):
        try:
            return fetch_models_for(rt.get("kind") or "openai", rt["base_url"], rt["api_key"])
        except Exception as exc:
            log.warning("获取上游模型列表失败: %s", exc)
            return []
    spec = PROVIDER_SPECS.get(provider_id)
    if spec is None:
        return []
    api_key = os.environ.get(spec.env_key) or ""
    base = (os.environ.get(spec.env_base) or "").strip()
    if not api_key or not base:
        return []
    try:
        return fetch_models_for("openai", base, api_key)
    except Exception as exc:
        log.warning("获取上游模型列表失败: %s", exc)
        return []


def resolve_provider(provider: str | None = None) -> ResolvedProvider:
    """读取 provider 配置,校验 API key,并返回统一配置。

    用户运行时覆盖(set_runtime_llm)激活时优先:base_url/api_key/model
    全部来自覆盖;model 留空时依次回落 全局模型覆盖 → env 模型 → 报错。
    """
    rt = get_runtime_llm()
    if rt and rt.get("base_url") and rt.get("api_key"):
        normalized_kind = rt.get("kind") or "openai"
        if normalized_kind == "minimax":
            normalized_kind = "openai"
        # B3(2026-10-27):请求侧 SSRF 二次校验。保存侧(api/auth.
        # save_llm_config)已校验,但运行时覆盖是模块级 set_runtime_llm,
        # 可被测试/脚本绕过保存直接注入;所有 chat/stream 调用必经本函数,
        # 在此兜底。残余风险:校验与真实建连间的 DNS rebinding 窗口仍在,
        # 彻底方案为 transport 层 pin 已校验 IP(遗留项)。
        from security.auth import validate_external_url

        try:
            validated_base = validate_external_url(rt["base_url"])
        except ValueError as exc:
            raise RuntimeError(f"LLM 接口地址被拒绝(仅允许公网 http/https): {exc}") from exc
        spec = PROVIDER_SPECS.get(normalized_kind)
        model = rt.get("model") or get_model_override() or ""
        if not model and spec is not None:
            model = (os.environ.get(spec.env_model) or "").strip()
        if not model:
            raise RuntimeError(
                "未指定模型:请在「监控与管理 → LLM 设置」中选择模型后保存。"
            )
        return ResolvedProvider(
            id=normalized_kind,
            label={"anthropic": "Anthropic 兼容", "openai": "OpenAI 兼容"}.get(
                normalized_kind, "自定义接口",
            ),
            api_key=rt["api_key"],
            base_url=validated_base,
            model=model,
            mode="anthropic" if normalized_kind == "anthropic" else "chat",
        )

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
    # 运行时覆盖优先于 env
    override = get_model_override()
    if override:
        model = override
    return ResolvedProvider(
        id=provider_id,
        label=spec.label,
        api_key=api_key,
        base_url=base_url.rstrip("/"),
        model=model,
        mode=spec.mode,
    )


def _client_key(provider_id: str, timeout: float, api_key: str = "", base_url: str = "") -> str:
    # 缓存键须带 base_url/key 指纹:用户运行时覆盖会切换同一 provider id 的
    # 指向,仅按 id+timeout 缓存会复用旧配置的客户端。
    fp = hashlib.sha1(f"{api_key}|{base_url}".encode("utf-8")).hexdigest()[:12]
    return f"{provider_id}:{timeout}:{fp}"


def _close_client(provider_id: str, timeout: float) -> None:
    prefix = f"{provider_id}:"
    for key in [k for k in list(_clients) if k.startswith(prefix)]:
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
    key = _client_key(resolved.id, timeout_value, resolved.api_key, resolved.base_url)
    if key not in _clients:
        # SDK 内部重试关闭:openai>=1 在 client 创建时默认开启 max_retries=2,
        # 与外层 messages_create(max_retries=1) 叠加导致单次调用在客户端侧
        # 多等 4-7 秒并输出 'Retrying request to /chat/completions'。重试完全
        # 交给外层 fallback 链路，不再让 SDK 内部静默重试。
        _clients[key] = OpenAI(
            api_key=resolved.api_key,
            base_url=resolved.base_url,
            timeout=timeout_value,
            max_retries=0,
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


def messages_stream_ex(
    system: str,
    user: str,
    max_tokens: int = 4000,
    model: str | None = None,
    temperature: float | None = None,
    provider: str | None = None,
) -> Generator[tuple[str, str], None, None]:
    """流式调用 LLM,逐块返回 (kind, text) 二元组。

    kind="reasoning" 为模型思考增量(reasoning_details / reasoning_content),
    仅供过程展示,调用方不得拼入正文;kind="content" 为正文增量。
    按 `select_providers` 给出的顺序尝试 provider: 第一个能产出正文增量的
    provider 胜出;首个 provider 在还没产生任何正文前就抛鉴权/超时/网关错误时
    自动换到下一级。一旦开始产出正文,后续就不再切 provider(流式内切会污染下游)。
    """
    candidates = select_providers(provider)

    fallback_used: list[str] = []
    last_err: Exception | None = None
    for pid in candidates:
        # 2026-10-27:必须先于 try 初始化。此前放在 try 内 resolve_provider
        # 之后,resolve 抛 RuntimeError(如覆盖态未选模型)时 except 分支访问
        # 未绑定的 yielded_any 抛 UnboundLocalError,掩盖原始错误信息。
        yielded_any = False
        try:
            resolved = resolve_provider(pid)
            for kind, piece in _stream_with_resolved(
                resolved, system=system, user=user,
                max_tokens=max_tokens, model=model, temperature=temperature,
            ):
                if kind == "content":
                    yielded_any = True
                yield kind, piece
            if pid != candidates[0] and fallback_used:
                log.info("流式调用从 %s 回退到 %s", pid, candidates[0])
            return
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            # v9.6:本 provider 已产出文本后流中断,切换 provider 会从头重流,
            # 消费方拼出「前半段+全文」重复正文——直接抛出按失败处理
            if yielded_any:
                raise
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


def messages_stream(
    system: str,
    user: str,
    max_tokens: int = 4000,
    model: str | None = None,
    temperature: float | None = None,
    provider: str | None = None,
) -> Generator[str, None, None]:
    """流式调用 LLM,逐块返回正文增量(向后兼容包装,思考增量不外发)。"""
    for kind, piece in messages_stream_ex(
        system=system, user=user, max_tokens=max_tokens,
        model=model, temperature=temperature, provider=provider,
    ):
        if kind == "content":
            yield piece


def _stream_with_resolved(
    resolved: ResolvedProvider,
    *,
    system: str,
    user: str,
    max_tokens: int,
    model: str | None,
    temperature: float | None,
) -> Generator[tuple[str, str], None, None]:
    """单个 provider 的流式调用,yield (kind, text): kind ∈ reasoning/content。"""
    model = model or resolved.model
    emitted_any = False
    try:
        if resolved.mode == "anthropic":
            # Anthropic Messages 协议(system 顶层、x-api-key 鉴权、
            # thinking_delta/text_delta 双通道),与 chat 分支同语义
            payload = {
                "model": model,
                "max_tokens": max_tokens,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            }
            if temperature is not None:
                payload["temperature"] = temperature
            got_any = False
            for kind, text in _anthropic_stream(
                resolved.base_url, resolved.api_key, payload, timeout=300.0,
            ):
                got_any = True
                if kind == "content":
                    emitted_any = True
                yield kind, text
            if not got_any:
                raise RuntimeError("Anthropic 接口未返回任何内容")
            _record_provider_status(resolved.id, success=emitted_any, error=None)
            return

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
            for event in client.responses.create(**kwargs):
                event_type = getattr(event, "type", "") or ""
                delta = getattr(event, "delta", None)
                if event_type.startswith("response.output_text") and isinstance(delta, str):
                    if delta:
                        emitted_any = True
                        yield "content", delta
                elif event_type == "response.completed" and not emitted_any:
                    response = getattr(event, "response", None)
                    text = getattr(response, "output_text", "") or ""
                    if text:
                        emitted_any = True
                        yield "content", text
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

        # 思考增量双来源:reasoning_split 的 reasoning_details 与 deepseek 风格的
        # delta.reasoning_content;实时对外 yield 供过程展示,同时留 buffer 兜底
        reasoning_buffer = ""
        for chunk in client.chat.completions.create(**kwargs):
            if not getattr(chunk, "choices", None):
                continue
            delta = chunk.choices[0].delta
            details = getattr(delta, "reasoning_details", None)
            if details:
                think_text = "".join(t for t in (_item_text(i) for i in details) if t)
                if think_text:
                    reasoning_buffer += think_text
                    yield "reasoning", think_text
            rc = getattr(delta, "reasoning_content", None)
            if isinstance(rc, str) and rc:
                reasoning_buffer += rc
                yield "reasoning", rc
            piece = getattr(delta, "content", None)
            if not piece:
                continue
            if isinstance(piece, str):
                emitted_any = True
                yield "content", piece
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
                    yield "content", "".join(text_parts)
        if not emitted_any and reasoning_buffer.strip():
            # 兜底:等价于 _strip_think 的「剥完为空 → 正文必在 think 内」
            yield "content", re.sub(r"</?think>", "", reasoning_buffer).strip()
        # v9.6:success 此前为 emitted_any or True 恒真,provider 健康面板永远显示
        # 成功——改为「有正文输出或 reasoning 兜底产出」才算成功
        _record_provider_status(
            resolved.id, success=emitted_any or bool(reasoning_buffer.strip()), error=None
        )
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

    # max_retries=0 表示"只调一次、不重试"(格式修复等短调用依赖该语义),
    # range(0) 会让循环体一次都不跑、last_err 停留在 None,最终 raise None
    # 变成 TypeError: exceptions must derive from BaseException。
    for attempt in range(max(1, max_retries)):
        try:
            if resolved.mode == "anthropic":
                # Anthropic Messages 协议:错误为 RuntimeError(非 openai 异常族),
                # 不进下方 openai 异常重试分支——覆盖态单 provider,直接抛给前端
                payload = {
                    "model": model,
                    "max_tokens": max_tokens,
                    "system": system,
                    "messages": [{"role": "user", "content": user}],
                }
                if temperature is not None:
                    payload["temperature"] = temperature
                data = _anthropic_request(
                    resolved.base_url, resolved.api_key, payload, timeout=timeout,
                )
                content = _anthropic_text(data)
                if not content.strip():
                    # M3 类怪癖的 Anthropic 版:正文整体落在 thinking 块
                    content = _anthropic_thinking(data).strip()
                _record_provider_status(resolved.id, success=True, error=None)
                last_used_provider.set(resolved.id)
                return content
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
            last_used_provider.set(resolved.id)
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
    if last_err is None:
        last_err = RuntimeError(f"provider {resolved.id} 未执行任何请求即退出(max_retries={max_retries})")
    raise last_err


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
