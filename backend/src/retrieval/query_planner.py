"""LLM 把用户主题拆成专业级检索式(中英文双套 + 概念组结构)。

产物:严格 JSON,字段:
  {
    "topic_summary": "<一句英文检索意图>",
    "year_start": 2020, "year_end": 2026,
    "concepts": [
      {"id": "A", "label": "短视频", "synonyms_zh": ["短视频","抖音","快手"],
       "synonyms_en": ["short video", "TikTok", "short-form video"]},
      ...
    ],
    "keywords_zh": ["短视频", "抖音", ...],
    "keywords_en": ["short video", "TikTok", ...],
    "query_zh": "(短视频+抖音+快手)*(大学生+高校学生+本科生)*(心理健康+抑郁+焦虑)",
    "query_en": '("short video" OR TikTok) AND ("college student" OR undergraduate) AND ...',
    "field_zh": "SU",
    "field_en": "default"
  }

重要质量要求:
  - 概念数 3~4 个(过少 = 没拆解;过多 = 太碎)
  - 每个概念至少 3 个同义词(覆盖学术/平台/英文表达)
  - 中文用 +/*,英文用 AND/OR + 引号锁短语

兜底:
  - LLM 失败 → 完整 prompt 重试 1 次
  - 仍失败 → 启发式拆分(中文分词 + 同义词词典)
"""
from __future__ import annotations

import json
import logging
import re

from llm.client import messages_create

log = logging.getLogger(__name__)

QUERY_SYSTEM = """你是学术检索规划专家。

任务:把用户的研究主题拆成"专业级布尔检索式"(中文+英文双套)。

【四步法,严格遵循】
1. 切分核心概念:必须拆成 3~4 个独立概念模块。不要把整个题目当一个检索词。
   例:"短视频使用对大学生心理健康的影响研究" → 短视频 + 大学生 + 心理健康(3 个)
   例:"基于深度学习的医学影像诊断研究" → 深度学习 + 医学影像 + 疾病诊断(3 个)

2. 扩充同义词:每个概念必须至少 3 个中英文同义词(覆盖:学术表达/平台名/口语/英文)
   例:概念"短视频" → 中文:短视频 / 抖音 / 快手 / 微视 / 视频号;英文:short video / TikTok / short-form video
   例:概念"大学生" → 中文:大学生 / 本科生 / 高校学生 / 在校生;英文:college student / undergraduate / university student
   例:概念"心理健康" → 中文:心理健康 / 心理状态 / 抑郁 / 焦虑 / 幸福感;英文:mental health / psychological well-being / depression / anxiety

3. 构造布尔式:
   - 中文(CNKI 高级检索):(+同义1+同义2+...) * (+同义...) * (+同义...)
   - 英文(PubMed/OpenAlex):("phrase" OR word) AND ("phrase" OR word) AND ...

4. 输出严格 JSON,字段:
{
  "topic_summary": "<一句英文检索意图>",
  "year_start": 2020, "year_end": 2026,
  "concepts": [
    {"id": "A", "label": "<概念中文标签>", "label_en": "<概念英文>",
     "synonyms_zh": ["同义词1", "同义词2", "同义词3"],
     "synonyms_en": ["synonym1", "synonym2", "synonym3"]},
    ...  // 3-4 个概念,不多不少
  ],
  "query_zh": "<CNKI 布尔式 (+...+...) *(+...+...) *(+...+...)>",
  "query_en": '<英文布尔式 ("a" OR "b") AND ("c" OR "d") AND ("e" OR "f")>',
  "field_zh": "SU",
  "field_en": "default"
}

【硬性检查】
- concepts 必须恰好 3~4 个
- 每个概念的 synonyms_zh 和 synonyms_en 必须 ≥ 3 个
- query_zh 必须包含 * 字符(否则没 AND)
- query_en 必须包含 AND

不要列举具体文献,不要使用"据我所知"。只输出 JSON,不要 markdown 代码块包裹。"""


# 启发式兜底:常见同义词词典(覆盖硕士论文题目高频概念)
HEURISTIC_SYNONYMS = {
    "短视频": ["短视频", "抖音", "快手", "微视", "视频号", "TikTok", "short video"],
    "大学生": ["大学生", "本科生", "高校学生", "在校生", "college student", "undergraduate"],
    "心理健康": ["心理健康", "心理状态", "抑郁", "焦虑", "幸福感", "mental health", "depression"],
    "深度学习": ["深度学习", "神经网络", "卷积神经网络", "深度神经网络", "deep learning", "neural network", "CNN"],
    "医学影像": ["医学影像", "医学图像", "影像组学", "CT", "MRI", "medical imaging"],
    "疾病诊断": ["疾病诊断", "辅助诊断", "临床诊断", "诊断模型", "影像诊断", "disease diagnosis", "clinical diagnosis"],
    "诊断": ["诊断", "辅助诊断", "影像诊断", "诊断模型", "diagnosis", "diagnostic"],
    "影像": ["影像", "医学影像", "图像", "imaging", "image"],
    "卷积神经网络": ["卷积神经网络", "CNN", "深度学习", "convolutional neural network"],
    "人工智能": ["人工智能", "AI", "机器学习", "深度学习", "artificial intelligence", "machine learning"],
    "营销": ["营销", "市场营销", "营销策略", "品牌营销", "marketing"],
    "水产品": ["水产品", "水海产品", "海鲜", "鱼类产品", "seafood", "aquatic product"],
    "消费者": ["消费者", "顾客", "购买者", "consumer", "customer", "buyer"],
    "购买意愿": ["购买意愿", "购买行为", "消费意愿", "purchase intention", "buying behavior"],
    "电商": ["电商", "电子商务", "网购", "网络购物", "e-commerce", "online shopping"],
    "直播": ["直播", "直播带货", "网络直播", "live streaming", "live commerce"],
    "乡村振兴": ["乡村振兴", "农村发展", "三农", "rural revitalization", "rural development"],
    "数字经济": ["数字经济", "数字化转型", "digital economy", "digital transformation"],
    "可持续发展": ["可持续发展", "绿色发展", "低碳", "sustainable development", "sustainability"],
}


def plan_query(topic: str, default_year_start: int = 2020) -> dict:
    """返回结构化检索式(concepts / keywords_zh_en / query_zh_en / year_*)。

    失败兜底顺序:LLM 重试 → 启发式拆分 → 单概念包装。
    """
    user_msg = (
        f"研究主题:{topic}\n"
        f"当前年份:2026\n"
        f"请严格按四步法拆概念 + 列同义词 + 给布尔式。"
    )

    data: dict = {}
    # 第一次 LLM
    try:
        raw = messages_create(
            QUERY_SYSTEM, user_msg,
            max_tokens=2000, temperature=0.4,
        )
        raw = re.sub(r"```(?:json)?|```", "", raw).strip()
        data = json.loads(raw)
    except Exception as e:
        log.warning("query_planner LLM 首次失败: %s", e)
        # 重试 1 次
        try:
            raw = messages_create(
                QUERY_SYSTEM, user_msg,
                max_tokens=2000, temperature=0.6,
            )
            raw = re.sub(r"```(?:json)?|```", "", raw).strip()
            data = json.loads(raw)
        except Exception as e2:
            log.warning("query_planner LLM 重试失败,走启发式: %s", e2)
            data = {}

    concepts = _normalise_concepts(data.get("concepts"))

    # 质量检查:概念数太少 或 同义词不足 → 启发式拆分
    if len(concepts) < 3 or not _has_enough_synonyms(concepts):
        log.info("LLM 输出概念数不足(%d)或同义词不足,启用启发式拆分", len(concepts))
        heuristic = _heuristic_split(topic)
        # 把 LLM 给的更具体的概念叠加启发式
        if concepts:
            for c in concepts:
                # 把启发式中相同 label 的同义词合并
                hit = next((h for h in heuristic if h["label"] == c["label"]), None)
                if hit:
                    for s in hit["synonyms_zh"]:
                        if s not in c["synonyms_zh"]:
                            c["synonyms_zh"].append(s)
                    for s in hit["synonyms_en"]:
                        if s not in c["synonyms_en"]:
                            c["synonyms_en"].append(s)
            # 把启发式独有的加进来
            for h in heuristic:
                if not any(c["label"] == h["label"] for c in concepts):
                    concepts.append(h)
        else:
            concepts = heuristic

    # 截断 5 个(过多会变碎)
    concepts = concepts[:5]

    keywords_zh = sorted({s for c in concepts for s in c["synonyms_zh"]})
    keywords_en = sorted({s for c in concepts for s in c["synonyms_en"]})

    query_zh = (data.get("query_zh") or "").strip()
    query_en = (data.get("query_en") or "").strip()

    # 如果 LLM 没给布尔式,Python 自动构造
    if "*" not in query_zh:
        query_zh = _build_cnki_query(concepts)
    if "AND" not in query_en.upper():
        query_en = _build_english_query(concepts)

    try:
        year_start = int(data.get("year_start") or default_year_start)
        year_end = int(data.get("year_end") or 2026)
    except (ValueError, TypeError):
        year_start, year_end = default_year_start, 2026

    return {
        "topic_summary": str(data.get("topic_summary") or topic),
        "concepts": concepts,
        "keywords_zh": keywords_zh,
        "keywords_en": keywords_en,
        "query_zh": query_zh,
        "query_en": query_en,
        "field_zh": str(data.get("field_zh") or "SU"),
        "field_en": str(data.get("field_en") or "default"),
        "year_start": year_start,
        "year_end": year_end,
        "exclude": [str(x) for x in (data.get("exclude") or [])],
    }


# ─── 启发式拆分 ────────────────────────────────────────

def _heuristic_split(topic: str) -> list[dict]:
    """从 topic 字符串里匹配高频概念,匹配上的 → 一个概念 + 同义词词典。

    没匹配上的 → 整句作为一个概念兜底。
    """
    topic_norm = re.sub(r"\s+", "", topic)
    matched: list[dict] = []
    matched_labels: set[str] = set()
    for label, syns in HEURISTIC_SYNONYMS.items():
        if label in topic_norm:
            zh = [s for s in syns if not re.match(r"^[a-z\s\-]+$", s, re.IGNORECASE)]
            en = [s for s in syns if re.match(r"^[a-z\s\-]+$", s, re.IGNORECASE)]
            if zh and en:
                matched.append({
                    "id": chr(ord("A") + len(matched)),
                    "label": label,
                    "label_en": label,
                    "synonyms_zh": zh,
                    "synonyms_en": en,
                })
                matched_labels.add(label)

    # 至少 2 个概念才不至于 1 个
    if len(matched) < 2:
        # 整句作为兜底
        matched.append({
            "id": chr(ord("A") + len(matched)),
            "label": topic,
            "label_en": topic,
            "synonyms_zh": [topic],
            "synonyms_en": [topic],
        })
    return matched


# ─── 工具 ─────────────────────────────────────────

def _normalise_concepts(raw) -> list[dict]:
    if not isinstance(raw, list):
        return []
    out = []
    for i, c in enumerate(raw):
        if not isinstance(c, dict):
            continue
        out.append({
            "id": str(c.get("id") or chr(ord("A") + i)),
            "label": str(c.get("label") or "").strip() or f"概念{chr(ord('A')+i)}",
            "label_en": str(c.get("label_en") or c.get("label") or "").strip(),
            "synonyms_zh": [str(s).strip() for s in (c.get("synonyms_zh") or []) if str(s).strip()],
            "synonyms_en": [str(s).strip() for s in (c.get("synonyms_en") or []) if str(s).strip()],
        })
    return out


def _has_enough_synonyms(concepts: list[dict]) -> bool:
    """每个概念是否至少 2 个中文同义词 + 2 个英文同义词。"""
    if not concepts:
        return False
    for c in concepts:
        if len(c.get("synonyms_zh") or []) < 2:
            return False
        if len(c.get("synonyms_en") or []) < 2:
            return False
    return True


def _build_cnki_query(concepts: list[dict]) -> str:
    parts = []
    for c in concepts:
        syns = c["synonyms_zh"] or [c["label"]]
        parts.append("(" + "+".join(syns) + ")")
    return " * ".join(parts)


def _build_english_query(concepts: list[dict]) -> str:
    parts = []
    for c in concepts:
        syns = c["synonyms_en"] or [c["label_en"] or c["label"]]
        quoted = []
        for s in syns:
            if " " in s and not s.startswith('"'):
                quoted.append(f'"{s}"')
            else:
                quoted.append(s)
        parts.append("(" + " OR ".join(quoted) + ")")
    return " AND ".join(parts)