"""核查:正文夹注的作者表达与文献实际作者数一致性(v9.10)。

规范要求(GB/T 7714 著者表意 + 写作 prompt 硬约束):
- 单作者:只写姓名("张三(2024)" / "Smith(2024)")
- 两作者:中文用"和"、英文用"and"("张三和李四(2024)" / "Smith and Jones(2024)")
- 三人及以上:中文用"等"、英文用"et al."("王五等(2024)" / "Smith et al.(2024)")

此前的校验只保证"夹注能唯一命中池内文献",不校验作者表达方式;
LLM 会给两人文献写"等"、给三人文献只写第一作者,读者据此判断
文献规模会被误导。本核查把"表达 vs 真实作者数"的偏差显式暴露。

同时附带池级同名同年预警:同一第一作者同年多篇时,正文夹注
("张三(2023)")无法唯一区分,属于结构性引用风险,应提前暴露。
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
# 夹注:中文名(2-4 字)可选"和/与"连接第二作者,可选"等";英文姓氏 + et al./and
_CITE_CN = r"[一-鿿]{2,4}(?:\s*(?:和|与|、|,|，)\s*[一-鿿]{2,4})*(?:等)?"
_CITE_EN = r"[A-Z][A-Za-z\-]+(?:\s+(?:et\s+al\.|and\s+[A-Z][A-Za-z\-]+))?"
_AUTHOR_YEAR_RE = re.compile(
    "(?P<authors>(?:" + _CITE_CN + ")|(?:" + _CITE_EN + "))"
    + r"\s*[（(]\s*(?P<year>(?:19|20)\d{2})\s*[）)]",
)
# 夹注内的作者连接形态
_ET_AL_RE = re.compile(r"等\s*$|et\s+al\.?\s*$", re.IGNORECASE)
_AND_RE = re.compile(r"(?:和|与|、|,|，)\s*[一-鿿]|[A-Za-z\-]\s+and\s+[A-Za-z]")


def _first_author_key(paper: Paper) -> str:
    """第一作者的消歧键:中文取全名去非汉字,英文取姓氏小写。"""
    authors = getattr(paper, "authors", None) or []
    if not authors:
        return ""
    first = str(authors[0] or "")
    compact = re.sub(r"[^\u4e00-\u9fffA-Za-z]", "", first).lower()
    return compact


def check_same_author_year(papers: list[Paper]) -> list[QAIssue]:
    """池级同名同年预警:同一第一作者同年 ≥2 篇时,夹注无法唯一区分。"""
    issues: list[QAIssue] = []
    buckets: dict[tuple[str, int], list[Paper]] = {}
    for paper in papers:
        key = (_first_author_key(paper), int(paper.year or 0))
        if not key[0] or not key[1]:
            continue
        buckets.setdefault(key, []).append(paper)
    for (author_key, year), group in sorted(buckets.items()):
        if len(group) < 2:
            continue
        titles = " / ".join((p.title or "")[:24] for p in group[:3])
        issues.append(QAIssue(
            "ACCURACY_SAME_AUTHOR_YEAR", QACheckStatus.WARN,
            "authors", f"paper/{group[0].lit_id}",
            f"同一第一作者 {author_key} 同年({year})有 {len(group)} 篇入选,"
            f"正文夹注无法唯一区分,请人工核对引用指向({titles})",
            lit_id=group[0].lit_id,
        ))
    return issues


def check_author_et_al(
    sections: Iterable["SectionResult"],
    papers: list[Paper],
) -> QACheckResult:
    """核查正文夹注的作者表达(等/和/et al./and)与文献真实作者数一致。

    纯规则、零 LLM;偏差只告警不阻断(著录惯例存在合理变体,
    由作者在确认面板人工复核)。
    """
    result = QACheckResult(
        check_id="author_style_001",
        name="夹注作者表达一致性核查",
        status=QACheckStatus.PASS,
        metrics={"sections": 0},
    )
    # 编号 → lit_id(与 binding/claims 同一套顺序协议)
    cited_ids: list[str] = []
    for section in sections:
        for cid in section.citations or []:
            if cid and cid not in cited_ids:
                cited_ids.append(cid)
    number_to_lit = {i + 1: cid for i, cid in enumerate(cited_ids)}
    by_id = {p.lit_id: p for p in papers}

    checked = 0
    mismatches = 0
    for section in sections:
        if section.key == "comment":
            continue
        text = section.content or ""
        result.metrics["sections"] = result.metrics.get("sections", 0) + 1
        for sentence in _SENTENCE_SPLIT_RE.split(text):
            anchors = list(_NUMERIC_ANCHOR_RE.finditer(sentence))
            if not anchors:
                continue
            for anchor in anchors:
                lit_id = number_to_lit.get(int(anchor.group(1)))
                paper = by_id.get(lit_id) if lit_id else None
                if paper is None:
                    continue
                # 锚点前 80 字符内找夹注(与 Kimi 实践一致:近邻夹注才算同处引用)
                window_start = max(0, anchor.start() - 80)
                window = sentence[window_start:anchor.start()]
                mention = None
                for match in _AUTHOR_YEAR_RE.finditer(window):
                    mention = match
                if mention is None:
                    continue
                author_part = mention.group("authors")
                author_count = len(getattr(paper, "authors", None) or [])
                if author_count <= 0:
                    continue
                checked += 1
                is_et_al = bool(_ET_AL_RE.search(author_part))
                is_and = bool(_AND_RE.search(author_part))
                expect = (
                    "et_al" if author_count >= 3
                    else "and" if author_count == 2
                    else "single"
                )
                actual = "et_al" if is_et_al else "and" if is_and else "single"
                if expect != actual:
                    mismatches += 1
                    result.issues.append(QAIssue(
                        "AUTHOR_STYLE_MISMATCH", QACheckStatus.WARN,
                        "content", f"section:{section.key}",
                        f"夹注作者表达与实际作者数不符: 文献 {author_count} 位作者,"
                        f"夹注用了「{author_part.strip()}」"
                        f"(期望 {expect});year={mention.group('year')}",
                        lit_id=lit_id,
                        section_key=section.key,
                        snippet=sentence[max(0, mention.start() - 20):anchor.end()][:160],
                    ))

    result.metrics.update({
        "citations_checked": checked,
        "style_mismatches": mismatches,
    })
    result.status = QACheckStatus.WARN if mismatches else QACheckStatus.PASS
    result.notes.append(
        f"核查 {checked} 处夹注;作者表达与实际作者数不一致 {mismatches} 处"
        if mismatches else "全部夹注的作者表达与实际作者数一致"
    )
    log.info(
        "author style check: checked=%d mismatch=%d status=%s",
        checked, mismatches, result.status.value,
    )
    return result
