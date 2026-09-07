"""主题-文献相关性匹配机制:四维度量化评估 + 三级分层筛选。

四个核心评估指标:
1. 核心关键词重合度 keyword_overlap  —— 离线计算,可复现可验证。
   v8.3 优先「概念组语义匹配」:先由 LLM 把主题拆成中英概念组(如
   权利寻租/权力寻租/rent-seeking 同组),文献文本命中组内任一变体
   即整组命中。彻底修复两个硬伤:①中文主题对英文文献字面匹配恒 0 分;
   ②「权利寻租」vs 规范用语「权力寻租」一字之差导致中文文献全灭。
   概念组生成失败时退回字面覆盖率(并剔除「研究/视角」等套话虚字)。
2. 研究领域匹配度   field_match       —— LLM 评分
3. 研究方法适用性   method_applicability —— LLM 评分
4. 结论参考价值     conclusion_value  —— LLM 评分

分级规则(高相关必须同时满足三条硬标准):
- 高相关 high  : 关键词重合度 ≥ 80% 且 研究领域完全贴合 且 研究方法可直接参考
- 低相关 low   : 加权总分 < 50 或 关键词重合度 < 40%(仅可作背景补充,禁止作为核心论据)
- 中相关 medium: 其余
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from retrieval.types import Paper

log = logging.getLogger(__name__)

# 高相关的三条硬标准阈值
KEYWORD_OVERLAP_HIGH_THRESHOLD = 80.0   # 核心关键词重合度 ≥ 80%
FIELD_MATCH_FULL_THRESHOLD = 90.0       # 研究领域"完全贴合"
METHOD_APPLICABLE_THRESHOLD = 70.0      # 研究方法"可直接参考或借鉴"

# 低相关下限
LOW_TOTAL_THRESHOLD = 50.0
LOW_KEYWORD_THRESHOLD = 40.0

# 四维度权重(关键词重合度权重最高,因为它是唯一可精确复现的指标)
_WEIGHTS = {
    "keyword_overlap": 0.35,
    "field_match": 0.30,
    "method_applicability": 0.20,
    "conclusion_value": 0.15,
}

# LLM 未返回维度分时的保守默认值:取中位,确保不会凭空升级为高相关
_DEFAULT_LLM_SCORE = 50.0

# 分级排序权重(清单与配额排布都按此降序)
GRADE_ORDER = {"high": 0, "medium": 1, "low": 2}
GRADE_LABELS = {"high": "高相关", "medium": "中相关", "low": "低相关"}

# 主题分词用的连接词/虚词,不参与关键词比对
_CN_CONNECTORS = "与和及或的中下上对于关于基于面向针对以及并"
_EN_STOPWORDS = frozenset({
    "the", "and", "for", "with", "from", "into", "that", "this",
    "are", "was", "were", "based", "using", "study", "research",
})
_CN_SEG_RE = re.compile(r"[\u4e00-\u9fff]+")
_EN_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9-]{2,}")


# 中文片段参与覆盖率统计的最短命中长度(单字命中噪声太大,不计入)
_MIN_CN_HIT_LEN = 2

# 学术套话虚词:字面兜底比对前从主题片段中剥离,避免「理论视角/研究」
# 这类无区分度的字占覆盖率分母,把真实相关文献稀释成低相关。
_CN_FILLER_WORDS = (
    "研究", "分析", "探讨", "综述", "视角", "理论", "框架", "路径",
    "对策", "问题", "背景", "现状", "述评", "实证", "机制", "模式",
)


def _strip_filler_words(segment: str) -> str:
    """剥离主题片段中的套话虚词,得到参与字面比对的有效部分。"""
    for word in _CN_FILLER_WORDS:
        segment = segment.replace(word, "")
    return segment


# ===== 概念组:语义级重合度的比对单元 =====

_CONCEPT_SYSTEM = """你是学术文献检索专家。任务:把用户的研究主题拆解为恰好 5 个核心概念组,用于后续对中英文学文献做语义级关键词匹配。

要求:
1. 恰好 5 组,按以下维度各取其一:
   ①核心理论/机制(如主题「权利寻租…」须含规范用语「权力寻租」「寻租」)
   ②研究对象(如「村干部」含「村官」「村级干部」「农村基层干部」)
   ③核心问题/行为(如「职务犯罪」「贪污」「腐败」)
   ④治理/对策动作(如「治理」「监督」「预防」)
   ⑤情境限定(主题若限定农村/乡村等情境,给「农村」「乡村」「基层」等宽情境词;
     若主题无情境限定,此组改为主题最核心的延伸概念)
   「研究」「分析」「视角」这类套话不要单独成概念。
2. 每个概念给出中文同义/规范变体(必须包含主题原词;若主题用词不是学术规范用语,
   必须补上规范用语,如「权利寻租」须含「权力寻租」「寻租」)和英文对应变体。
3. 变体必须是文献标题/摘要中真实会出现的表述,不要生造词;英文变体同时给连字符和空格两种写法(如 rent-seeking 与 rent seeking)。
4. 恰好 5 组,不多不少。
严格输出 JSON 对象(不要 markdown 代码块,不要解释):
{"concepts": [{"cn": ["..."], "en": ["..."]}]}
"""


def _build_concept_groups_once(topic: str) -> list[dict] | None:
    """单次 LLM 调用生成概念组;解析失败/为空返回 None。"""
    from llm.client import messages_create

    raw = messages_create(
        _CONCEPT_SYSTEM,
        f"研究主题:{topic}",
        max_tokens=1500,
        temperature=0.3,
        timeout=60.0,
    )
    cleaned = re.sub(r"```(?:json)?|```", "", raw or "").strip()
    data = json.loads(cleaned)
    if isinstance(data, list):
        data = data[0] if data else {}
    groups: list[dict] = []
    for item in (data or {}).get("concepts") or []:
        if not isinstance(item, dict):
            continue
        cn = [str(v).strip().lower() for v in item.get("cn") or [] if str(v).strip()]
        en = [str(v).strip().lower() for v in item.get("en") or [] if str(v).strip()]
        if cn or en:
            groups.append({"cn": cn, "en": en})
    return groups or None


def build_concept_groups(topic: str) -> list[dict] | None:
    """LLM 把主题拆成中英概念组,供 keyword_overlap 做语义级匹配。

    返回 [{"cn": [...], "en": [...]}];失败或主题为空时返回 None,
    调用方退回字面覆盖率比对(算法仍可复现,只是退化为字面级)。
    每次综述任务只调用一次(内部偶发空返回重试 1 次),结果对当次全部文献一致。
    """
    if not (topic or "").strip():
        return None
    groups: list[dict] | None = None
    for attempt in (1, 2):
        try:
            groups = _build_concept_groups_once(topic)
        except Exception as exc:
            # 概念组是增强项,失败不阻塞综述主流程
            log.warning("概念组生成失败(第 %d 次): %s", attempt, exc)
            continue
        if groups:
            break
        log.warning("概念组生成为空(第 %d 次)", attempt)
    if not groups:
        return None
    if len(groups) > 5:
        # 80% 高相关线在 4/5 时才可达;组数超了截前 5 组(prompt 已约束,
        # 此处是 LLM 不听话时的保险)
        groups = groups[:5]
    log.info("概念组生成成功:%d 组(主题:%s)", len(groups), topic[:30])
    return groups


def _split_chinese_segment(seg: str) -> list[str]:
    """把中文片段切成可展示的候选词(≤4 字整体保留,更长则用 2 字滑窗)。

    仅用于 topic_keywords 的候选词展示;重合度改由 _segment_coverage 按
    最长公共子串覆盖率计算,不受切词粒度影响。
    """
    if len(seg) <= 4:
        return [seg]
    return [seg[i : i + 2] for i in range(len(seg) - 1)]


def _segment_coverage(seg: str, haystack: str) -> int:
    """统计中文片段中被 haystack 覆盖的字数。

    逐字向右扫描,取从当前位置出发能在 haystack 中匹配到的最长子串;
    长度 ≥ _MIN_CN_HIT_LEN 才算命中,命中后跳过整段已覆盖字符。
    这样 "新能源汽车市场" 命中 "能源汽车市场" 时能计 6 字,
    不会因为切词把 "源汽" / "车市场" 这类碎片判为未命中。
    """
    covered = 0
    idx = 0
    length = len(seg)
    while idx < length:
        best = 0
        for end in range(length, idx + _MIN_CN_HIT_LEN - 1, -1):
            if seg[idx:end] in haystack:
                best = end - idx
                break
        if best >= _MIN_CN_HIT_LEN:
            covered += best
            idx += best
        else:
            idx += 1
    return covered


def topic_keywords(topic: str) -> list[str]:
    """抽取主题的核心关键词,按出现顺序去重。"""
    words: list[str] = []
    for seg in _CN_SEG_RE.findall(topic or ""):
        for part in re.split(f"[{_CN_CONNECTORS}]", seg):
            if part:
                words.extend(_split_chinese_segment(part))
    for token in _EN_WORD_RE.findall(topic or ""):
        lowered = token.lower()
        if lowered not in _EN_STOPWORDS:
            words.append(lowered)
    seen: set[str] = set()
    unique: list[str] = []
    for word in words:
        if word not in seen:
            seen.add(word)
            unique.append(word)
    return unique


def keyword_overlap(
    topic: str,
    text: str,
    concept_groups: list[dict] | None = None,
) -> float:
    """核心关键词重合度(0-100)。

    传入概念组时:语义级匹配 —— 文献文本命中组内任一中/英变体即该概念命中,
    重合度 = 命中概念组数 / 概念组总数。中英文献同一把尺子。
    未传时:退回字面覆盖率,但主题片段先剥离套话虚字再统计。
    """
    haystack = (text or "").lower()
    if not haystack:
        return 0.0
    if concept_groups:
        hit = sum(
            1
            for group in concept_groups
            if any(v in haystack for v in group["cn"])
            or any(v in haystack for v in group["en"])
        )
        return round(hit / len(concept_groups) * 100, 2)

    # 字面兜底:剥离虚词后的片段覆盖率(历史行为,仅作概念组失败的降级)
    cn_segments = [
        stripped
        for seg in _CN_SEG_RE.findall(topic or "")
        for part in re.split(f"[{_CN_CONNECTORS}]", seg)
        if part
        for stripped in [_strip_filler_words(part)]
        if stripped
    ]
    en_words = [
        token.lower()
        for token in _EN_WORD_RE.findall(topic or "")
        if token.lower() not in _EN_STOPWORDS
    ]
    total_units = sum(len(segment) for segment in cn_segments) + len(en_words)
    if not total_units:
        return 0.0
    covered = sum(_segment_coverage(segment.lower(), haystack) for segment in cn_segments)
    covered += sum(1 for word in en_words if word in haystack)
    return round(covered / total_units * 100, 2)


def _paper_text(paper: Paper) -> str:
    """参与关键词比对的文本:标题 + 摘要 + 期刊名。"""
    return " ".join(filter(None, [paper.title, paper.abstract, paper.journal]))


@dataclass
class RelevanceScore:
    """单篇文献的四维度评估结果。"""

    lit_id: str
    keyword_overlap: float
    field_match: float
    method_applicability: float
    conclusion_value: float
    reason: str = ""
    match_points: list[str] = field(default_factory=list)

    @property
    def total(self) -> float:
        """加权总分(0-100)。"""
        score = (
            self.keyword_overlap * _WEIGHTS["keyword_overlap"]
            + self.field_match * _WEIGHTS["field_match"]
            + self.method_applicability * _WEIGHTS["method_applicability"]
            + self.conclusion_value * _WEIGHTS["conclusion_value"]
        )
        return round(score, 2)

    @property
    def grade(self) -> str:
        """三级相关性等级。"""
        if (
            self.keyword_overlap >= KEYWORD_OVERLAP_HIGH_THRESHOLD
            and self.field_match >= FIELD_MATCH_FULL_THRESHOLD
            and self.method_applicability >= METHOD_APPLICABLE_THRESHOLD
        ):
            return "high"
        if (
            self.total < LOW_TOTAL_THRESHOLD
            or self.keyword_overlap < LOW_KEYWORD_THRESHOLD
        ):
            return "low"
        return "medium"

    def to_row(self, title: str = "") -> dict:
        """分级清单的一行:等级 + 评估得分 + 核心匹配点。"""
        return {
            "lit_id": self.lit_id,
            "title": title,
            "grade": self.grade,
            "grade_label": GRADE_LABELS[self.grade],
            "total": self.total,
            "match_points": list(self.match_points),
            "reason": self.reason,
            "dimensions": {
                "keyword_overlap": self.keyword_overlap,
                "field_match": self.field_match,
                "method_applicability": self.method_applicability,
                "conclusion_value": self.conclusion_value,
            },
        }


def _coerce_score(value: object) -> float:
    """把 LLM 返回的维度分收敛到 [0, 100];缺失或非法时用保守默认值。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return _DEFAULT_LLM_SCORE
    return round(min(100.0, max(0.0, float(value))), 2)


def _match_points(
    topic: str,
    paper: Paper,
    score: "RelevanceScore",
    concept_groups: list[dict] | None = None,
) -> list[str]:
    """核心匹配点:说明这篇文献凭什么拿到该等级。"""
    text = _paper_text(paper).lower()
    hit_words: list[str] = []
    if concept_groups:
        # 概念组模式:列出实际命中的概念变体,可解释性对齐算法本身
        hit_variants = [
            variant
            for group in concept_groups
            for variant in (*group["cn"], *group["en"])
            if variant in text
        ]
        if hit_variants:
            points_head = "命中概念: " + "、".join(dict.fromkeys(hit_variants[:6]))
            hit_words = []
        else:
            points_head = "未命中任何概念变体"
    else:
        for segment in (
            part
            for seg in _CN_SEG_RE.findall(topic or "")
            for part in re.split(f"[{_CN_CONNECTORS}]", seg)
            if part
        ):
            if segment.lower() in text:
                hit_words.append(segment)
            else:
                for size in range(len(segment), _MIN_CN_HIT_LEN - 1, -1):
                    fragments = [
                        segment[index : index + size]
                        for index in range(len(segment) - size + 1)
                        if segment[index : index + size].lower() in text
                    ]
                    if fragments:
                        hit_words.extend(fragments[:2])
                        break
        hit_words.extend(
            word for word in topic_keywords(topic)
            if word.isascii() and word in text
        )
        points_head = (
            "命中主题片段: " + "、".join(dict.fromkeys(hit_words[:6]))
            if hit_words else "未命中任何主题核心词"
        )
    points: list[str] = [points_head]
    points.append(f"关键词重合度 {score.keyword_overlap}%")
    points.append(f"研究领域匹配度 {score.field_match}")
    points.append(f"研究方法适用性 {score.method_applicability}")
    points.append(f"结论参考价值 {score.conclusion_value}")
    return points


def grade_papers(
    topic: str,
    papers: list[Paper],
    decisions: list[dict],
    concept_groups: list[dict] | None = None,
) -> dict[str, RelevanceScore]:
    """混合评分:关键词重合度离线算,其余三维取 LLM 打分。

    传入 concept_groups 时重合度走语义级概念组匹配,否则退回字面覆盖率。
    返回 lit_id -> RelevanceScore 的映射。
    """
    by_id = {
        str(d.get("lit_id", "")): d
        for d in decisions
        if isinstance(d, dict)
    }
    scores: dict[str, RelevanceScore] = {}
    for paper in papers:
        decision = by_id.get(paper.lit_id, {})
        score = RelevanceScore(
            lit_id=paper.lit_id,
            keyword_overlap=keyword_overlap(topic, _paper_text(paper), concept_groups),
            field_match=_coerce_score(decision.get("field_match")),
            method_applicability=_coerce_score(decision.get("method_applicability")),
            conclusion_value=_coerce_score(decision.get("conclusion_value")),
            reason=str(decision.get("reason", "")),
        )
        score.match_points = _match_points(topic, paper, score, concept_groups)
        scores[paper.lit_id] = score
    return scores


def sort_by_relevance(
    papers: list[Paper],
    scores: dict[str, RelevanceScore],
) -> list[Paper]:
    """按等级 + 总分降序排布,保证高相关文献优先进入配额与正文。"""
    def _key(paper: Paper) -> tuple[int, float]:
        score = scores.get(paper.lit_id)
        if score is None:
            return (GRADE_ORDER["medium"], 0.0)
        return (GRADE_ORDER[score.grade], -score.total)

    return sorted(papers, key=_key)


def build_relevance_report(
    papers: list[Paper],
    scores: dict[str, RelevanceScore],
) -> dict:
    """生成《文献相关性分级清单》结构化数据。"""
    ordered = sort_by_relevance(papers, scores)
    titles = {p.lit_id: p.title for p in papers}
    items = [
        scores[p.lit_id].to_row(titles.get(p.lit_id, ""))
        for p in ordered
        if p.lit_id in scores
    ]
    summary = {"high": 0, "medium": 0, "low": 0}
    for row in items:
        summary[row["grade"]] += 1
    return {
        "criteria": {
            "keyword_overlap_high": KEYWORD_OVERLAP_HIGH_THRESHOLD,
            "field_match_full": FIELD_MATCH_FULL_THRESHOLD,
            "method_applicable": METHOD_APPLICABLE_THRESHOLD,
            "weights": dict(_WEIGHTS),
        },
        "summary": summary,
        "total": len(items),
        "items": items,
    }
