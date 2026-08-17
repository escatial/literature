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

import logging
import re
from dataclasses import dataclass, field
from typing import Generator

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnableLambda

from src.llm.client import messages_create, messages_stream, normalize_model_output
from src.retrieval.types import Paper
from src.writing.classifier import Group
from src.writing.templates import SectionSpec
from prompts.service import render

log = logging.getLogger(__name__)

CITE_RE = re.compile(r"\[(lit_[a-zA-Z0-9_]+|hash:[a-zA-Z0-9_]+)\]")


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


def _build_papers_catalog_alias(papers: list[Paper], alias_map: dict[str, str]) -> str:
    """带短 alias 的论文清单(给 LLM 用的版本)。"""
    lines = []
    real_to_alias = {real: alias for alias, real in alias_map.items()}
    for p in papers:
        alias = real_to_alias.get(p.lit_id, p.lit_id)
        lines.append(
            f"- {alias} | {p.title} | {', '.join(p.authors)} | "
            f"{p.journal or 'N/A'} | {p.year or 'N/A'}"
        )
    return "\n".join(lines) if lines else "(本章无可引用文献)"


def _build_abstract_catalog(
    papers: list[Paper],
    alias_map: dict[str, str],
    max_summary_chars: int = 400,
) -> str:
    """构造带摘要的文献池, 直接供 LLM 基于摘要写作。"""
    lines = []
    real_to_alias = {real: alias for alias, real in alias_map.items()}
    for p in papers:
        alias = real_to_alias.get(p.lit_id, p.lit_id)
        meta = (
            f"{alias} | {p.title} | {', '.join(p.authors)} | "
            f"{p.journal or 'N/A'} | {p.year or 'N/A'}"
        )
        if p.abstract:
            ab = p.abstract.strip()
            if len(ab) > max_summary_chars:
                ab = ab[:max_summary_chars] + "…"
            lines.append(f"- {meta}\n  摘要: {ab}")
        else:
            lines.append(f"- {meta}\n  摘要: (无摘要, 仅标题可参考)")
    return "\n".join(lines) if lines else "(本章无可引用文献)"


def _invoke_section_chain(
    *,
    system: str,
    user: str,
    model: str | None = None,
) -> str:
    """使用 LangChain 编排章节写作, 不接入检索器。"""
    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", "{system}"),
            ("human", "{user}"),
        ]
    )

    def _call_llm(payload: dict) -> str:
        messages = payload["messages"].to_messages()
        system_text = next(m.content for m in messages if m.type == "system")
        user_text = next(m.content for m in messages if m.type == "human")
        return messages_create(
            system=system_text,
            user=user_text,
            max_tokens=8000,
            model=payload.get("model"),
        )

    chain = (
        RunnableLambda(
            lambda payload: {
                "messages": prompt.invoke(
                    {
                        "system": payload["system"],
                        "user": payload["user"],
                    }
                ),
                "model": payload.get("model"),
            }
        )
        | RunnableLambda(_call_llm)
        | StrOutputParser()
    )
    return chain.invoke({"system": system, "user": user, "model": model})


def _build_section_role(
    section: SectionSpec,
    groups: list[Group],
    require_citation: bool,
    alias_map: dict[str, str],
) -> str:
    """把 SectionSpec 与分类上下文组合成一段自然语言描述。"""
    if section.key == "comment":
        return (
            "这是综述最后的『文献述评』部分(literature-review skill 强制规则):\n"
            "1. 综合评述国内外(各主题)研究的共识、分歧、研究方法的优势与局限\n"
            "2. 明确指出 Research Gap,引出本研究的问题与设计\n"
            "3. 不得引用任何文献 — 不出现 [lit_xxx],也不出现『作者(年份)』夹注\n"
            "4. 提及前述观点时,只作概括性表述(现有研究/多数学者/相关文献)"
        )
    group_names = ", ".join(g.name for g in groups)
    cite_rule = (
        "**必须**使用上面的 alias(形如 lit_oa_1 / lit_pm_2)进行内联引用,并在 alias 前加"
        "『作者（年份）』夹注;同一 alias 在本章只允许出现一次。"
        if require_citation
        else "本章不强求引用,如需佐证可用 alias。"
    )
    return (
        f"分类方式下的分组:{group_names}。本节定位:{section.instruction}\n"
        f"引用要求:{cite_rule}\n"
        "遵循 literature-review skill 的批判性写作原则:"
        "禁止简单罗列、必须包含比较与对比、识别共识与争议。"
    )


def _prepare_section_context(
    section: SectionSpec,
    topic: str,
    groups: list[Group],
    papers: list[Paper],
) -> tuple[str, str, set[str], dict[str, str], bool]:
    allowed = {p.lit_id for p in papers}
    is_comment = section.key == "comment"
    require_citation = (not is_comment) and bool(allowed) and section.key not in (
        "introduction", "method", "conclusion",
    )

    alias_map: dict[str, str] = {}
    short_list: list[Paper] = []
    if not is_comment and papers:
        for idx, p in enumerate(papers, start=1):
            prefix = {
                "openalex": "oa",
                "pubmed": "pm",
                "cnki": "cn",
                "wanfang": "wf",
                "cqvip": "cq",
                "crossref": "cr",
                "user_imported": "ui",
            }.get(p.source.value, "x")
            alias = f"lit_{prefix}_{idx}"
            alias_map[alias] = p.lit_id
            short_list.append(p)

    if is_comment:
        role_hint = "【本章不引用任何文献 — 见下方规则】"
    elif require_citation:
        role_hint = "【本章必须使用下方 alias 短标记引用文献 — 见下方规则】"
    else:
        role_hint = "【本章不强制引用 — 见下方规则】"

    catalog = "(本章无可引用文献)"
    if not is_comment and papers:
        if any(p.abstract and p.abstract.strip() for p in short_list):
            catalog = _build_abstract_catalog(short_list, alias_map)
        else:
            catalog = _build_papers_catalog_alias(short_list, alias_map)

    system = render(
        "literature-review-section",
        topic=topic,
        section_key=section.key,
        section_title=section.title,
        section_role=_build_section_role(section, groups, require_citation, alias_map),
        papers_catalog=catalog,
        available_lit_ids="\n".join(sorted(alias_map.keys())) if alias_map else "(无,本节不引用任何文献)",
        humanize=True,
        role_hint=role_hint,
    )
    user = (
        f"研究主题:{topic}\n\n"
        f"章节:{section.title}\n\n"
        f"分组概览:{', '.join(g.name for g in groups) if groups else '无分组'}\n\n"
        "请输出本章节正文。"
    )
    return system, user, allowed, alias_map, is_comment


def _finalize_section_result(
    section: SectionSpec,
    raw_content: str,
    allowed: set[str],
    alias_map: dict[str, str],
    is_comment: bool,
) -> SectionResult:
    content = normalize_model_output(raw_content)
    citations: list[str] = []
    dropped: list[str] = []
    real_lit_by_alias = dict(alias_map)

    def _strip(m: re.Match[str]) -> str:
        token = m.group(1)
        if is_comment:
            dropped.append(token)
            return ""
        real = real_lit_by_alias.get(token, token)
        if real not in allowed:
            dropped.append(token)
            return ""
        if real not in citations:
            citations.append(real)
        return f"[{real}]"

    cleaned = CITE_RE.sub(_strip, content)
    return SectionResult(
        key=section.key,
        title=section.title,
        content=cleaned,
        citations=citations,
        dropped_citations=dropped,
    )


def write_section(
    section: SectionSpec,
    topic: str,
    groups: list[Group],
    papers: list[Paper],
    model: str | None = None,
) -> SectionResult:
    """写一章。papers 是本章允许引用的全集。"""
    system, user, allowed, alias_map, is_comment = _prepare_section_context(
        section, topic, groups, papers,
    )
    raw = _invoke_section_chain(system=system, user=user, model=model)
    return _finalize_section_result(section, raw, allowed, alias_map, is_comment)


def write_section_stream(
    section: SectionSpec,
    topic: str,
    groups: list[Group],
    papers: list[Paper],
    model: str | None = None,
) -> Generator[tuple[str, bool, SectionResult | None], None, None]:
    """流式写一章:逐 token 返回,最后附带最终章节结果。"""
    system, user, allowed, alias_map, is_comment = _prepare_section_context(
        section, topic, groups, papers,
    )
    raw_parts: list[str] = []
    for piece in messages_stream(system=system, user=user, max_tokens=8000, model=model):
        raw_parts.append(piece)
        yield piece, False, None
    result = _finalize_section_result(
        section,
        "".join(raw_parts),
        allowed,
        alias_map,
        is_comment,
    )
    yield "", True, result
