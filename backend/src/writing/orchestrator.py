"""综述写作总控:筛选 → 分类 → 分章写作 → 汇总引文清单。

提供两种入口:
- generate_review:一次性返回完整 ReviewResult(向后兼容,测试用)
- generate_review_stream:生成器,按事件 yield 进度(供 SSE 流式接口)
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Generator

from retrieval.types import Paper, Source
from screening.llm_filter import screen_batch
from writing.classifier import Group, classify
from writing.section_writer import SectionResult, write_section, write_section_stream
from writing.settings import (
    SECTION_COMMENT_INSTRUCTION,
    SECTION_COMMENT_TITLE,
    SECTION_LOCALE_INSTRUCTION_TEMPLATE,
    SECTION_THEME_INSTRUCTION_TEMPLATE,
)
from writing.templates import SectionSpec

log = logging.getLogger(__name__)


_CHINESE_NUMBERS = ["一", "二", "三", "四", "五", "六", "七", "八", "九", "十"]


def _format_chinese_index(idx: int) -> str:
    """将 1-based 索引格式化为中文序数(超出十则回退到阿拉伯数字)。"""
    if 1 <= idx <= len(_CHINESE_NUMBERS):
        return _CHINESE_NUMBERS[idx - 1]
    return str(idx)


def build_review_sections(classify_mode: str, groups: list[Group]) -> list[SectionSpec]:
    """根据分类结果构造综述章节。"""
    instruction_template = (
        SECTION_THEME_INSTRUCTION_TEMPLATE
        if classify_mode == "theme"
        else SECTION_LOCALE_INSTRUCTION_TEMPLATE
    )
    sections: list[SectionSpec] = []
    for idx, group in enumerate(groups, start=1):
        sections.append(
            SectionSpec(
                key=f"theme_{idx}",
                title=f"{_format_chinese_index(idx)}、{group.name}",
                instruction=instruction_template.format(name=group.name),
            )
        )
    sections.append(
        SectionSpec(
            key="comment",
            title=(
                f"{_format_chinese_index(len(sections) + 1)}、"
                f"{SECTION_COMMENT_TITLE}"
            ),
            instruction=SECTION_COMMENT_INSTRUCTION,
        )
    )
    return sections


def _papers_for_section(
    spec: SectionSpec,
    groups: list[Group],
    papers: list[Paper],
) -> list[Paper]:
    if spec.key == "comment":
        return papers
    group_index = int(spec.key.removeprefix("theme_")) - 1
    allowed_ids = set(groups[group_index].lit_ids)
    return [paper for paper in papers if paper.lit_id in allowed_ids]


@dataclass
class ReviewResult:
    """一次综述生成的完整结果。"""

    topic: str
    classify_mode: str
    groups: list[Group]
    sections: list[SectionResult]
    screened_out_ids: list[str] = field(default_factory=list)
    dropped_citations: list[str] = field(default_factory=list)


def _sse_event(event: str, data: Any) -> str:
    """格式化为 SSE data 行。"""
    payload = json.dumps({"event": event, "data": data}, ensure_ascii=False)
    return f"data: {payload}\n\n"


def generate_review_stream(
    topic: str,
    papers: list[Paper],
    classify_mode: str,
    do_screening: bool = True,
) -> Generator[str, None, None]:
    """流式生成综述,按事件 yield SSE 字符串。"""
    try:
        yield _sse_event("start", {
            "topic": topic,
            "total_papers": len(papers),
            "classify_mode": classify_mode,
        })

        screened_out: list[str] = []
        if do_screening and papers:
            yield _sse_event("screening_started", {"total": len(papers)})
            decisions = screen_batch(papers, topic)
            kept_ids = {d["lit_id"] for d in decisions if d.get("relevant", True)}
            screened_out = [p.lit_id for p in papers if p.lit_id not in kept_ids]
            papers = [p for p in papers if p.lit_id in kept_ids]
            yield _sse_event("screening_done", {
                "kept": len(papers),
                "screened_out": screened_out,
            })

        yield _sse_event("classify_started", {
            "classify_mode": classify_mode,
            "total": len(papers),
        })
        groups = classify(papers, topic, classify_mode)
        yield _sse_event("classify_done", {
            "groups": [{"name": g.name, "lit_ids": g.lit_ids} for g in groups],
        })

        section_specs = build_review_sections(classify_mode, groups)
        sections: list[SectionResult] = []
        all_dropped: list[str] = []
        for idx, spec in enumerate(section_specs):
            section_papers = _papers_for_section(spec, groups, papers)
            yield _sse_event("section_preparing", {
                "index": idx,
                "total": len(section_specs),
                "key": spec.key,
                "title": spec.title,
                "message": f"正在准备《{spec.title}》的上下文与引用约束...",
            })
            yield _sse_event("section_started", {
                "index": idx,
                "total": len(section_specs),
                "key": spec.key,
                "title": spec.title,
            })
            total_chars = 0
            for piece, done, res in write_section_stream(
                spec, topic, groups, section_papers,
            ):
                if piece:
                    total_chars += len(piece)
                    yield _sse_event("section_token", {
                        "index": idx,
                        "total": len(section_specs),
                        "key": spec.key,
                        "title": spec.title,
                        "delta": piece,
                        "chars": total_chars,
                    })
                if done and res is not None:
                    sections.append(res)
                    all_dropped.extend(res.dropped_citations)
                    yield _sse_event("section_done", {
                        "index": idx,
                        "total": len(section_specs),
                        "key": res.key,
                        "title": res.title,
                        "content": res.content,
                        "citations": res.citations,
                        "dropped_citations": res.dropped_citations,
                    })

        cited_ids = {cid for s in sections for cid in s.citations}
        cited_papers = [p for p in papers if p.lit_id in cited_ids]
        yield _sse_event("reference_started", {"count": len(cited_papers)})
        ref = render_reference_list(cited_papers)
        yield _sse_event("reference_list", {"reference_list": ref})

        yield _sse_event("complete", {
            "screened_out_ids": screened_out,
            "dropped_citations": all_dropped,
        })

    except Exception as e:
        log.exception("generate_review_stream 失败")
        yield _sse_event("error", {"message": str(e)})


def generate_review(
    topic: str,
    papers: list[Paper],
    classify_mode: str,
    do_screening: bool = True,
) -> ReviewResult:
    """一次性生成完整综述(测试/向后兼容用)。"""
    if classify_mode not in ("locale", "theme"):
        raise ValueError(f"unknown classify_mode: {classify_mode}")

    screened_out: list[str] = []
    if do_screening and papers:
        decisions = screen_batch(papers, topic)
        kept_ids = {d["lit_id"] for d in decisions if d.get("relevant", True)}
        screened_out = [p.lit_id for p in papers if p.lit_id not in kept_ids]
        papers = [p for p in papers if p.lit_id in kept_ids]

    groups = classify(papers, topic, classify_mode)

    section_specs = build_review_sections(classify_mode, groups)
    sections: list[SectionResult] = []
    all_dropped: list[str] = []
    for spec in section_specs:
        res = write_section(
            spec,
            topic,
            groups,
            _papers_for_section(spec, groups, papers),
        )
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
    """生成参考文献列表(中文用原文,英文用平台元数据)。"""
    lines = []
    for p in papers:
        if p.source == Source.USER_IMPORTED and p.raw_citation:
            lines.append(p.raw_citation)
            continue

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
