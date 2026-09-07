"""LLM 把用户主题拆成三个数据库各自可执行的动态检索式列表。

设计原则:
- LLM 是唯一的检索式生成者,不接受领域词表兜底;
- LLM 直接输出完整检索式字符串;
- 每个数据库生成 4～8 条有梯度的检索式,后端逐条遍历并跨条去重;
- 失败重试 3 次仍失败直接抛错,绝不静默兜底。
"""
from __future__ import annotations

import datetime
import json
import logging
import re

from openai import APIConnectionError, APITimeoutError

from llm.client import messages_create

log = logging.getLogger(__name__)

MIN_QUERY_COUNT = 4
MAX_QUERY_COUNT = 8


def normalize_cnki_query(query: str) -> str:
    """将知网专业检索式统一为可执行形式。

    知网专业检索 Expert 语法(SearchType=4)实测语义(2026-08):
      - '空格' 在同引号对内,会被知网当作字面字符"主题词A 主题词B" → 通常 0 结果
      - '+' 在同引号对内 = OR 关键字
      - '*' 在组间 = AND 关键字

    LLM 经常把组内同义词写成 `'主题词A' '主题词B'`(空格分隔,期望 OR),
    实际这会被知网当字面长串 → 0 结果。这里把同引号对内的 `'X' 'Y'`
    统一改写为 `'X'+'Y'`,外层 `*` 保留。

    同时折叠 `SU=x * SU=y` 为 `SU=x*y`,避免 'SU=' 子串出现两次。

    v8.4 两处加固(2026-08-29 检索式解析事故):
      - 全角标点归一化:LLM 偶发输出全角引号/加号/星号,知网 Expert 解析失败后
        不报错而是静默降级为「最新收录」默认列表(165 篇全 2026 大杂烩实证)。
      - 硬性要求 ≥2 组交叉:单组式(无 *)等价于全库 OR 大杂烩,直接判非法。
    """
    raw = " ".join(query.strip().split())
    if not raw:
        raise ValueError("知网检索式不能为空")

    # 全角标点归一化为知网 Expert 认的半角(在一切校验之前)
    for full, half in (("“", "'"), ("”", "'"), ("‘", "'"), ("’", "'"),
                       ("＋", "+"), ("＊", "*"), ("（", "("), ("）", ")"),
                       ("　", " ")):
        raw = raw.replace(full, half)
    raw = " ".join(raw.split())

    upper = raw.upper()
    if not upper.startswith("SU="):
        raise ValueError(f"知网检索式必须以 SU= 开头: {query}")

    body = raw[3:].strip()

    # 折叠多字段前缀拼接(SU=x * SU=y)为单 SU= 内部
    body = re.sub(r"\s*\*\s*SU\s*=\s*", " * ", body, flags=re.IGNORECASE)
    if re.search(r"\bSU\s*=", body, flags=re.IGNORECASE):
        raise ValueError(f"知网检索式包含重复字段前缀 SU=: {query}")

    # 关键修复:同一对单引号内部的空格 → + (知网 Expert 模式空格=字面,+=OR)
    # 用简单的状态机扫描,只在引号内部改。
    out_chars: list[str] = []
    in_string = False
    prev_was_quote = False
    for ch in body:
        if ch == "'":
            # 切换字符串;同一引号对前导的空格应该已被 ' '.join 化掉,这里不再处理
            in_string = not in_string
            out_chars.append(ch)
            prev_was_quote = True
            continue
        if in_string:
            out_chars.append(ch)
        else:
            # 在引号外部,空格通常是组间分隔或 * 周围空白,保留
            out_chars.append(ch)
    body = "".join(out_chars)

    # 把连续的 'X' 'Y' 改写成 'X'+'Y'(核心修复)
    # 重复多次直到不再变化,处理多词组
    prev_body = None
    while prev_body != body:
        prev_body = body
        body = re.sub(r"'\s*'\s*\+?\s*", "'+'", body)

    # 如果之前用的是字面 '+' 写法(LLM 给 `A + B`),把空格换成加号
    # 即 'A' + 'B' → 'A'+'B'
    body = re.sub(r"'\s+\+\s+'", "'+'", body)

    # 多余引号去重
    body = re.sub(r"'+", "'", body)
    body = body.strip()

    # 单组式(无 *)= 全库 OR 大杂烩,知网会忠实返回海量无关文献,
    # 必须至少 2 组概念交叉。2026-08-29 事故实证:缺此校验时一次检索
    # 返回 165 篇全领域文献,0 篇与主题交叉相关。
    if "*" not in body:
        raise ValueError(f"知网检索式必须至少 2 组概念交叉(含 *): {query}")

    return f"SU={body}"


INTENT_SYSTEM = """你是学术检索规划专家。任务:把用户的研究主题拆成 3 个数据库各自可执行的动态检索式列表,直接以 JSON 输出。

严格要求:
1. 每个数据库(中国知网 / OpenAlex / PubMed)输出 4～8 条检索式,数量由主题复杂度决定,不得固定为 3 条。至少覆盖严格式、中等式、场景式、方法/对象式、近义词式和宽松兜底式,并形成由严到宽的梯度。各条检索式必须有实质差异,不能只是重复改写。
2. 中国知网:用专业检索式语法。同义/近义词用 +,不同检索维度用 *。每条只能有一个 `SU=` 前缀,后续概念组直接用 `*` 连接,禁止写成 `SU=(...)*SU=(...)`。目标是高召回,不要把多个概念拼成只有少数论文会命中的超长固定短语。每条最多 3 组,组内允许 3~8 个高相关词,避免加入泛化词。
   **每条检索式必须含至少一个 `*`**(≥2 组概念交叉,如 `SU=('主题词A'+'主题词B')*'主题词C'`);严禁输出只有一组词的单组式——单组式会命中全库大杂烩。主题核心概念(对象/领域)必须作为一组参与交叉,不得只给「营销」「电商」这类泛概念。
3. OpenAlex:用 AND/OR/NOT 拼接,短语用双引号。
4. PubMed:用 [tiab] / [ti] 后缀限定字段,短语用双引号。
5. 不要列举具体论文;不要解释理由;只输出 JSON。
6. 第一行必须是 JSON 对象的开始大括号 `{`,不要任何前缀文字、思考块或代码围栏。
7. 主题中的概念必须转换成学术规范用语及其常见变体后再进检索式:用户措辞可能不
   是文献中的规范表述(如「权利寻租」在学术文献中规范用语是「权力寻租」,检索式
   必须同时覆盖「权力寻租」「寻租」;「村官」应映射为「村干部」)。口语词、个别
   用字不当都要映射到规范学术词,否则知网会因一字之差漏掉最核心的文献。
"""

INTENT_USER_TEMPLATE = """研究主题:{topic}
当前年份:{year}

请按上述要求输出 JSON,字段:
- topic_summary (一句英文研究问题,10~200 字符)
- queries_cnki: [4~8 个字符串] (知网专业检索式语法)
- queries_openalex: [4~8 个字符串] (OpenAlex 布尔式)
- queries_pubmed: [4~8 个字符串] (PubMed 方言)
"""


def _parse_json_lenient(raw: str):
    """把 LLM 输出的内容解析为 JSON。

    兼容以下情况:
    - 顶层被包在 ```json ... ``` 代码块里
    - 内容前/后有自然语言说明、思考块、Markdown 围栏
    - 字符串被截断(无终止符 / 缺右括号)→ 截断到最后一个完整对象
    """
    if raw is None:
        raise ValueError("LLM 返回为空")
    text = raw.strip()

    # 1) 抽取 ```json ... ``` 代码块(优先)
    fence = re.search(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", text, flags=re.DOTALL)
    if fence:
        text = fence.group(1).strip()

    # 2) 先尝试严格 json.loads
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 3) 抓第一个 { 或 [ 起点,尝试回退解析
    starts = [i for i in (text.find("{"), text.find("[")) if i != -1]
    if not starts:
        raise ValueError(f"LLM 输出里找不到 JSON 起点: {raw[:200]!r}")
    start = min(starts)
    candidate = text[start:]

    # 3a) 直接再试一次(可能原本就差一点点)
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass

    # 3b) 扫描到最后一个能完整闭合的位置
    last_good_end = -1
    depth_brace = depth_bracket = 0
    in_string = False
    escape = False
    for idx, ch in enumerate(candidate):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth_brace += 1
        elif ch == "}":
            depth_brace -= 1
            if depth_brace == 0 and depth_bracket == 0:
                last_good_end = idx + 1
        elif ch == "[":
            depth_bracket += 1
        elif ch == "]":
            depth_bracket -= 1
            if depth_brace == 0 and depth_bracket == 0:
                last_good_end = idx + 1

    if last_good_end <= 0:
        raise ValueError(f"LLM 输出 JSON 无法截断修复: {raw[:200]!r}")
    truncated = candidate[:last_good_end]
    try:
        return json.loads(truncated)
    except json.JSONDecodeError as exc:
        raise ValueError(f"LLM 输出 JSON 截断后仍不合法: {exc}; head={raw[:120]!r}") from exc


def _validate_queries(name: str, queries) -> None:
    if not isinstance(queries, list) or not MIN_QUERY_COUNT <= len(queries) <= MAX_QUERY_COUNT:
        actual = len(queries) if hasattr(queries, "__len__") else "?"
        raise ValueError(
            f"{name} 必须是 {MIN_QUERY_COUNT}～{MAX_QUERY_COUNT} 条字符串数组,实际收到"
            f" {type(queries).__name__} 长度 {actual}"
        )

    normalized = [q.strip() if isinstance(q, str) else "" for q in queries]
    if any(not query for query in normalized):
        raise ValueError(f"{name} 包含空检索式或非字符串")
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{name} 包含重复检索式")


def plan_query_strings(topic: str, year: int | None = None) -> dict:
    """把用户主题拆成三个数据库各自的动态检索式列表。"""
    year = year or datetime.datetime.now().year
    user_msg = INTENT_USER_TEMPLATE.format(topic=topic, year=year)

    last_err: Exception | None = None
    for attempt in range(3):
        try:
            raw = messages_create(
                INTENT_SYSTEM,
                user_msg,
                # 之前 4000 时 deepseek-v4-flash 偶尔在数组第 N 项字符串里被截断,
                # 导致所有 3 个数据库列表都只能解析到前 2-3 项。
                # 升到 8000 给足 buffer,并随 attempt 递增以应对偶发长尾。
                max_tokens=(6000, 8000, 8000)[attempt],
                temperature=(0.3, 0.5, 0.7)[attempt],
                # MiniMax-M3 是深度推理模型:实测本步骤 reasoning tokens ~6900,
                # 端到端 60~90s。之前 25s(防 ChatGLM 不可达)必定超时 → 3 次重试
                # 全失败 → 前端自动重试 → 表现为"永远卡在生成检索式"。
                # 现在 provider 锁定 minimax(实测可达),timeout 提到 120s。
                timeout=120.0,
            )
            data = _parse_json_lenient(raw)
            if isinstance(data, list):
                data = data[0] if data else {}
            if not isinstance(data, dict):
                raise ValueError("LLM 返回的检索规划不是 JSON 对象")

            topic_summary = (data.get("topic_summary") or "").strip()
            if len(topic_summary) < 10:
                raise ValueError(f"topic_summary 至少 10 字符,实际 {len(topic_summary)}")

            queries_cnki = []
            for index, query in enumerate(data.get("queries_cnki") or []):
                try:
                    queries_cnki.append(normalize_cnki_query(query))
                except (AttributeError, TypeError, ValueError) as exc:
                    raise ValueError(f"queries_cnki[{index}] 不是可执行的知网检索式: {exc}") from exc
            queries_openalex = data.get("queries_openalex") or []
            queries_pubmed = data.get("queries_pubmed") or []
            _validate_queries("queries_cnki", queries_cnki)
            _validate_queries("queries_openalex", queries_openalex)
            _validate_queries("queries_pubmed", queries_pubmed)

            return {
                "topic_summary": topic_summary,
                "queries_cnki": queries_cnki,
                "queries_openalex": [q.strip() for q in queries_openalex],
                "queries_pubmed": [q.strip() for q in queries_pubmed],
                "year": year,
            }
        except (APITimeoutError, APIConnectionError) as exc:
            # 超时/连接错误重试无意义(每次又要 ~120s,3 次 = 6 分钟):
            # 直接抛错让任务标记 failed / 前端明确提示,由用户手动重试。
            raise RuntimeError(
                f"LLM 连接超时或网络异常({type(exc).__name__}),请稍后重试: {exc}"
            ) from exc
        except Exception as exc:
            last_err = exc
            log.warning("plan_query_strings 第 %s 次失败: %s", attempt + 1, exc)

    raise RuntimeError(
        f"LLM 未能生成合法的 3 库 × 每库 {MIN_QUERY_COUNT}～{MAX_QUERY_COUNT} 条检索式"
        f" JSON(已重试 3 次): {last_err}"
    )