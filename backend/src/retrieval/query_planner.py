"""LLM 把用户主题拆成结构化 SearchIntent。

设计原则(对应通用文献综述 agent):
- LLM 是唯一的检索式生成者,不接受任何领域词表兜底;
- 输出必须是 SearchIntent(Pydantic schema),由 response_format 强约束;
- 失败重试 1 次仍失败 -> 直接抛错,绝不静默退化为整句匹配。

返 SearchIntent 后由各 AcademicSource.build_query(intent) 翻译成本地语法。
"""
from __future__ import annotations

import json
import logging

from llm.client import messages_create
from retrieval.intent import Concept, HardFilters, LoopConfig, SearchIntent, SnowballConfig

log = logging.getLogger(__name__)

INTENT_SYSTEM = """你是学术检索规划专家。
任务:把用户的研究主题拆解成结构化检索意图,直接以 JSON 输出。

严格要求:
1. 拆 3~5 个核心概念(concepts),前两个必须是研究对象/主体,后续是关系、任务或场景维度;
   每个概念同时给出中英文标签及 2~4 个中英文同义词
   (覆盖学术/平台/常用缩写,如 "无人机"、"无人飞行器"、"drone"、"UAV")。
   至少一个核心概念的 label_zh 或 synonyms_zh 必须直接取自用户研究主题;
   其余概念可扩展为与主题紧密相关的关系、机制、任务或场景维度。
2. boolean_template 必须用 concepts.id (A/B/C...) 拼接,如 "(A) AND (B) AND (C)";
   引用的概念必须恰好等于 concepts 列表(多一个少一个都不行)。
3. exclude_terms 列出能显著污染检索结果的词(如某主题要排除 "review" 时填 "review")。
4. filters.min_year / max_year 按主题合理填;max_year 默认当前年。
5. filters.allowed_types 选 ["article", "review"] (OpenAlex 规范名)。
6. filters.language 按主题需求,如纯英文 ["en"],中英混合 ["en","zh"]。
7. snowball:是否启用前向/后向引用,取决于综述深度需求。
8. 不允许用"按我所知",不要列举具体文献,只输出 JSON。
9. 只输出一个 JSON 对象:不要 ``` 代码围栏,不要任何解释、思考过程或前后缀文字。
"""

INTENT_USER_TEMPLATE = """研究主题:{topic}
当前年份:{year}

请按上述要求输出 JSON,字段:
- topic_summary (一句英文研究问题)
- concepts: [{{id, label_zh, label_en, synonyms_zh, synonyms_en, field, weight}}, ...] 3-5 个
- boolean_template: "(A) AND (B) AND (C)"
- exclude_terms: [...]
- filters: {{min_year, max_year, language, allowed_types, require_abstract, min_citations}}
- snowball: {{enabled, forward_depth, backward_depth, max_seeds, max_results}}
"""


def _truncate_concepts(data: dict) -> dict:
    """LLM 偶发给 >5 个概念;确定性截断到 5 个并同步 boolean_template。

    只处理简单的 "(A) AND/OR (B)" 链(LLM 按 prompt 生成的都是这种);
    删除被截断概念的组及其相邻布尔运算符。复杂嵌套由重试兜底。
    """
    concepts = data.get("concepts") or []
    if len(concepts) <= 5:
        return data
    import re

    drop_ids = [c["id"] for c in concepts[5:]]
    kept = concepts[:5]
    template = data.get("boolean_template", "")
    for did in drop_ids:
        template = re.sub(rf"\s*(?:AND|OR)\s*\(\s*{did}\s*\)", "", template)
        template = re.sub(rf"\(\s*{did}\s*\)\s*(?:AND|OR)\s*", "", template)
        template = re.sub(rf"\(\s*{did}\s*\)", "", template)
    # 清理开头/结尾残留的孤立 AND/OR 与多余空格
    template = re.sub(r"^\s*(?:AND|OR)\s*", "", template)
    template = re.sub(r"\s*(?:AND|OR)\s*$", "", template)
    template = re.sub(r"\s{2,}", " ", template).strip()
    data["concepts"] = kept
    data["boolean_template"] = template or "(A)"
    return data


def _repair_boolean_template(data: dict) -> dict:
    import re

    concepts = data.get("concepts") or []
    ids = [str(concept.get("id") or "").strip() for concept in concepts]
    ids = [concept_id for concept_id in ids if concept_id]
    if not ids:
        return data
    template = str(data.get("boolean_template") or "")
    referenced = set(re.findall(r"\b([A-Z])\b", template))
    if referenced != set(ids):
        data["boolean_template"] = " AND ".join(f"({concept_id})" for concept_id in ids)
    return data


def _validate_topic_grounding(topic: str, intent: SearchIntent) -> None:
    topic_compact = "".join(topic.split())
    candidates = [
        term.strip()
        for concept in intent.concepts
        for term in [concept.label_zh, *concept.synonyms_zh]
        if term and len(term.strip()) >= 2
    ]
    if not any(term in topic_compact or topic_compact in term for term in candidates):
        raise ValueError("至少一个核心概念或中文同义词必须直接锚定研究主题")


def _normalize_year_filters(data: dict, current_year: int | None = None) -> dict:
    import datetime

    year = current_year or datetime.datetime.now().year
    filters = data.setdefault("filters", {})
    filters["max_year"] = year
    if filters.get("min_year", 2020) > year:
        filters["min_year"] = year
    data["exclude_terms"] = []
    return data


def plan_intent(topic: str, year: int | None = None) -> SearchIntent:
    """把用户主题变成 SearchIntent。LLM 失败重试,仍失败抛错(不静默兜底)。"""
    import datetime
    year = year or datetime.datetime.now().year
    user_msg = INTENT_USER_TEMPLATE.format(topic=topic, year=year)

    schema = SearchIntent.model_json_schema()
    # MiniMax-M3 对 strict json_schema 兼容性极差(实测常返回空 content 或残缺 JSON);
    # 改用纯 prompt 约束 + 本地容错链(json 提取/数组容错/默认值)兜底,输出更稳定。
    # schema 仍用于 model_validate 校验,只是不传给模型。
    _ = schema

    last_err: Exception | None = None
    for attempt in range(3):
        try:
            raw = messages_create(
                INTENT_SYSTEM,
                user_msg,
                # MiniMax-M3 会先输出 <think> 推理,再输出 JSON;
                # token 上限太低(3000)会被推理耗尽导致 content 为空,提到 8000
                max_tokens=8000,
                temperature=(0.3, 0.5, 0.7)[attempt],
            )
            data = json.loads(raw)
            # MiniMax 偶发把对象包成数组(如 [{...}]);取首元素容错
            if isinstance(data, list):
                data = data[0] if data else {}
            # LLM 偶发给 >5 个概念,超 schema 上限;截断并同步模板
            data = _truncate_concepts(data)
            data = _repair_boolean_template(data)
            data = _normalize_year_filters(data)
            intent = SearchIntent.model_validate(data)
            _validate_topic_grounding(topic, intent)
            return intent
        except Exception as e:
            last_err = e
            log.warning("plan_intent 第 %s 次失败: %s", attempt + 1, e)

    raise RuntimeError(
        f"LLM 未能生成合法 SearchIntent(已重试 3 次): {last_err}"
    )


# === 旧 plan_query 兼容层 ===========================================
# 老接口 plan_query(topic) -> dict 保留,以便不破坏现有检索/任务代码。
# 新代码请一律走 plan_intent(topic) -> SearchIntent。

def plan_query(topic: str, default_year_start: int | None = None) -> dict:
    """旧接口兼容:返回 legacy dict 结构,内部走 SearchIntent。

    任何字段都不要被新代码依赖,只是为了不破坏现有单元测试和 API。
    """
    intent = plan_intent(topic)
    if default_year_start is None:
        intent.filters.min_year = None
        intent.filters.max_year = None
    else:
        intent.filters.min_year = default_year_start
    keywords_en = sorted({s for c in intent.concepts for s in c.synonyms_en})
    keywords_zh = sorted({s for c in intent.concepts for s in c.synonyms_zh})
    query_en = _render_boolean(intent)
    query_zh = render_cnki_expert(intent)
    queries_zh = render_cnki_candidates(intent)
    return {
        "topic_summary": intent.topic_summary,
        "concepts": [c.model_dump() for c in intent.concepts],
        "keywords_zh": keywords_zh,
        "keywords_en": keywords_en,
        "query_zh": query_zh,
        "queries_zh": queries_zh,
        "query_en": query_en,
        "queries_en": render_en_candidates(intent),  # 概念 id 模板(供各源渲染子式)
        "field_zh": "SU",
        "field_en": "default",
        "year_start": intent.filters.min_year,
        "year_end": intent.filters.max_year,
        "exclude": intent.exclude_terms,
        "intent": intent,  # 新代码直接用这个
    }


def _render_boolean(intent: SearchIntent) -> str:
    """把 boolean_template 里的 A/B/C 替换成 (synonym OR synonym) 形式,生成 OpenAlex 友好的字符串。"""
    import re
    groups = {}
    for c in intent.concepts:
        syns = c.synonyms_en or [c.label_en]
        quoted = [f'"{s}"' if " " in s else s for s in syns]
        groups[c.id] = "(" + " OR ".join(quoted) + ")"
    pattern = re.compile(r"\b([A-Z])\b")

    def _sub(m):
        return groups.get(m.group(1), m.group(0))

    return pattern.sub(_sub, intent.boolean_template)


def render_cnki_expert(intent: SearchIntent) -> str:
    import re

    groups = {}
    for concept in intent.concepts:
        terms = [concept.label_zh, *concept.synonyms_zh]
        if not terms:
            raise ValueError(f"Concept {concept.id} 缺少中文标签和同义词")
        unique_terms = list(dict.fromkeys(term.strip() for term in terms if term.strip()))
        quoted = [f"'{term.replace(chr(39), chr(39) * 2)}'" for term in unique_terms]
        groups[concept.id] = quoted[0] if len(quoted) == 1 else "(" + " + ".join(quoted) + ")"

    pattern = re.compile(r"\b([A-Z])\b")
    expression = pattern.sub(lambda match: groups[match.group(1)], intent.boolean_template)
    expression = re.sub(r"\s+AND\s+", " * ", expression, flags=re.IGNORECASE)
    expression = re.sub(r"\s+OR\s+", " + ", expression, flags=re.IGNORECASE)
    expression = re.sub(r"\((('[^']*'|\([^)]*\)))\)", r"\1", expression)
    return f"SU={expression}"


def render_cnki_candidates(
    intent: SearchIntent,
    max_candidates: int = 5,
    max_chars: int = 120,
) -> list[str]:
    def group(concept: Concept) -> str:
        source = [concept.label_zh, *concept.synonyms_zh]
        terms = list(dict.fromkeys(term.strip() for term in source if term.strip()))[:2]
        if not terms:
            raise ValueError(f"Concept {concept.id} 缺少中文标签和同义词")
        quoted = [f"'{term.replace(chr(39), chr(39) * 2)}'" for term in terms]
        return quoted[0] if len(quoted) == 1 else "(" + "+".join(quoted) + ")"

    groups = [group(concept) for concept in intent.concepts]
    if len(groups) < 2:
        # 1 概念(LLM 兜底场景):直接返回该概念的单条检索式,不生成多候选
        query = "SU=" + groups[0]
        return [query] if len(query) <= max_chars else []

    anchors = groups[:2]
    dimensions = groups[2:]
    raw = ["SU=" + "*".join(groups)]
    raw.extend("SU=" + "*".join([*anchors, dimension]) for dimension in dimensions)
    raw.append("SU=" + "*".join(anchors))

    candidates = []
    for query in raw:
        if len(query) > max_chars:
            continue
        if query not in candidates:
            candidates.append(query)
        if len(candidates) >= max_candidates:
            break
    if not candidates:
        raise ValueError(f"最短知网专业检索式仍超过 {max_chars} 字符")
    return candidates


def render_en_candidates(
    intent: SearchIntent,
    max_candidates: int = 5,
    min_concepts: int = 3,
) -> list[tuple[str, ...]]:
    """英文多子检索式拆分(对称中文 render_cnki_candidates)。

    语义单元 = 概念组(一个概念及其同义词)。长度超过阈值(概念数 >= min_concepts)
    的长英文检索式拆成多个独立的子检索式,按概念 id 模板输出,由各数据源
    build_sub_query 渲染成本地方言后再依次执行,最后由 PaperPool 合并去重:

      1. 全概念链:所有概念 AND,保留完整语义;
      2. 锚点(前 2 概念,即研究对象/主体)× 每个剩余维度:每个维度独立成式,
         避免"多概念全交过窄"导致零结果(与中文"锚点×单维度"策略一致);
      3. 仅锚点:最宽兜底式,保证首轮即有结果。

    概念数 < min_concepts 视为短检索式,不拆分,直接返回全概念链。
    拆分依据是概念(语义单元),不在词法层面切割,避免破坏英文短语/术语完整性。
    """
    ids = [c.id for c in intent.concepts]
    if len(ids) < min_concepts:
        return [tuple(ids)]

    anchors = ids[:2]
    dimensions = ids[2:]
    raw = [tuple(ids)]  # 全概念式
    raw.extend(tuple([*anchors, d]) for d in dimensions)  # 锚点 × 单维度
    raw.append(tuple(anchors))  # 仅锚点

    candidates: list[tuple[str, ...]] = []
    for template in raw:
        if template not in candidates:
            candidates.append(template)
        if len(candidates) >= max_candidates:
            break
    return candidates
