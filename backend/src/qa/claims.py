"""论断-证据审计(v9.9 新增,方案 M3)。

把正文解析为逐句论断(claim),标注支撑文献与风险:
- 单一文献支撑的关键判断(强断言 + 仅 1 篇支撑)→ WARN;
- 低严谨性文献(method_rigor < 60)单独支撑关键判断 → WARN。

纯规则、无 LLM,默认启用;产出进入 QA 报告的"引用审计表"小节,
对应参考方案 §12 第六步 / §13 审计表。
"""
from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Iterable

from qa.models import QACheckResult, QAIssue
from qa.rules import QACheckStatus
from retrieval.types import Paper

if TYPE_CHECKING:
    from writing.orchestrator import SectionResult  # pragma: no cover

log = logging.getLogger(__name__)

_NUMERIC_ANCHOR_RE = re.compile(r"\[(\d{1,3})\]")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？!?])|\n")
# 强断言标记:出现这些词的论断被认定为"关键判断"
_KEY_CLAIM_MARKERS = (
    "证明", "证实", "首次", "显著", "关键", "核心", "决定", "普遍认为",
    "一致", "明确", "重要", "根本", "必然",
)
# method_rigor 低于该值的文献不得单独支撑关键判断
_LOW_RIGOR_THRESHOLD = 60.0


def _sentence_spans(text: str) -> list[tuple[int, int]]:
    """按句号/问号/感叹号/换行切句,返回 (start, end) 区间。"""
    spans: list[tuple[int, int]] = []
    start = 0
    for m in _SENTENCE_SPLIT_RE.finditer(text):
        end = m.end()
        if end > start:
            spans.append((start, end))
        start = end
    if start < len(text):
        spans.append((start, len(text)))
    return spans


def extract_claims(
    sections: Iterable["SectionResult"],
    papers: list[Paper],
) -> list[dict]:
    """把各章正文解析为论断列表。

    编号→lit_id 的映射与 qa.binding 同一套规则:section.citations 的
    出现顺序即全局编号顺序。
    """
    cited_ids_in_order: list[str] = []
    for section in sections:
        for cid in section.citations or []:
            if cid and cid not in cited_ids_in_order:
                cited_ids_in_order.append(cid)
    number_to_lit = {i + 1: cid for i, cid in enumerate(cited_ids_in_order)}

    claims: list[dict] = []
    for section in sections:
        text = section.content or ""
        for start, end in _sentence_spans(text):
            sentence = text[start:end]
            numbers = [int(n) for n in _NUMERIC_ANCHOR_RE.findall(sentence)]
            lit_ids = list(dict.fromkeys(
                number_to_lit[n] for n in numbers if n in number_to_lit
            ))
            if not lit_ids:
                continue
            claims.append({
                "section_key": section.key,
                "sentence": sentence.strip()[:500],
                "supporting_lit_ids": lit_ids,
                "is_key_claim": any(marker in sentence for marker in _KEY_CLAIM_MARKERS),
            })
    return claims


def check_claim_audit(
    sections: list["SectionResult"],
    papers: list[Paper],
    *,
    rigor_map: dict[str, float] | None = None,
) -> QACheckResult:
    """论断-证据审计入口。只产 WARN,不阻断交付(审计是建议不是门禁)。"""
    result = QACheckResult(
        check_id="claim_audit_001",
        name="论断-证据审计",
        status=QACheckStatus.PASS,
        metrics={"sections": len(sections)},
    )
    claims = extract_claims(sections, papers)
    by_id = {p.lit_id: p for p in papers}
    title_of = {
        lit_id: (p.title or "")[:40]
        for lit_id, p in by_id.items()
    }

    single_source = [c for c in claims if len(c["supporting_lit_ids"]) == 1]
    single_key = [c for c in single_source if c["is_key_claim"]]
    low_rigor_single = []
    if rigor_map:
        for claim in single_key:
            lit_id = claim["supporting_lit_ids"][0]
            rigor = rigor_map.get(lit_id)
            if rigor is not None and rigor < _LOW_RIGOR_THRESHOLD:
                low_rigor_single.append(claim)

    for claim in single_key[:20]:
        lit_id = claim["supporting_lit_ids"][0]
        result.issues.append(QAIssue(
            "CLAIM_SINGLE_SOURCE_KEY", QACheckStatus.WARN,
            "content", f"section:{claim['section_key']}",
            "关键判断仅由单一文献支撑,建议补充同主题文献或弱化断言",
            lit_id=lit_id,
            section_key=claim["section_key"],
            snippet=claim["sentence"][:160],
        ))
    for claim in low_rigor_single[:10]:
        lit_id = claim["supporting_lit_ids"][0]
        result.issues.append(QAIssue(
            "CLAIM_LOW_RIGOR_SUPPORT", QACheckStatus.WARN,
            "content", f"section:{claim['section_key']}",
            "低方法严谨性文献单独支撑关键判断,可信度存疑",
            lit_id=lit_id,
            section_key=claim["section_key"],
            snippet=claim["sentence"][:160],
        ))

    result.metrics.update({
        "claims_total": len(claims),
        "key_claims": sum(1 for c in claims if c["is_key_claim"]),
        "single_source_claims": len(single_source),
        "single_source_key_claims": len(single_key),
        "low_rigor_single_support": len(low_rigor_single),
        "audit_table_rows": min(len(claims), 200),
    })
    warned = bool(result.issues)
    result.status = QACheckStatus.WARN if warned else QACheckStatus.PASS
    result.notes.append(
        f"共解析 {len(claims)} 条论断;"
        + (
            f"单一文献支撑的关键判断 {len(single_key)} 处"
            + (f",其中 {len(low_rigor_single)} 处支撑文献严谨性偏低" if low_rigor_single else "")
            if warned else "无单一文献支撑的关键判断"
        )
    )
    log.info(
        "claim audit: claims=%d key=%d single=%d single_key=%d status=%s",
        len(claims), sum(1 for c in claims if c["is_key_claim"]),
        len(single_source), len(single_key), result.status.value,
    )
    return result


__all__ = ["extract_claims", "check_claim_audit"]
