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

import re as _re
from writing.citeproc_renderer import format_citation_via_citeproc as _citeproc_render
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


def _screen_papers(
    topic: str,
    papers: list[Paper],
    do_screening: bool,
) -> tuple[list[Paper], list[str]]:
    """执行写作前唯一允许的文献入口筛选。"""
    if not do_screening:
        raise ValueError("写作必须先完成文献筛选,不允许跳过筛选阶段")
    if not papers:
        return [], []

    decisions = screen_batch(papers, topic)
    kept_ids = {
        decision["lit_id"]
        for decision in decisions
        if decision.get("relevant") is True and decision.get("abstract_ok") is True
    }
    screened_out = [paper.lit_id for paper in papers if paper.lit_id not in kept_ids]
    kept_papers = [paper for paper in papers if paper.lit_id in kept_ids]
    if not kept_papers:
        raise ValueError("文献筛选后没有符合主题且摘要完整的文献")
    _enforce_chinese_source_mix(kept_papers)
    return kept_papers, screened_out


def _enforce_chinese_source_mix(papers: list[Paper]) -> None:
    """确保进入写作的筛选结果中,中文来源至少占三分之二。"""
    chinese_sources = {Source.CNKI, Source.USER_IMPORTED}
    chinese_count = sum(paper.source in chinese_sources for paper in papers)
    total_count = len(papers)
    if chinese_count * 3 < total_count * 2:
        raise ValueError(
            "筛选后中文文献占比不足三分之二: "
            f"中文 {chinese_count} 篇 / 总计 {total_count} 篇。"
            "请扩大中国知网检索范围后重新检索,不能用英文文献补足比例。"
        )


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

        yield _sse_event("screening_started", {"total": len(papers)})
        papers, screened_out = _screen_papers(topic, papers, do_screening)
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

        # 打通正文锚点与参考文献编号:分配全局编号、替换正文 [lit_xxx] -> [N]
        ref, _number_map = apply_citation_numbering(sections, papers)
        finalized_sections = [
            {
                "key": s.key,
                "title": s.title,
                "content": s.content,
                "citations": s.citations,
            }
            for s in sections
        ]
        yield _sse_event("reference_started", {"count": len(collect_cited_ids(sections))})
        yield _sse_event("sections_finalized", {"sections": finalized_sections})
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
    papers, screened_out = _screen_papers(topic, papers, do_screening)

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

    # 统一编号:正文 [lit_xxx] -> [N],与参考文献列表顺序一致(调用方渲染列表)
    apply_citation_numbering(sections, papers)

    return ReviewResult(
        topic=topic,
        classify_mode=classify_mode,
        groups=groups,
        sections=sections,
        screened_out_ids=screened_out,
        dropped_citations=all_dropped,
    )


_DIRTY_FRAGMENTS = (
    "查看该刊数据库收录来源",
    "查看该刊数据库收录。",
    "知网节选",
    "[知网节选]",
    "下载App",
    "在线阅读",
)
_CLEAN_RES = [_re.compile(_re.escape(s)) for s in _DIRTY_FRAGMENTS]
_BRACKET_RE = _re.compile(r"\[[^\]]{0,30}\]")
_SPACE_RE = _re.compile(r"\s{2,}")






def _clean_dirty(s: str | None) -> str:
    """过滤抓取时的脏数据片段(知网跳转文案、App 推广等)。

    重要:合法的 CSL 文献类型标识 [J] / [J/OL] / [M] / 访问日期 [YYYY-MM-DD]
    **不能剥**——这些是 GB/T 7714 的合法字段,不是脏数据。
    所以只剥 _CLEAN_RES 列出的已知脏片段,不做 blanket 剥 [xxx]。
    """
    if not s:
        return ""
    for pat in _CLEAN_RES:
        s = pat.sub("", s)
    s = _SPACE_RE.sub(" ", s).strip()
    s = s.rstrip(",;:")
    return s



def _format_one_gbt(p: Paper) -> str:
    """Return an official citation for a paper.

    Routing:
    - CNKI / USER_IMPORTED: 用户粘贴的 raw_citation(GB/T 7714 原文)
    - OPENALEX / PUBMED: citeproc-py 规则渲染

    兼顾中文手入库与英文 API 两种来源;无 raw_citation 时不构造,直接报错,
    避免虚假拼接。
    """
    if p.source in (Source.CNKI, Source.USER_IMPORTED):
        raw = _clean_dirty(getattr(p, "raw_citation", None))
        if not raw:
            raise ValueError(
                f"中文手工导入缺少 raw_citation: {p.lit_id} ({p.title})"
            )
        return raw.rstrip(".") + "."

    if p.source in (Source.OPENALEX, Source.PUBMED):
        rendered = _citeproc_render(p)
        if rendered:
            return rendered
        raise ValueError(
            f"GB/T 7714-2025 citeproc rendering failed: "
            f"source={p.source.value}, lit_id={p.lit_id}, title={p.title}"
        )

    raise ValueError(
        f"Unsupported citation source: source={p.source.value}, "
        f"lit_id={p.lit_id}, title={p.title}"
    )


_CITE_ANCHOR_RE = _re.compile(r"\[(lit_[a-zA-Z0-9_]+|hash:[a-zA-Z0-9_]+)\]")


def collect_cited_ids(sections: list[SectionResult]) -> list[str]:
    """按章节与引用出现顺序收集去重的 lit_id,作为参考文献顺序与编号依据。"""
    cited: list[str] = []
    for s in sections:
        for cid in s.citations:
            if cid not in cited:
                cited.append(cid)
    return cited


def replace_anchors_with_numbers(content: str, number_map: dict[str, int]) -> str:
    """把正文中的 [lit_xxx] 锚点替换为参考文献数字编号 [N]。

    number_map 之外的未知锚点(理论上已被幻觉剥离逻辑过滤)原样保留。
    """
    def _repl(m: _re.Match) -> str:
        token = m.group(1)
        return f"[{number_map.get(token, token)}]"
    return _CITE_ANCHOR_RE.sub(_repl, content or "")


def apply_citation_numbering(
    sections: list[SectionResult],
    papers: list[Paper],
) -> tuple[str, dict[str, int]]:
    """打通正文锚点与参考文献编号两套体系。

    - 按引用顺序分配全局编号 N(1, 2, 3, ...);
    - 把各章节正文中的 [lit_xxx] 原地替换为 [N];
    - 按同一顺序渲染参考文献列表(编号 [N] 连续)。

    返回 (reference_list, lit_id -> N 映射)。
    """
    cited_ids = collect_cited_ids(sections)
    number_map = {cid: i + 1 for i, cid in enumerate(cited_ids)}
    for s in sections:
        s.content = replace_anchors_with_numbers(s.content, number_map)
    # 参考文献必须与引用顺序一致,不能沿用 papers 原始顺序,否则编号错位
    by_id = {p.lit_id: p for p in papers}
    cited_papers = [by_id[cid] for cid in cited_ids if cid in by_id]
    return render_reference_list(cited_papers), number_map


def render_reference_list(papers: list[Paper]) -> str:
    """生成 GB/T 7714-2025 参考文献列表,按传入顺序统一编号 [N]。

    - papers 顺序即引用顺序(由 collect_cited_ids 保证去重与保序);
    - CNKI 使用 raw_citation,OpenAlex/PubMed 使用 citeproc 渲染条目;
    - 编号由本函数统一分配,不再依赖 citeproc 单条渲染(否则每条都是 [1])。
    """
    lines: list[str] = []
    for idx, p in enumerate(papers, start=1):
        lines.append(f"[{idx}] {_format_one_gbt(p)}")
    return "\n".join(lines)





