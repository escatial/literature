"""LLM 把用户主题直接拆成 3 个库的 3 条检索式字符串。

设计原则(对应通用文献综述 agent):
- LLM 是唯一的检索式生成者,不接受任何领域词表兜底;
- LLM 直接输出完整检索式字符串,不再拆 concept / boolean_template / 同义词;
- 每库恰好 3 条检索式,后端逐条遍历翻页 + 跨条去重汇入文献池;
- 失败重试 3 次仍失败 -> 直接抛错,绝不静默兜底。
"""
from __future__ import annotations

import datetime
import json
import logging

from llm.client import messages_create

log = logging.getLogger(__name__)

INTENT_SYSTEM = """你是学术检索规划专家。任务:把用户的研究主题直接拆成 3 个数据库各自可执行的检索式,直接以 JSON 输出。

严格要求:
1. 每个数据库(中国知网 / OpenAlex / PubMed)分别输出恰好 3 条检索式,覆盖:
   (a) 主概念 + 全部维度(最严格);
   (b) 主概念 + 1 个关键维度(中等);
   (c) 主概念(最宽松兜底,保证首轮即有结果)。
   3 条检索式应当有梯度,前一条在前一条基础上放宽。
2. 中国知网:用专业检索式语法。同义/近义词用 +,不同检索维度用 *。
   每条检索式必须同时保留“场景限定概念”和“核心对象概念”,不能只写一个宽泛概念。
   以农村末端物流为例,推荐结构:
   1) SU=('农村物流'+'乡村物流'+'农村配送')*('末端配送'+'最后一公里')*('配送效率'+'配送成本'+'共同配送')
   2) SU=('农村物流'+'乡村物流'+'农村配送')*('末端配送'+'最后一公里')
   3) SU=('农村末端物流配送'+'乡村末端物流配送'+'农村最后一公里配送')
   要求:第 1、2 条至少 2 组 AND,且必须包含场景限定词和核心对象词;第 3 条使用相对完整的具体短语,不拆成宽泛单词。
   每条最多 3 组,组内最多 3 个词,避免堆砌“物流网络、政策支持”这类泛化词。
3. OpenAlex:用 AND/OR/NOT 拼接,短语用双引号,如
   ("Understanding by Design" OR UbD) AND ("math teaching" OR "mathematics education")。
4. PubMed:用 [tiab] / [ti] 后缀限定字段,短语用双引号,如
   ("Understanding by Design"[tiab] OR UbD[tiab]) AND "math teaching"[tiab]。
5. 不要列举具体论文;不要解释理由;只输出 JSON。
6. 第一行必须是 JSON 对象的开始大括号 `{`。不要任何前缀文字、
   不要 `<think>` / `<thinking>` 等思考块、不要 ``` 代码围栏、
   不要"好的/以下是/我先分析"等开场白。直接以 `{` 开头、以 `}` 结尾。
"""

INTENT_USER_TEMPLATE = """研究主题:{topic}
当前年份:{year}

请按上述要求输出 JSON,字段:
- topic_summary (一句英文研究问题,10~200 字符)
- queries_cnki: [string, string, string]  (恰好 3 条,知网专业检索式语法)
- queries_openalex: [string, string, string]  (恰好 3 条,OpenAlex 布尔式)
- queries_pubmed: [string, string, string]  (恰好 3 条,PubMed 方言)
"""


def _validate_queries(name: str, queries) -> None:
    if not isinstance(queries, list) or len(queries) != 3:
        raise ValueError(
            f"{name} 必须是恰好 3 条字符串数组,实际收到 {type(queries).__name__} 长度 "
            f"{len(queries) if hasattr(queries, '__len__') else '?'}"
        )
    for i, q in enumerate(queries):
        if not isinstance(q, str) or not q.strip():
            raise ValueError(f"{name}[{i}] 必须是非空字符串,实际 {q!r}")


def plan_query_strings(topic: str, year: int | None = None) -> dict:
    """把用户主题直接拆成 3 个库各自的 3 条检索式字符串。失败重试 3 次,仍失败抛错。"""
    year = year or datetime.datetime.now().year
    user_msg = INTENT_USER_TEMPLATE.format(topic=topic, year=year)

    last_err: Exception | None = None
    for attempt in range(3):
        try:
            raw = messages_create(
                INTENT_SYSTEM,
                user_msg,
                # 单次超时 60s:上游 LLM 当前响应极慢,避免 3 次重试叠加 5+ 分钟
                max_tokens=2000,
                temperature=(0.3, 0.5, 0.7)[attempt],
                timeout=60.0,
            )
            data = json.loads(raw)
            # 容错:LLM 偶发把对象包成数组(如 [{...}]);取首元素
            if isinstance(data, list):
                data = data[0] if data else {}

            topic_summary = (data.get("topic_summary") or "").strip()
            if len(topic_summary) < 10:
                raise ValueError(f"topic_summary 至少 10 字符,实际 {len(topic_summary)}")

            queries_cnki = data.get("queries_cnki") or []
            queries_openalex = data.get("queries_openalex") or []
            queries_pubmed = data.get("queries_pubmed") or []
            _validate_queries("queries_cnki", queries_cnki)
            _validate_queries("queries_openalex", queries_openalex)
            _validate_queries("queries_pubmed", queries_pubmed)

            return {
                "topic_summary": topic_summary,
                "queries_cnki": [q.strip() for q in queries_cnki],
                "queries_openalex": [q.strip() for q in queries_openalex],
                "queries_pubmed": [q.strip() for q in queries_pubmed],
                "year": year,
            }
        except Exception as e:
            last_err = e
            log.warning("plan_query_strings 第 %s 次失败: %s", attempt + 1, e)

    raise RuntimeError(
        f"LLM 未能生成合法的 3 库 × 3 检索式 JSON(已重试 3 次): {last_err}"
    )