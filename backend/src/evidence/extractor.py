"""逐篇文献的 LLM 结构化证据抽取(方案 M2)。

输入: 写作文献池(题名+摘要)
输出: lit_id -> 证据 dict,字段:
  research_question / theory / method_design / sample /
  findings[] / limitations[] / key_variables[] / polarity

- 批量抽取(默认每批 8 篇),MiniMax 单次 JSON 输出;
- JSON 截断走抢救路径(与 writing.relevance._salvage_truncated_concepts 同思路);
- 单批失败重试 1 次后仍失败 -> 该批降级为占位证据,不阻塞主流程;
- 摘要未报告的字段一律 null / "unclear",与参考方案 §4 "原文未明确报告"对齐。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Iterable

from retrieval.types import Paper

log = logging.getLogger(__name__)

EXTRACTOR_VERSION = "v1"

# 结论极性枚举(synthesis 与结论比较表的原料)
POLARITY_VALUES = (
    "positive",         # 正向关系/促进作用
    "negative",         # 负向关系/抑制作用
    "nonlinear",        # 非线性关系
    "non_significant",  # 不显著
    "mixed",            # 同一文献内结论分化(如对 X 正向、对 Y 负向)
    "unclear",          # 摘要未明确报告
)

_BATCH_SIZE = 8
_ABSTRACT_CHARS = 1500
_MAX_TOKENS = 6000
_TIMEOUT = 90.0

_SYSTEM = """你是学术文献证据抽取助手。任务:从每篇论文的题名与摘要中抽取结构化证据,供后续文献综述的主题综合使用。

对每篇论文输出以下字段:

- research_question: 该文研究的核心问题(一句话);摘要未明确则为 null
- theory: 使用的理论/框架名称(如"工作要求-资源模型");未提及则为 null
- method_design: 研究设计类型,只能取: survey(问卷/横截面) / experiment(实验) /
  longitudinal(纵向/面板) / qualitative(访谈/案例等质性) / secondary(二手数据/元分析) /
  unclear(摘要未交代)
- sample: 样本或数据的简要描述(如"386名中国企业员工");未报告则为 null
- findings: 核心发现,1-3 条,每条一句话;摘要无实质发现则为空数组
- limitations: 作者自述或摘要可见的局限,0-3 条;未提及则为空数组
- key_variables: 核心变量/概念名,2-6 个(如"AI使用"、"技术焦虑"、"创新行为")
- polarity: 核心变量间主要关系的极性,只能取:
  positive(正向) / negative(负向) / nonlinear(非线性) / non_significant(不显著) /
  mixed(结论分化,如对A正向对B负向) / unclear(未明确)

硬性规则:
1. 只能使用摘要与题名中明确出现的信息,禁止凭常识或领域知识补全。
2. 摘要没写的信息一律 null / 空数组 / "unclear",这比编造更正确。
3. findings 不得包含摘要中没有的数字或因果强度表述。

严格输出 JSON 对象(不要 markdown 代码块,不要解释):
{"items": [
  {"lit_id": "<原文lit_id>", "research_question": "...|null", "theory": "...|null",
   "method_design": "survey|...", "sample": "...|null",
   "findings": ["..."], "limitations": ["..."],
   "key_variables": ["..."], "polarity": "positive|..."}
]}
必须为输入中的每一个 lit_id 输出且只能输出一次。"""


class EvidenceExtractionError(RuntimeError):
    """单批证据抽取失败(调用方按批降级)。"""


def _salvage_truncated_items(raw: str) -> list[dict] | None:
    """从被 max_tokens 截断的 items 数组里抢救已完整闭合的对象。

    MiniMax 思考内容与正文共享预算,JSON 常在数组中途截断;
    丢弃残缺的最后一个对象,补全括号后重解析。
    """
    start = raw.find("{")
    if start == -1:
        return None
    marker = raw.find('"items"', start)
    if marker == -1:
        return None
    arr = raw.find("[", marker)
    if arr == -1:
        return None
    i, n = arr + 1, len(raw)
    last_obj_end = -1
    while i < n:
        while i < n and raw[i] in " \t\r\n,":
            i += 1
        if i >= n or raw[i] != "{":
            break
        depth, in_str, esc, j = 0, False, False, i
        while j < n:
            ch = raw[j]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        break
            j += 1
        if j >= n:
            break
        last_obj_end = j
        i = j + 1
    if last_obj_end == -1:
        return None
    repaired = raw[start:last_obj_end + 1] + "]}"
    try:
        data = json.loads(repaired)
    except json.JSONDecodeError:
        return None
    items = data.get("items") if isinstance(data, dict) else None
    return items if isinstance(items, list) else None


def _parse_items(raw: str) -> list[dict]:
    cleaned = re.sub(r"```(?:json)?|```", "", raw or "").strip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        items = _salvage_truncated_items(cleaned)
        if items is None:
            raise EvidenceExtractionError("证据抽取结果不是合法 JSON 且无法抢救")
        return items
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        items = data.get("items")
        if isinstance(items, list):
            return [x for x in items if isinstance(x, dict)]
    raise EvidenceExtractionError("证据抽取结果缺少 items 数组")


def _clean_str(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or text.lower() in {"null", "none", "n/a", "unknown", "未报告", "未提及"}:
        return None
    return text[:300]


def _clean_list(value: object, limit: int = 3) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        text = _clean_str(item)
        if text:
            out.append(text)
        if len(out) >= limit:
            break
    return out


def _normalize_polarity(value: object) -> str:
    text = str(value or "").strip().lower()
    if text in POLARITY_VALUES:
        return text
    return "unclear"


def _normalize_item(item: dict, valid_ids: set[str]) -> tuple[str, dict] | None:
    lit_id = str(item.get("lit_id", "")).strip()
    if lit_id not in valid_ids:
        log.warning("证据抽取返回未知 lit_id,已丢弃: %s", lit_id[:40])
        return None
    evidence = {
        "research_question": _clean_str(item.get("research_question")),
        "theory": _clean_str(item.get("theory")),
        "method_design": _clean_str(item.get("method_design")) or "unclear",
        "sample": _clean_str(item.get("sample")),
        "findings": _clean_list(item.get("findings")),
        "limitations": _clean_list(item.get("limitations")),
        "key_variables": _clean_list(item.get("key_variables"), limit=6),
        "polarity": _normalize_polarity(item.get("polarity")),
    }
    if evidence["method_design"] not in {
        "survey", "experiment", "longitudinal", "qualitative", "secondary", "unclear",
    }:
        evidence["method_design"] = "unclear"
    return lit_id, evidence


def fallback_evidence(paper: Paper) -> dict:
    """LLM 抽取失败时的占位证据:只保留诚实空值,绝不伪造内容。"""
    return {
        "research_question": None,
        "theory": None,
        "method_design": "unclear",
        "sample": None,
        "findings": [],
        "limitations": [],
        "key_variables": [],
        "polarity": "unclear",
        "extracted": False,
    }


def _extract_batch_once(papers: list[Paper], topic: str) -> list[dict]:
    payload = [
        {
            "lit_id": p.lit_id,
            "title": p.title,
            "abstract": (p.abstract or "")[:_ABSTRACT_CHARS],
        }
        for p in papers
    ]
    user = (
        f"研究主题:{topic}\n"
        f"论文清单:\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n"
        "请输出严格 JSON 对象,格式为 {\"items\":[...]},覆盖全部论文。"
    )
    from llm.client import messages_create
    raw = messages_create(
        _SYSTEM,
        user,
        max_tokens=_MAX_TOKENS,
        max_retries=0,
        timeout=_TIMEOUT,
        response_format={"type": "json_object"},
    )
    return _parse_items(raw)


def _extract_batch(papers: list[Paper], topic: str) -> dict[str, dict]:
    valid_ids = {p.lit_id for p in papers}
    last_error: Exception | None = None
    for attempt in (1, 2):
        try:
            items = _extract_batch_once(papers, topic)
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            log.warning("证据抽取批次失败(第 %d 次, %d 篇): %s", attempt, len(papers), exc)
            continue
        normalized: dict[str, dict] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            pair = _normalize_item(item, valid_ids)
            if pair is not None:
                lit_id, evidence = pair
                normalized.setdefault(lit_id, evidence)
        if len(normalized) >= max(1, len(papers) // 2):
            # 覆盖过半即可交付,缺的部分由 fallback_evidence 兜底;
            # 严格全量校验会放大 MiniMax 偶发漏答的失败率,不值得。
            return normalized
        last_error = EvidenceExtractionError(
            f"证据抽取覆盖不足({len(normalized)}/{len(papers)})"
        )
    if last_error is not None:
        raise last_error
    raise EvidenceExtractionError("证据抽取未产生结果")


def extract_pool_evidence(
    papers: Iterable[Paper],
    topic: str,
    *,
    batch_size: int = _BATCH_SIZE,
) -> dict[str, dict]:
    """全池证据抽取入口。单批失败降级为占位证据,整体不抛错。

    返回 lit_id -> evidence dict;LLM 成功抽取的条目带 "extracted": True。
    """
    pool = list(papers)
    result: dict[str, dict] = {}
    for start in range(0, len(pool), max(1, batch_size)):
        batch = pool[start:start + max(1, batch_size)]
        try:
            batch_result = _extract_batch(batch, topic)
        except Exception as exc:  # noqa: BLE001
            log.warning("证据抽取批次降级为占位(%d 篇): %s", len(batch), exc)
            batch_result = {}
        for paper in batch:
            evidence = batch_result.get(paper.lit_id)
            if evidence is None:
                evidence = fallback_evidence(paper)
            evidence.setdefault("extracted", True)
            evidence["extractor_version"] = EXTRACTOR_VERSION
            result[paper.lit_id] = evidence
    return result


def is_cross_sectional_dominant(
    papers: list[Paper],
    evidence_map: dict[str, dict] | None,
) -> bool:
    """判断文献池是否以横截面问卷为主(因果 lint 的上下文输入)。

    无证据数据时返回 False(不启用上下文收紧,lint 仍按通用规则告警)。
    """
    if not evidence_map:
        return False
    known = [
        (e.get("method_design") or "unclear")
        for e in evidence_map.values()
        if e.get("extracted")
    ]
    if len(known) < 5:
        return False
    survey_like = sum(1 for d in known if d in {"survey", "unclear"})
    return survey_like / len(known) >= 0.7


__all__ = [
    "EXTRACTOR_VERSION",
    "POLARITY_VALUES",
    "EvidenceExtractionError",
    "extract_pool_evidence",
    "fallback_evidence",
    "is_cross_sectional_dominant",
]
