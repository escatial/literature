"""任务失败错误码归一 + 行动指引(P0-2/P0-5,2026-11-18)。

原则:
- 失败路径一律发 TaskErrorCode,不再透传自由文本给前端与轨迹表;
- 每个码配一条行动指引(ACTION_HINTS)——暂停消息必须告诉用户
  「你现在要做什么」,而不是只说「发生错误」;
- format_task_error 产出统一格式,既给人看也可程序解析::

      [错误码 cnki_cookie_expired] <指引文案> | 详情: <原始异常>

- 兼容性:format 输出始终包含原始 detail 全文,既有测试对关键
  子串(如「手动停止」「执行异常」)的断言不因归一而失配。
"""
from __future__ import annotations

from enum import Enum


class TaskErrorCode(str, Enum):
    TASK_CANCELLED = "task_cancelled"
    COOKIE_EXPIRED = "cnki_cookie_expired"
    CNKI_PAGINATION_LIMIT = "cnki_pagination_limit"
    CNKI_REVISION = "cnki_revision"
    CAPTCHA_BALANCE = "captcha_balance"
    SESSION_BLOCKED = "session_blocked"
    LLM_RATE_LIMIT = "llm_rate_limit"
    LLM_ALL_PROVIDERS_FAILED = "llm_all_providers_failed"
    RETRIEVAL_HIT_COUNT_LOW = "retrieval_hit_count_low"
    CONFIRM_EXPIRED = "confirm_expired"
    INTERNAL_ERROR = "internal_error"


# 行动指引表:键=错误码,值=给用户的「你现在要做什么」。
# 占位符 {threshold} 由 format_task_error 的 ctx 填充。
ACTION_HINTS: dict[TaskErrorCode, str] = {
    TaskErrorCode.TASK_CANCELLED:
        "任务已按你的操作停止;如需继续请重新发起。",
    TaskErrorCode.COOKIE_EXPIRED:
        "知网 Cookie 失效或未录入。请到 Cookie 监控页粘贴新的浏览器 Cookie"
        "(从知网检索页复制 cURL 的 -b/--cookie 值),保存后重新发起任务。",
    TaskErrorCode.CNKI_PAGINATION_LIMIT:
        "已达知网游客翻页上限。请录入登录 Cookie 后重新发起任务,"
        "或缩小检索主题以减少所需翻页数。",
    TaskErrorCode.CNKI_REVISION:
        "知网页面结构疑似改版,工具需要更新。请反馈此错误,等待版本修复。",
    TaskErrorCode.CAPTCHA_BALANCE:
        "验证码打码服务余额不足,请充值超级鹰账户后重试。",
    TaskErrorCode.SESSION_BLOCKED:
        "知网会话被风控拦截。请稍后重试,或更换网络环境/使用登录 Cookie。",
    TaskErrorCode.LLM_RATE_LIMIT:
        "LLM 服务限流或余额不足。请检查你配置的 LLM 账户余额与套餐限流,"
        "稍后在任务页重试。",
    TaskErrorCode.LLM_ALL_PROVIDERS_FAILED:
        "所有已配置 LLM 服务均调用失败。请到 LLM 配置页检查 API Key、"
        "Base URL 与模型名,确认后重试。",
    TaskErrorCode.RETRIEVAL_HIT_COUNT_LOW:
        "检索命中 {actual} 篇,低于最低门槛 {threshold} 篇,继续写必然主题簇塌方。"
        "建议:换更宽泛的主题关键词、放宽年份范围,或录入登录 Cookie 扩大检索,再重新发起。",
    TaskErrorCode.CONFIRM_EXPIRED:
        "主题确认窗口已超时过期。请重新发起主题划分并尽快确认。",
    TaskErrorCode.INTERNAL_ERROR:
        "发生未分类内部错误。请带上详情反馈,我们按错误码归档排查。",
}


# crawler 自定义异常的 code 属性 → 错误码(duck-typing,避免 import 重模块)
_EXC_CODE_ATTR_MAP: dict[str, TaskErrorCode] = {
    "cookie_expired": TaskErrorCode.COOKIE_EXPIRED,
    "cnki_pagination_limit": TaskErrorCode.CNKI_PAGINATION_LIMIT,
    "cnki_revision": TaskErrorCode.CNKI_REVISION,
    "captcha_balance": TaskErrorCode.CAPTCHA_BALANCE,
    "session_blocked": TaskErrorCode.SESSION_BLOCKED,
}

# 消息签名匹配(错误事件里只剩 str(message) 时用)
_MESSAGE_SIGNATURES: list[tuple[str, TaskErrorCode]] = [
    ("LLM 调用所有候选 provider 失败", TaskErrorCode.LLM_ALL_PROVIDERS_FAILED),
    ("rate limit", TaskErrorCode.LLM_RATE_LIMIT),
    ("insufficient balance", TaskErrorCode.LLM_RATE_LIMIT),
    ("限流", TaskErrorCode.LLM_RATE_LIMIT),
    ("余额不足", TaskErrorCode.LLM_RATE_LIMIT),
    ("请稍后重试", TaskErrorCode.CNKI_PAGINATION_LIMIT),
    ("游客翻页上限", TaskErrorCode.CNKI_PAGINATION_LIMIT),
    ("cookie", TaskErrorCode.COOKIE_EXPIRED),
    ("Cookie", TaskErrorCode.COOKIE_EXPIRED),
    ("验证码", TaskErrorCode.CAPTCHA_BALANCE),
    ("改版", TaskErrorCode.CNKI_REVISION),
    ("用户已停止", TaskErrorCode.TASK_CANCELLED),
    ("手动停止", TaskErrorCode.TASK_CANCELLED),
]


def classify_exception(exc: BaseException) -> TaskErrorCode:
    """按异常对象分类。优先级:code 属性 > 类型特征 > 消息签名 > INTERNAL。"""
    code_attr = getattr(exc, "code", None)
    if isinstance(code_attr, str) and code_attr in _EXC_CODE_ATTR_MAP:
        return _EXC_CODE_ATTR_MAP[code_attr]
    name = type(exc).__name__
    if name == "TaskCancelledError":
        return TaskErrorCode.TASK_CANCELLED
    if name in ("CnkiCookieError",):
        return TaskErrorCode.COOKIE_EXPIRED
    msg = str(exc)
    if "LLM 调用所有候选 provider 失败" in msg:
        return TaskErrorCode.LLM_ALL_PROVIDERS_FAILED
    lowered = msg.lower()
    if "rate limit" in lowered or "insufficient balance" in lowered or "429" in lowered:
        return TaskErrorCode.LLM_RATE_LIMIT
    return classify_message(msg)


def classify_message(message: str) -> TaskErrorCode:
    """按消息签名分类(错误事件/轨迹表里只剩字符串时用)。"""
    for signature, code in _MESSAGE_SIGNATURES:
        if signature in message:
            return code
    return TaskErrorCode.INTERNAL_ERROR


def format_task_error(
    code: TaskErrorCode,
    detail: str = "",
    **ctx: object,
) -> str:
    """产出统一格式错误串:`[错误码 x] 指引 | 详情: 原文`。

    detail 全文保留——错误码归一不吞原始信息,排查仍看详情段。
    """
    hint = ACTION_HINTS[code]
    if ctx:
        try:
            hint = hint.format(**ctx)
        except (KeyError, IndexError):
            pass  # 占位符与 ctx 不匹配时保留模板原文,不抛
    parts = [f"[错误码 {code.value}] {hint}"]
    if detail:
        parts.append(f"详情: {detail}")
    return " ".join(parts)
