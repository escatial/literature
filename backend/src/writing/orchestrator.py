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
from writing.section_writer import SectionResult, write_section
from writing.templates import SECTIONS, SectionSpec

log = logging.getLogger(__name__)


_CHINESE_NUMBERS = ["一", "二", "三", "四", "五", "六", "七", "八", "九", "十"]


def _format_chinese_index(idx: int) -> str:
    """将 1-based 索引格式化为中文序数(超出十则回退到阿拉伯数字)。

    避免章节数超过 10 时出现 IndexError。
    """
    if 1 <= idx <= len(_CHINESE_NUMBERS):
        return _CHINESE_NUMBERS[idx - 1]
    return str(idx)


def build_review_sections(classify_mode: str, groups: list[Group]) -> list[SectionSpec]:
    """根据分类结果构造综述章节。

    主题模式不写"文献检索方法",而是按文献题名/摘要归纳出的 3~5 个并列主题展开。
    """
    if classify_mode != "theme":
        return [s for s in SECTIONS if s.key != "method"]

    theme_groups = groups[:5]
    sections = [
        SectionSpec(
            key="introduction",
            title=f"{_format_chinese_index(1)}、引言",
            instruction="说明研究主题的背景、综述范围和主题划分逻辑。此处不写资料获取过程。",
        )
    ]
    for idx, group in enumerate(theme_groups, start=2):
        sections.append(
            SectionSpec(
                key=f"theme_{idx - 1}",
                title=f"{_format_chinese_index(idx)}、{group.name}",
                instruction=(
                    f"围绕『{group.name}』归纳相关文献的核心观点、共识、分歧与不足。"
                    "本节只讨论该并列主题,不要写数据库来源或筛选流程。"
                ),
            )
        )
    sections.append(
        SectionSpec(
            key="comment",
            title=f"{_format_chinese_index(len(sections) + 1)}、文献述评",
            instruction="综合评价上述主题研究,指出研究空白和本文切入点。此处不新增文献引用。",
        )
    )
    return sections


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
    """流式生成综述,按事件 yield SSE 字符串。

    事件序列:
    - start                开始,带文献数/分类方式
    - screening_started    LLM 筛选开始
    - screening_done       筛选完成,带剔除数
    - classify_done        分类完成,带分组
    - section_started      第 N 章开始
    - section_done         第 N 章完成,带正文+引用
    - reference_list       参考文献列表
    - complete             全部完成,带 dropped_citations
    - error                任何一步失败
    """
    try:
        yield _sse_event("start", {
            "topic": topic,
            "total_papers": len(papers),
            "classify_mode": classify_mode,
        })

        # 1) 筛选
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

        # 2) 分类
        groups = classify(papers, topic, classify_mode)
        yield _sse_event("classify_done", {
            "groups": [{"name": g.name, "lit_ids": g.lit_ids} for g in groups],
        })

        # 3) 分章写作
        section_specs = build_review_sections(classify_mode, groups)
        sections: list[SectionResult] = []
        all_dropped: list[str] = []
        for idx, spec in enumerate(section_specs):
            yield _sse_event("section_started", {
                "index": idx,
                "total": len(section_specs),
                "key": spec.key,
                "title": spec.title,
            })
            res = write_section(spec, topic, groups, papers)
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

        # 4) 参考文献列表(只纳入正文实际引用的)
        cited_ids = {cid for s in sections for cid in s.citations}
        cited_papers = [p for p in papers if p.lit_id in cited_ids]
        ref = render_reference_list(cited_papers)
        yield _sse_event("reference_list", {"reference_list": ref})

        # 5) 完成
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