"""章节 LLM 写作:带引用强校验,拒绝幻觉。

使用 prompts/literature-review-section.md 模板作为 system prompt。
核心约束:
- LLM 只能用 [lit_xxx] 形式引用输入清单里存在的 lit_id
- 生成的文本中出现的任何 [lit_xxx] 都必须在允许集合内
- 幻觉引用会被从正文中剥离并记录警告
- 每篇文献在同一章里只出现一次
- "comment" 章节不引用任何文献(literature-review skill 强制规则)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from src.llm.client import messages_create
from src.retrieval.types import Paper
from src.writing.classifier import Group
from src.writing.templates import SectionSpec
from prompts.service import render

# 正文中 [lit_xxx] 的匹配
CITE_RE = re.compile(r"\[(lit_[0-9a-f]{16})\]")


@dataclass
class SectionResult:
    """单章写作结果。"""

    key: str
    title: str
    content: str
    citations: list[str] = field(default_factory=list)
    dropped_citations: list[str] = field(default_factory=list)


def _build_papers_catalog(papers: list[Paper]) -> str:
    """论文清单(标题/作者/年份/期刊)。"""
    lines = []
    for p in papers:
        lines.append(
            f"- {p.lit_id} | {p.title} | {', '.join(p.authors)} | "
            f"{p.journal or 'N/A'} | {p.year or 'N/A'}"
        )
    return "\n".join(lines) if lines else "(本章无可引用文献)"


def _build_section_role(section: SectionSpec, groups: list[Group]) -> str:
    """把 SectionSpec 与分类上下文组合成一段自然语言描述。"""
    if section.key == "comment":
        return (
            "这是综述最后的『文献述评』部分(literature-review skill 强制规则):\n"
            "1. 综合评述国内外(各主题)研究的共识、分歧、研究方法的优势与局限\n"
            "2. 明确指出 Research Gap,引出本研究的问题与设计\n"
            "3. 不得引用任何文献 — 不出现 [lit_xxx],不出现『作者(年份)』夹注\n"
            "4. 提及前述观点时,只作概括性表述(现有研究/多数学者/相关文献)"
        )
    group_names = ", ".join(g.name for g in groups)
    return (
        f"分类方式下的分组:{group_names}。本节聚焦于本节具体主题,"
        f"遵循 literature-review skill 的批判性写作原则:"
        f"禁止简单罗列、必须包含比较与对比、识别共识与争议。"
    )


def write_section(
    section: SectionSpec,
    topic: str,
    groups: list[Group],
    papers: list[Paper],
    model: str | None = None,
) -> SectionResult:
    """写一章。papers 是本章允许引用的全集。"""
    allowed = {p.lit_id for p in papers}
    is_comment = section.key == "comment"

    system = render(
        "literature-review-section",
        topic=topic,
        section_key=section.key,
        section_title=section.title,
        section_role=_build_section_role(section, groups),
        papers_catalog=_build_papers_catalog([] if is_comment else papers),
        available_lit_ids="\n".join(sorted(allowed)) if allowed and not is_comment else "(无,本节不引用任何文献)",
        humanize=True,
    )
    user = (
        f"研究主题:{topic}\n\n"
        f"章节:{section.title}\n\n"
        f"分组概览:{', '.join(g.name for g in groups) if groups else '无分组'}\n\n"
        f"请输出本章节正文。"
    )

    content = messages_create(
        system=system,
        user=user,
        max_tokens=4000,
        model=model,
    )

    # 校验引用: 落入 allowed 的保留并去重(同章内同 lit_id 只保留首次出现),其余剥离
    citations: list[str] = []
    dropped: list[str] = []

    def _strip(m: re.Match[str]) -> str:
        lit_id = m.group(1)
        if is_comment or lit_id not in allowed:
            dropped.append(lit_id)
            return ""
        # 正文中保留所有出现,只在 citations 列表里去重(供 orchestrator 跨章去重判断)
        if lit_id not in citations:
            citations.append(lit_id)
        return m.group(0)

    cleaned = CITE_RE.sub(_strip, content)

    return SectionResult(
        key=section.key,
        title=section.title,
        content=cleaned,
        citations=citations,
        dropped_citations=dropped,
    )