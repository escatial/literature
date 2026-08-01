"""综述写作总控:筛选 → 分类 → 分章写作 → 汇总引文清单。"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.retrieval.types import Paper, Source
from src.screening.llm_filter import screen_batch
from src.writing.classifier import Group, classify
from src.writing.section_writer import SectionResult, write_section
from src.writing.templates import SECTIONS


@dataclass
class ReviewResult:
    """一次综述生成的完整结果。"""

    topic: str
    classify_mode: str
    groups: list[Group]
    sections: list[SectionResult]
    screened_out_ids: list[str] = field(default_factory=list)
    dropped_citations: list[str] = field(default_factory=list)


def generate_review(
    topic: str,
    papers: list[Paper],
    classify_mode: str,  # "locale" | "theme"
    do_screening: bool = True,
) -> ReviewResult:
    """生成一篇完整文献综述。

    papers 必须已经由调用方准备好(通常来自前端 IndexedDB 的当前文献池)。
    """
    if classify_mode not in ("locale", "theme"):
        raise ValueError(f"unknown classify_mode: {classify_mode}")

    # 1) 主题不符筛选
    if do_screening and papers:
        kept = screen_batch(papers, topic)
        screened_out = [p.lit_id for p in papers if p not in kept]
        papers = kept
    else:
        screened_out = []

    # 2) 分类
    groups = classify(papers, topic, classify_mode)

    # 3) 分章写作
    sections: list[SectionResult] = []
    all_dropped: list[str] = []
    for spec in SECTIONS:
        res = write_section(spec, topic, groups, papers)
        sections.append(res)
        all_dropped.extend(res.dropped_citations)

    return ReviewResult(
        topic=topic,
        classify_mode=classify_mode,
        groups=groups,
        sections=sections,
        screened_out_ids=screened_out,
        dropped_citations=all_dropped,
    )


def render_reference_list(papers: list[Paper]) -> str:
    """生成参考文献列表(中文用原文,英文用平台元数据)。

    中文文献:用户导入时提供的 raw_citation 原文(GB/T 7714,来自知网)。
    英文文献:OpenAlex / CrossRef 的元数据按 GB/T 7714 期刊格式渲染。
    """
    lines = []
    for p in papers:
        if p.source == Source.USER_IMPORTED and p.raw_citation:
            lines.append(p.raw_citation)
            continue

        # 英文:作者. 题名[J]. 刊名, 年, 卷(期): 页码.
        parts: list[str] = []
        authors = ", ".join(p.authors) if p.authors else "Anon"
        parts.append(f"{authors}. {p.title}[J].")
        tail_bits = []
        if p.journal:
            tail_bits.append(p.journal)
        if p.year:
            tail_bits.append(str(p.year))
        vol_issue = ""
        if p.volume and p.issue:
            vol_issue = f"{p.volume}({p.issue})"
        elif p.volume:
            vol_issue = p.volume
        elif p.issue:
            vol_issue = f"({p.issue})"
        if vol_issue:
            tail_bits.append(vol_issue)
        tail = ", ".join(tail_bits)
        if p.pages:
            tail = f"{tail}: {p.pages}" if tail else p.pages
        if tail:
            parts.append(tail + ".")
        lines.append(" ".join(parts))
    return "\n".join(lines)
