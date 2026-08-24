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

from llm.client import messages_create

log = logging.getLogger(__name__)

MIN_QUERY_COUNT = 4
MAX_QUERY_COUNT = 8


def normalize_cnki_query(query: str) -> str:
    """将知网专业检索式统一为单个字段前缀的可执行形式。"""
    normalized = " ".join(query.strip().split())
    if not normalized:
        raise ValueError("知网检索式不能为空")

    upper = normalized.upper()
    if not upper.startswith("SU="):
        raise ValueError(f"知网检索式必须以 SU= 开头: {query}")

    body = normalized[3:].strip()
    body = re.sub(r"\s*\*\s*SU\s*=\s*", " * ", body, flags=re.IGNORECASE)
    if re.search(r"\bSU\s*=", body, flags=re.IGNORECASE):
        raise ValueError(f"知网检索式包含重复字段前缀 SU=: {query}")

    return f"SU={body}"


INTENT_SYSTEM = """你是学术检索规划专家。任务:把用户的研究主题拆成 3 个数据库各自可执行的动态检索式列表,直接以 JSON 输出。

严格要求:
1. 每个数据库(中国知网 / OpenAlex / PubMed)输出 4～8 条检索式,数量由主题复杂度决定,不得固定为 3 条。至少覆盖严格式、中等式、场景式、方法/对象式、近义词式和宽松兜底式,并形成由严到宽的梯度。各条检索式必须有实质差异,不能只是重复改写。
2. 中国知网:用专业检索式语法。同义/近义词用 +,不同检索维度用 *。每条只能有一个 `SU=` 前缀,后续概念组直接用 `*` 连接,禁止写成 `SU=(...)*SU=(...)`。目标是高召回,不要把多个概念拼成只有少数论文会命中的超长固定短语。每条最多 3 组,组内允许 3~8 个高相关词,避免加入泛化词。
3. OpenAlex:用 AND/OR/NOT 拼接,短语用双引号。
4. PubMed:用 [tiab] / [ti] 后缀限定字段,短语用双引号。
5. 不要列举具体论文;不要解释理由;只输出 JSON。
6. 第一行必须是 JSON 对象的开始大括号 `{`,不要任何前缀文字、思考块或代码围栏。
"""

INTENT_USER_TEMPLATE = """研究主题:{topic}
当前年份:{year}

请按上述要求输出 JSON,字段:
- topic_summary (一句英文研究问题,10~200 字符)
- queries_cnki: [4~8 个字符串] (知网专业检索式语法)
- queries_openalex: [4~8 个字符串] (OpenAlex 布尔式)
- queries_pubmed: [4~8 个字符串] (PubMed 方言)
"""


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
                max_tokens=4000,
                temperature=(0.3, 0.5, 0.7)[attempt],
                timeout=60.0,
            )
            data = json.loads(raw)
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
        except Exception as exc:
            last_err = exc
            log.warning("plan_query_strings 第 %s 次失败: %s", attempt + 1, exc)

    raise RuntimeError(
        f"LLM 未能生成合法的 3 库 × 每库 {MIN_QUERY_COUNT}～{MAX_QUERY_COUNT} 条检索式"
        f" JSON(已重试 3 次): {last_err}"
    )