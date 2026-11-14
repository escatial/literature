"""章节 LLM 写作:带引用强校验,拒绝幻觉。

使用 prompts/literature-review.md 模板中的 `section` 段作为 system prompt。
核心约束:
- LLM 只能用 [lit_xxx] 形式引用输入清单里存在的 lit_id
- 生成的文本中出现的任何 [lit_xxx] 都必须在允许集合内
- 幻觉引用会被从正文中剥离并记录警告
- 每篇文献在同一章里只出现一次
- "comment" 章节不引用任何文献(literature-review skill 强制规则)
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Generator

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnableLambda

from src.llm.client import (
    messages_create,
    messages_stream_ex,
    normalize_model_output,
)
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
    # 刀2:重写后引用密度仍不达标时的标记(SSE 下发给前端展示)
    density_warning: bool = False


def _citation_hint(p: Paper) -> str:
    """D1:预格式化的正文夹注形态,LLM 必须照抄。

    修复两类实测缺陷:
    - 英文全名直书(「Rambabu Lavuri et al. (2022)」→ 应为「Lavuri et al. (2022)」);
    - 无作者文献被当作者名(「通威鱼(2013)」实为广告策划案例)→ 用短题名。
    """
    authors = [a.strip() for a in (p.authors or []) if a and a.strip()]
    year = str(p.year) if p.year else "无年份"
    if not authors:
        title = (p.title or "").strip().rstrip(".")
        short = title[:16] + ("…" if len(title) > 16 else "")
        return f"《{short}》({year})"
    ascii_names = all(a.isascii() for a in authors)
    if ascii_names:
        first = authors[0]
        if "," in first:
            first = first.split(",", 1)[0].strip()
        surname = first.split()[-1].rstrip(".") if first.split() else first
        return f"{surname} et al. ({year})" if len(authors) > 1 else f"{surname} ({year})"
    return f"{authors[0]}等 ({year})" if len(authors) > 1 else f"{authors[0]} ({year})"


_CORE_LEVEL_MARKS = ("核心", "CSSCI", "北大核心", "CSCD", "SCI", "SSCI", "EI", "Q1", "Q2")


def _is_core_journal(p: Paper) -> bool:
    """期刊分层:journal_level 命中核心标识视为核心期刊(缺失视为非核心)。"""
    level = (p.journal_level or "").strip()
    return any(mark.lower() in level.lower() for mark in _CORE_LEVEL_MARKS)


def _order_for_writing(papers: list[Paper], grades: dict[str, str] | None) -> list[Paper]:
    """写作清单排序:核心期刊优先、高相关次之(此前中文池被低层次对策文主导)。"""
    grade_order = {"high": 0, "medium": 1, "low": 2}

    def sort_key(p: Paper):
        grade = grade_order.get((grades or {}).get(p.lit_id, "medium"), 1)
        return (0 if _is_core_journal(p) else 1, grade)

    return sorted(papers, key=sort_key)


def _build_papers_catalog(papers: list[Paper]) -> str:
    """论文清单(标题/作者/年份/期刊)。"""
    lines = []
    for p in papers:
        lines.append(
            f"- {p.lit_id} | {p.title} | {', '.join(p.authors)} | "
            f"{p.journal or 'N/A'} | {p.year or 'N/A'}"
        )
    return "\n".join(lines) if lines else "(本章无可引用文献)"


_GRADE_LABELS = {"high": "高相关", "medium": "中相关", "low": "低相关(仅背景)"}
_GRADE_ORDER = {"high": 0, "medium": 1, "low": 2}


def _grade_of(lit_id: str, grades: dict[str, str] | None) -> str:
    """取文献的相关性等级;缺失时按中相关处理,不擅自升级为高相关。"""
    if not grades:
        return "medium"
    return grades.get(lit_id, "medium")


def _grade_suffix(lit_id: str, grades: dict[str, str] | None) -> str:
    """清单行尾的相关性标注,只在有分级数据时输出。"""
    if not grades:
        return ""
    return f" | 相关性: {_GRADE_LABELS[_grade_of(lit_id, grades)]}"


def _order_by_grade(papers: list[Paper], grades: dict[str, str] | None) -> list[Paper]:
    """高相关文献排在清单最前,引导 LLM 优先取用。"""
    if not grades:
        return list(papers)
    return sorted(papers, key=lambda p: _GRADE_ORDER[_grade_of(p.lit_id, grades)])


def _build_papers_catalog_alias(
    papers: list[Paper],
    alias_map: dict[str, str],
    grades: dict[str, str] | None = None,
) -> str:
    """带短 alias 的论文清单(给 LLM 用的版本)。"""
    lines = []
    real_to_alias = {real: alias for alias, real in alias_map.items()}
    for p in _order_by_grade(papers, grades):
        alias = real_to_alias.get(p.lit_id, p.lit_id)
        lines.append(
            f"- {alias} | {p.title} | {', '.join(p.authors)} | "
            f"{p.journal or 'N/A'} | {p.year or 'N/A'}"
            f" | 夹注: {_citation_hint(p)}"
            f"{_grade_suffix(p.lit_id, grades)}"
        )
    return "\n".join(lines) if lines else "(本章无可引用文献)"


def _build_abstract_catalog(
    papers: list[Paper],
    alias_map: dict[str, str],
    max_summary_chars: int = 400,
    grades: dict[str, str] | None = None,
) -> str:
    """构造带摘要的文献池, 直接供 LLM 基于摘要写作。"""
    lines = []
    real_to_alias = {real: alias for alias, real in alias_map.items()}
    for p in _order_by_grade(papers, grades):
        alias = real_to_alias.get(p.lit_id, p.lit_id)
        meta = (
            f"{alias} | {p.title} | {', '.join(p.authors)} | "
            f"{p.journal or 'N/A'} | {p.year or 'N/A'}"
            f" | 夹注: {_citation_hint(p)}"
            f"{_grade_suffix(p.lit_id, grades)}"
        )
        if p.abstract:
            ab = p.abstract.strip()
            if len(ab) > max_summary_chars:
                ab = ab[:max_summary_chars] + "…"
            lines.append(f"- {meta}\n  摘要: {ab}")
        else:
            lines.append(f"- {meta}\n  摘要: (无摘要, 仅标题可参考)")
    return "\n".join(lines) if lines else "(本章无可引用文献)"


def _extract_plain_section_text(raw: str) -> str:
    """严格提取章节正文，拒绝结构化响应和调试包络。"""
    text = normalize_model_output(raw).strip()
    if not text:
        raise ValueError("章节模型返回空正文")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return text
    if isinstance(parsed, dict):
        raise ValueError("章节模型返回 JSON 对象，拒绝将结构化响应当作正文")
    if isinstance(parsed, list):
        raise ValueError("章节模型返回 JSON 数组，拒绝将结构化响应当作正文")
    raise ValueError("章节模型返回不可接受的结构化值")


_META_REASONING_MARKERS = (
    "let me check", "let me make sure", "let me finalize", "let me revise",
    "ok final", "one more consideration", "the previous draft", "auto-check",
    "the original draft", "i should not write", "maybe the issue is",
    # 2026-11-12 事故扩容:glm-5.3 经网关把英文自我分析写进正文通道
    # (评审42第3章),词形集中在 We need / Let's / Potential concern 一族
    "we need", "let's", "need answer", "pure body text",
    "potential concern", "potential issue", "polish:", "polished final",
    "我先检查", "上一版草稿", "自动检查", "内部推理", "思考过程",
)
_FALLBACK_EVIDENCE_PREFIXES = (
    "补充证据显示",
    "与本节问题相邻的研究中",
)
# 草稿过程结构标签行(Paragraph N: / Polish: / Potential concern: 等)
_STRUCTURAL_LABEL_RE = re.compile(
    r"^\s*(?:paragraph\s*\d+(?:\s+final)?|second|third|fourth|fifth)\s*[:：]"
    r"|^\s*(?:polish|potential\s+(?:concern|issue)|questions?)\s*[:：]",
    re.MULTILINE | re.IGNORECASE,
)
# 流式首稿长度上限:提示词目标 1200-2000 字,超过 4 倍视为缓冲/重复流
_SECTION_STREAM_MAX_CHARS = int(
    os.environ.get("WRITING_SECTION_STREAM_MAX_CHARS", "8000")
)


def _is_fallback_only(text: str) -> bool:
    """识别章节是否含有题录兜底句。"""
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return any(line.startswith(_FALLBACK_EVIDENCE_PREFIXES) for line in lines)


def _section_stream_anomaly(text: str) -> bool:
    """流式首稿是否为网关缓冲/推理回声类异常输出,不可进入夹注回填与润色链。

    2026-11-12 事故:并行章节流式写入时,网关把整段响应缓冲成单块返回
    (评审42:单块 45766 字符,内容为系统提示词英文回声+错位重复流)。
    正常章节正文按提示词目标为 1200-2000 字,超限或英文主导即判异常。
    """
    value = (text or "").strip()
    if not value:
        return True
    if len(value) > _SECTION_STREAM_MAX_CHARS:
        return True
    zh = len(re.findall(r"[\u4e00-\u9fff]", value))
    en = len(re.findall(r"[A-Za-z]", value))
    # 中文综述正文不可能英文超过中文 1.5 倍(引用作者名也远达不到)
    if en > 400 and en > zh * 1.5:
        return True
    return _looks_like_non_article(value)


def _looks_like_non_article(text: str) -> bool:
    """识别模型把内部检查、文献清单或计划泄漏进正文的情况。"""
    value = (text or "").strip()
    if not value:
        return False
    head = value[:1200].lower()
    marker_hits = sum(marker in head for marker in _META_REASONING_MARKERS)
    catalog_lines = sum(
        1 for line in value.splitlines()
        if re.search(r"^\s*[-*]\s+.*\blit_[a-z0-9_]+\b", line, flags=re.IGNORECASE)
    )
    # Valid section prose receives several [lit_xxx] anchors from this module
    # after author/year binding.  Counting those anchors as raw ``lit_`` text
    # made every well-cited section look like a leaked catalog and replaced the
    # real 2,000+ character draft with a one-line placeholder.  Only count
    # unwrapped lit_id text after removing legitimate inline anchors.
    non_anchor_text = CITE_RE.sub("", value)
    leaked_lit_ids = len(re.findall(r"\blit_[a-z0-9_]+\b", non_anchor_text, re.IGNORECASE))
    zh = len(re.findall(r"[\u4e00-\u9fff]", value))
    en = len(re.findall(r"[A-Za-z]", value))
    return (
        marker_hits >= 1
        or catalog_lines >= 3
        or leaked_lit_ids >= 3
        or len(_STRUCTURAL_LABEL_RE.findall(value)) >= 3
        or (en > 400 and en > zh * 1.5)
        or _is_fallback_only(value)
    )


def _article_quality_score(text: str, citations: int = 0) -> int:
    """在首稿/重写稿之间优先选择可交付正文，而非只看引用数量。"""
    value = text or ""
    score = len(re.findall(r"[\u4e00-\u9fff]", value)) + citations * 80
    if _looks_like_non_article(value):
        score -= 100000
    return score


def _strip_obvious_meta_lines(text: str) -> str:
    """删除无法作为正文交付的模型清单/内部步骤行。"""
    kept: list[str] = []
    for line in (text or "").splitlines():
        low = line.strip().lower()
        if not low:
            kept.append(line)
            continue
        if any(marker in low for marker in _META_REASONING_MARKERS):
            continue
        # 草稿结构标签行(Paragraph 1 final: / Polish: 等)整行删除
        if _STRUCTURAL_LABEL_RE.match(line.strip()):
            continue
        if re.search(r"^\s*[-*]\s+.*\blit_[a-z0-9_]+\b", line, flags=re.IGNORECASE):
            continue
        if "lit_" in low and re.search(r"\b(?:paragraph|段落|plan|计划|draft|草稿)\b", low):
            continue
        kept.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()


def _invoke_section_chain(
    *,
    system: str,
    user: str,
    model: str | None = None,
) -> str:
    """使用 LangChain 编排章节写作，不接入检索器。"""
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
    return _extract_plain_section_text(
        chain.invoke({"system": system, "user": user, "model": model})
    )


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
        "每条观点末尾标点前写入该文献的 [lit_xxx] 锚点(严格从 available_lit_ids 取)。"
        "格式:作者（年份）认为...观点[lit_xxx]。 后端会校验锚点并转为顺序编号脚注;"
        "未在列表中的 lit_xxx 会被剥离,不得编造文献。"
        if require_citation
        else "本章不强求引用,如需佐证只能写文献清单中的作者（年份）,不要输出锚点。"
    )
    return (
        f"分类方式下的分组:{group_names}。本节定位:{section.instruction}\n"
        f"引用要求:{cite_rule}\n"
        "正文只能使用作者+年份指代文献，禁止出现期刊、刊物、数据库名称；期刊字段仅供文末参考文献元数据使用。\n"
        "遵循 literature-review skill 的批判性写作原则:"
        "禁止简单罗列、必须包含比较与对比、识别共识与争议。"
    )


def _build_grade_rule(
    papers: list[Paper],
    alias_map: dict[str, str],
    grades: dict[str, str] | None,
) -> str:
    """相关性分级的写作硬约束:高相关优先,低相关仅作背景补充。"""
    if not grades or not alias_map:
        return ""
    real_to_alias = {real: alias for alias, real in alias_map.items()}
    low = [
        real_to_alias.get(p.lit_id, p.lit_id)
        for p in papers
        if _grade_of(p.lit_id, grades) == "low"
    ]
    rule = (
        "\n【文献相关性分级约束(必须遵守)】\n"
        "文献清单每条已标注相关性等级。写作时:\n"
        "1. 正文论述与论据支撑优先取用『高相关』文献,核心结论必须由高相关文献支撑\n"
        "2. 『中相关』文献用于补充论证与横向对比\n"
        "3. 『低相关(仅背景)』文献只能出现在背景铺垫或研究现状概述中,"
        "严禁作为核心论据、严禁用于支撑关键结论"
    )
    if low:
        rule += "\n本章仅可作背景补充的文献: " + ", ".join(low)
    return rule


# === v9.9: 主题综合(synthesis)素材注入 ===

def _alias_refs(lit_ids: list[str], alias_map: dict[str, str]) -> str:
    real_to_alias = {real: alias for alias, real in alias_map.items()}
    return ", ".join(real_to_alias.get(lid, lid) for lid in lit_ids)


def _build_theme_synthesis_block(
    synthesis: dict, alias_map: dict[str, str]
) -> str:
    """主题章的综合素材块:共识/争议/方法差异/空白,文献以别名指代。"""
    lines: list[str] = [
        "【主题综合素材(前序综合阶段产出,必须消化进正文;括号内为文献清单别名)】"
    ]
    for item in synthesis.get("consensus") or []:
        lines.append(
            f"- 共识: {item['claim']} (支撑: {_alias_refs(item['lit_ids'], alias_map)})"
        )
    for item in synthesis.get("controversies") or []:
        a, b = item["side_a"], item["side_b"]
        causes = "、".join(c["cause"] for c in item.get("possible_causes") or [])
        line = (
            f"- 争议: {item['claim']} —— 一方({_alias_refs(a['lit_ids'], alias_map)})"
            f"{a.get('position') or ''};另一方({_alias_refs(b['lit_ids'], alias_map)})"
            f"{b.get('position') or ''}"
        )
        if causes:
            line += f";可能原因(推测,正文须以推测语气表述): {causes}"
        lines.append(line)
    for diff in synthesis.get("method_differences") or []:
        lines.append(f"- 方法差异: {diff}")
    for gap in synthesis.get("gaps") or []:
        if gap.get("kind") == "confirmed":
            lines.append(f"- 证据性空白: {gap['gap']} (依据: {gap.get('evidence') or '见上'})")
        else:
            lines.append(
                f"- 检索性空白: {gap['gap']} (只能表述为『现有文献较少涉及』,"
                "严禁写成『尚无研究』)"
            )
    return "\n".join(lines)


def _build_comment_synthesis_block(synthesis_map: dict | None) -> str:
    """述评章的跨主题素材:争议点与空白(不引用文献,只概括)。"""
    if not synthesis_map:
        return ""
    lines: list[str] = ["【跨主题综合素材(本段不出现任何文献夹注,仅作概括性表述)】"]
    for key in sorted(synthesis_map.keys()):
        synthesis = synthesis_map[key] or {}
        for item in synthesis.get("controversies") or []:
            causes = "、".join(c["cause"] for c in item.get("possible_causes") or [])
            suffix = f"(可能原因: {causes})" if causes else ""
            lines.append(f"- 争议点: {item['claim']}{suffix}")
    confirmed = [
        g["gap"]
        for s in synthesis_map.values() if s
        for g in (s.get("gaps") or []) if g.get("kind") == "confirmed"
    ]
    retrieved = [
        g["gap"]
        for s in synthesis_map.values() if s
        for g in (s.get("gaps") or []) if g.get("kind") != "confirmed"
    ]
    for gap in confirmed:
        lines.append(f"- 研究空白(有证据支撑): {gap}")
    for gap in retrieved:
        lines.append(f"- 研究空白(仅未检索到,表述为『现有研究较少』): {gap}")
    return "\n".join(lines) if len(lines) > 1 else ""


def _build_rq_block(
    research_questions: list[str] | None, review_goal: str | None
) -> str:
    parts: list[str] = []
    rqs = [str(q).strip() for q in (research_questions or []) if str(q).strip()]
    if rqs:
        parts.append("研究问题(正文论证须服务于此):\n" + "\n".join(f"- {q}" for q in rqs))
    goal = (review_goal or "").strip()
    if goal:
        parts.append(f"综述目标: {goal}")
    return "\n".join(parts)


def _prepare_section_context(
    section: SectionSpec,
    topic: str,
    groups: list[Group],
    papers: list[Paper],
    grades: dict[str, str] | None = None,
    synthesis_map: dict[str, dict] | None = None,
    research_questions: list[str] | None = None,
    review_goal: str | None = None,
    notes_block: str | None = None,
) -> tuple[str, str, set[str], dict[str, str], bool]:
    allowed = {p.lit_id for p in papers}
    is_comment = section.key == "comment"
    require_citation = (not is_comment) and bool(allowed) and section.key not in (
        "introduction", "method", "conclusion",
    )

    alias_map: dict[str, str] = {}
    short_list: list[Paper] = []
    if not is_comment and papers:
        # D1:写作清单核心期刊优先、高相关次之 —— 别名/锚点仍按原列表分配,
        # 只影响清单展示顺序(引导 LLM 优先取用高质量文献)。
        papers = _order_for_writing(list(papers), grades)
        for idx, p in enumerate(papers, start=1):
            prefix = {
                "openalex": "oa",
                "pubmed": "pm",
                "cnki": "cn",
                "crossref": "cr",
                "user_imported": "ui",
            }.get(p.source.value, "x")
            alias = f"lit_{prefix}_{idx}"
            alias_map[alias] = p.lit_id
            short_list.append(p)

    if is_comment:
        role_hint = "【本章不引用任何文献 — 见下方规则】"
    elif require_citation:
        role_hint = (

            "【每条观点末尾标点前写入该文献的 [lit_xxx] 锚点,锚点必须严格来自 available_lit_ids 列表】\n"

            "引用格式:作者（年份）认为...观点[lit_xxx]。\n"

            "示例:周爱莲等（2020）认为...策略有效[lit_cnki_abc]。 / Smith et al.（2025）found...[lit_openalex_def]。"

        )
    else:
        role_hint = "【本章不强制引用 — 见下方规则】"

    catalog = "(本章无可引用文献)"
    if not is_comment and papers:
        if any(p.abstract and p.abstract.strip() for p in short_list):
            catalog = _build_abstract_catalog(short_list, alias_map, grades=grades)
        else:
            catalog = _build_papers_catalog_alias(short_list, alias_map, grades=grades)

    section_role = _build_section_role(section, groups, require_citation, alias_map)
    if not is_comment:
        section_role += _build_grade_rule(short_list, alias_map, grades)
    if require_citation:
        section_role += (
            f"\n本节共有 {len(short_list)} 篇可引用文献，至少覆盖其中 "
            f"{_min_citation_target(len(short_list))} 篇不同文献。"
            "请围绕这些文献进行比较、归纳和评述，不要只反复引用少数文献。"
            "每个正文段落至少应比较或并置3位不同学者及其文献；不足3篇时必须扩展该段的文献对话，"
            "不能用同一篇文献重复凑数。"
            "\n段落密度与结构(硬要求):每个自然段不少于 250 字;每段须完成"
            "「共识——分歧(机制/情境差异)——证据局限」三元推进,不得只罗列各文献做法;"
            "相邻段落不得使用相同句式开头(如连续以「X认为」「Y则」起段),须变换论述结构;"
            "正文夹注必须照抄文献清单中的「夹注」形态(英文作者只用第一作者姓氏+et al.)。"
        )
    # v9.9: 主题综合素材(theme 章 = 本主题条目;comment 章 = 跨主题空白/争议)
    if synthesis_map:
        if is_comment:
            synthesis_block = _build_comment_synthesis_block(synthesis_map)
        else:
            synthesis = synthesis_map.get(section.key)
            synthesis_block = (
                _build_theme_synthesis_block(synthesis, alias_map)
                if synthesis else ""
            )
        if synthesis_block:
            section_role += "\n" + synthesis_block
    # R1' 蒸馏证据层(2026-10-27 先读后写):通读笔记进 prompt,位于
    # 综合素材之后;comment 章不引用文献,不注入笔记。None 时零改动。
    if notes_block and not is_comment:
        section_role += "\n" + notes_block
    if is_comment:
        rqs = [str(q).strip() for q in (research_questions or []) if str(q).strip()]
        if rqs:
            section_role += (
                "\n述评末尾必须以『文献述评 → 研究空白 → 引出研究问题』的顺序,"
                "明确说明上述不足如何引出并回应以下研究问题: "
                + ";".join(rqs)
            )

    system = render(
        "literature-review:section",
        topic=topic,
        section_key=section.key,
        section_title=section.title,
        section_role=section_role,
        papers_catalog=catalog,
        available_lit_ids="\n".join(sorted(alias_map.keys())) if alias_map else "(无,本节不引用任何文献)",
        humanize=True,
        role_hint=role_hint,
    )
    user_parts = [f"研究主题:{topic}"]
    rq_block = _build_rq_block(research_questions, review_goal)
    if rq_block:
        user_parts.append(rq_block)
    user_parts.append(f"章节:{section.title}")
    user_parts.append(
        f"分组概览:{', '.join(g.name for g in groups) if groups else '无分组'}"
    )
    user_parts.append("请输出本章节正文。")
    user = "\n\n".join(user_parts)
    return system, user, allowed, alias_map, is_comment




# === v6.2: 自动从"作者(年份)"夹注派生 [lit_xxx] 锚点 ===
# LLM 只负责写正文中的“作者(年份)”夹注, 后端以正则查询表查 paper 自动补 [lit_xxx]。
# 2026-10-27:旧版实现(AUTHOR_YEAR_PATTERNS + _extract_surname +
# 旧 _build_surname_year_index / _inject_citations_by_author_year,返回 2 元组)
# 已删除 —— 它们被下方 V63 版(返回 3 元组)同名覆盖,运行时不可达,
# 且两版返回契约不同,留存会误导维护。
import re as _re_inject


# 模型偶尔输出"周。愉。峰"或"胡，大，伟"——即中文标点(。 ， 、 ,)把姓名单字拆开,
# 导致下面的 [\u4e00-\u9fff]{2,8} 长度约束无法命中,作者/年份夹注自然漏匹配。
# 修复策略:在 _inject_citations_by_author_year 之前把"夹注前 1~6 个汉字之间的中文标点"压平。
_CN_PUNCT_IN_NAMES = re.compile(
    "([\u4e00-\u9fff])[\u3002\uff0c\u3001\u300b\u300d\uff0e\u00b7\uff1b\uff1a\uff01\uff1f]+(?=[\u4e00-\u9fff])"
)
_YEAR_BRACKET = re.compile(r"[\uff08(]\s*\d{4}\s*[\uff09)]")
_CITE_ANCHOR_LIT = re.compile(r"\[(lit_[a-zA-Z0-9_]+|hash:[a-zA-Z0-9_]+)\]")


def _is_chinese_name_char(ch: str) -> bool:
    """判断字符是否属于可能出现在作者姓名/年份夹注中的内容。"""
    if not ch or ch == "\n":
        return False
    cp = ord(ch)
    if 0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF:
        return True
    if 0x3000 <= cp <= 0x303F or 0xFF00 <= cp <= 0xFFEF:
        return True
    return ch == " "


def _collapse_author_punctuation(text: str) -> str:
    """压平 (YYYY) 夹注之前 1~6 个汉字之间的中文标点,使姓名能被正则识别。"""
    if not text:
        return text
    out: list[str] = []
    last = 0
    for m in _YEAR_BRACKET.finditer(text):
        start = m.start()
        head = start
        # 严格上限:回溯最多 6 字符,且遇到句末标点 。！？!? 与
        # 中文逗号、冒号、顿号 立刻停止。
        while head > last and (start - head) < 6:
            ch = text[head - 1]
            if ch in "。！？!?，：；、":
                break
            if not _is_chinese_name_char(ch):
                break
            head -= 1
        head = max(head, last)
        seg = text[head:start]
        if start - head <= 8 and _CN_PUNCT_IN_NAMES.search(seg):
            cleaned = _CN_PUNCT_IN_NAMES.sub("\1", seg)
            out.append(text[last:head])
            out.append(cleaned)
        else:
            out.append(text[last:start])
        out.append(m.group(0))
        last = m.end()
    out.append(text[last:])
    return "".join(out)


# v6.3 override:按句解析作者(年份),并将真实 lit_id 插入对应句末。
# 该定义位于旧版兼容实现之后,确保运行时使用下面的确定性逻辑。
_LATIN_AUTHOR = r"A-Za-z\u00c0-\u024f"
_LATIN_TOKEN = rf"[{_LATIN_AUTHOR}][{_LATIN_AUTHOR}'-]*(?:\.)?"
_AUTHOR_YEAR_PATTERNS_V63 = (
    _re_inject.compile(
        # 中文作者组必须以 2~4 字作者名开头，并只允许显式作者分隔词。
        # 过宽的 {2,8} 会把“这类研究与张三”整段吞成作者名，随后把
        # 正文普通文字误判成幻觉引用并剥掉年份。
        rf"(?P<authors>[\u4e00-\u9fff]{{2,4}}(?:\s*(?:、|,|，|和|与)\s*[\u4e00-\u9fff]{{2,4}})*(?:\s*等)?)[\uff08(]\s*(?P<year>\d{{4}})\s*[\uff09)]"
    ),
    _re_inject.compile(
        rf"(?P<authors>{_LATIN_TOKEN}(?:\s+{_LATIN_TOKEN})?\s+et\s+al\.?)[\uff08(]\s*(?P<year>\d{{4}})\s*[\uff09)]",
        _re_inject.IGNORECASE,
    ),
    _re_inject.compile(
        rf"(?P<authors>{_LATIN_TOKEN}(?:\s+{_LATIN_TOKEN})?\s+(?:and|&|与)\s+{_LATIN_TOKEN})[\uff08(]\s*(?P<year>\d{{4}})\s*[\uff09)]",
        _re_inject.IGNORECASE,
    ),
    _re_inject.compile(
        rf"(?P<authors>{_LATIN_TOKEN})\s*等[\uff08(]\s*(?P<year>\d{{4}})\s*[\uff09)]"
    ),
    _re_inject.compile(
        rf"(?P<authors>{_LATIN_TOKEN}(?:\s+{_LATIN_TOKEN}){{1,2}})\s*等[\uff08(]\s*(?P<year>\d{{4}})\s*[\uff09)]"
    ),
    _re_inject.compile(
        rf"(?P<authors>{_LATIN_TOKEN}(?:\s+{_LATIN_TOKEN}){{0,2}})\s*[\uff08(]\s*(?P<year>\d{{4}})\s*[\uff09)]"
    ),
)


def _normalize_author_key_v63(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.casefold().replace("–", "-").replace("—", "-")
    return _re_inject.sub(r"[^a-z0-9\u4e00-\u9fff-]", "", value)


def _author_surname_keys_v63(author_str: str) -> set[str]:
    raw = (author_str or "").strip().rstrip(".")
    if not raw:
        return set()
    if _re_inject.search(r"[\u4e00-\u9fff]", raw):
        compact = _re_inject.sub(r"[^\u4e00-\u9fff]", "", raw)
        # Chinese narrative citations use the author's full name.  Indexing
        # the first character as a surname made virtually every common surname
        # (Li/Wang/Liu/Zhang...) ambiguous in the same year and prevented real
        # anchors from being injected.  Keep only the exact full-name key.
        key = _normalize_author_key_v63(compact)
        return {key} if key else set()
    raw = _re_inject.sub(r"\s+et\s+al\.?$", "", raw, flags=_re_inject.IGNORECASE)
    raw = _re_inject.sub(r"\s+(?:and|&)\s+.*$", "", raw, flags=_re_inject.IGNORECASE)
    if "," in raw:
        family = raw.split(",", 1)[0].strip()
    else:
        parts = raw.split()
        if not parts:
            return set()
        # OpenAlex 常见 Given Family, PubMed 常见 Family Initials。
        family = parts[-1] if len(parts) >= 2 and len(parts[-1].rstrip(".")) > 1 else parts[0]
    key = _normalize_author_key_v63(family)
    return {key} if key else set()


def _build_paper_metadata_index(papers: list[Paper]) -> dict[str, Paper]:
    """建立 lit_id -> Paper 的权威文献元数据索引。"""
    index: dict[str, Paper] = {}
    for paper in papers:
        lit_id = getattr(paper, "lit_id", None)
        if not lit_id:
            continue
        if lit_id in index and index[lit_id] is not paper:
            log.error("文献池存在重复 lit_id,保留后出现的元数据: %s", lit_id)
        index[lit_id] = paper
    return index


def _build_author_year_to_lit_ids(
    paper_metadata_by_id: dict[str, Paper],
) -> dict[tuple[str, int], list[str]]:
    """从 lit_id -> Paper 派生作者/年份到 lit_id 的反向索引。

    反向键来自每条 Paper 的完整 authors/year 字段,最终使用前仍必须
    通过返回的 lit_id 回到 paper_metadata_by_id 取回整条文献。
    """
    index: dict[tuple[str, int], list[str]] = {}
    for lit_id, paper in paper_metadata_by_id.items():
        try:
            year = int(paper.year)
        except (TypeError, ValueError):
            continue
        if year < 1000 or year > 9999:
            continue
        for author in (paper.authors or []):
            for surname in _author_surname_keys_v63(author):
                if surname:
                    ids = index.setdefault((surname, year), [])
                    if lit_id not in ids:
                        ids.append(lit_id)
    return index


def _build_surname_year_index(papers: list[Paper]):
    """兼容旧调用点;索引由 Paper 元数据索引派生。"""
    return _build_author_year_to_lit_ids(_build_paper_metadata_index(papers))


def _normalize_known_author_punctuation(content: str, papers: list[Paper]) -> str:
    """只按真实文献作者元数据修复姓名内部的中文标点。

    模型偶尔会把“胡大伟”输出成“胡，大，伟”或“蒋桥桥”输出成
    “蒋桥。桥”。全局删除中文标点会误伤正文，因此这里仅生成真实作者
    姓名的“标点分隔变体”并替换，且只处理 2 字以上的中文姓名。
    """
    text = content or ""
    names: set[str] = set()
    for paper in papers:
        for author in paper.authors or []:
            raw = str(author or "")
            if not _re_inject.search(r"[\u4e00-\u9fff]", raw):
                continue
            compact = _re_inject.sub(r"[^\u4e00-\u9fff]", "", raw)
            if len(compact) >= 2:
                names.add(compact)
    # 长姓名优先，避免短姓名先替换后破坏长姓名的匹配。
    for name in sorted(names, key=lambda value: (-len(value), value)):
        if len(name) < 2:
            continue
        # 必须至少出现一个分隔符才会命中，普通完整姓名不会被改写。
        separator = r"[\s,，。．、·；：:！？!?\-—]+"
        pattern = _re_inject.compile(
            _re_inject.escape(name[0])
            + "".join(separator + _re_inject.escape(ch) for ch in name[1:])
        )
        text = pattern.sub(name, text)
    return text


def _citation_author_keys_v63(author_text: str) -> set[str]:
    text = _re_inject.sub(
        r"\s*(?:等|et\s+al\.?|and|&)\s*",
        ",",
        author_text,
        flags=_re_inject.IGNORECASE,
    )
    parts = [part.strip() for part in _re_inject.split(r"[,，、和与]", text) if part.strip()]
    keys: set[str] = set()
    for part in parts:
        keys.update(_author_surname_keys_v63(part))
    return keys


def _citation_context_terms(value: str) -> set[str]:
    """Build compact lexical features for author/year disambiguation."""
    terms = {
        token.casefold()
        for token in _re_inject.findall(r"[A-Za-z][A-Za-z0-9-]{2,}", value or "")
        if token.casefold() not in {
            "the", "and", "with", "for", "from", "this", "that", "study",
            "research", "based", "using", "model", "method",
        }
    }
    for chunk in _re_inject.findall(r"[\u4e00-\u9fff]{2,}", value or ""):
        if len(chunk) <= 4:
            terms.add(chunk)
        terms.update(chunk[i:i + 2] for i in range(len(chunk) - 1))
        terms.update(chunk[i:i + 3] for i in range(len(chunk) - 2))
    return terms


def _resolve_ambiguous_lit_id(
    candidates: set[str],
    context: str,
    metadata_by_id: dict[str, Paper],
) -> str | None:
    """Resolve same-author/same-year candidates from the surrounding sentence."""
    context_terms = _citation_context_terms(context)
    ranked: list[tuple[int, str]] = []
    for lit_id in candidates:
        paper = metadata_by_id.get(lit_id)
        if paper is None:
            continue
        title_terms = _citation_context_terms(paper.title or "")
        abstract_terms = _citation_context_terms((paper.abstract or "")[:1200])
        score = len(context_terms & title_terms) * 4 + len(context_terms & abstract_terms)
        ranked.append((score, lit_id))
    ranked.sort(reverse=True)
    if not ranked or ranked[0][0] <= 0:
        return None
    if len(ranked) > 1 and ranked[0][0] == ranked[1][0]:
        return None
    return ranked[0][1]


def _inject_citations_by_author_year(content, papers):
    """只给命中当前文献池的作者(年份)夹注补真实 lit_id,位置固定在句末。

    返回 (content, cited_ids, dropped_unmatched):
    - cited_ids: 命中并注入锚点的 lit_id(去重,按出现顺序)
    - dropped_unmatched: 未命中文献池的夹注原文(幻觉引用),用于向上游上报;
      正文中这类夹注的"（年份）"会被剥离、仅保留作者名,避免残留伪引用。
    """
    if not content or not papers:
        return content, [], []
    # 先做基于真实作者表的定向规范化，再运行作者/年份识别。
    # 这一步不能使用“任意汉字+标点”全局替换，否则会把普通叙述改成假姓名。
    content = _normalize_known_author_punctuation(content, list(papers))
    paper_metadata_by_id = _build_paper_metadata_index(papers)
    surname_index = _build_author_year_to_lit_ids(paper_metadata_by_id)
    if not surname_index:
        return content, [], []

    matches = []
    for pattern in _AUTHOR_YEAR_PATTERNS_V63:
        matches.extend(pattern.finditer(content))
    # 不同正则可能命中同一夹注的子串,按最长优先并丢弃重叠短匹配。
    unique_matches = []
    for match in sorted(matches, key=lambda item: (item.start(), -len(item.group(0)))):
        if any(
            match.start() < accepted.end() and accepted.start() < match.end()
            for accepted in unique_matches
        ):
            continue
        unique_matches.append(match)

    sentence_ends = [
        match.start()
        for match in _re_inject.finditer(
            r"[。！？!?；;\n]|(?<=[A-Za-zÀ-Þ])\.(?!等)(?=\s*[A-ZÀ-Þ\u4e00-\u9fff]|$)",
            content,
        )
    ]
    insertions: dict[int, list[str]] = {}
    cited_ids: list[str] = []
    unmatched_spans: list[tuple[int, int]] = []
    ambiguous_spans: list[tuple[int, int]] = []
    for match in unique_matches:
        try:
            year = int(match.group("year"))
        except (TypeError, ValueError):
            continue
        candidate_sets = [
            set(surname_index.get((author_key, year), []))
            for author_key in _citation_author_keys_v63(match.group("authors"))
        ]
        candidate_sets = [items for items in candidate_sets if items]
        candidates: set[str] = set()
        if candidate_sets:
            candidates = set.intersection(*candidate_sets)
            if not candidates:
                candidates = set.union(*candidate_sets)

        sentence_end = next((pos for pos in sentence_ends if pos >= match.end()), None)
        sentence_start = 0
        for pos in sentence_ends:
            if pos >= match.start():
                break
            sentence_start = pos + 1
        context = content[sentence_start:(sentence_end + 1 if sentence_end is not None else len(content))]

        ambiguous_match = len(candidates) > 1
        if len(candidates) == 1:
            resolved_ids = list(candidates)
        elif ambiguous_match:
            resolved = _resolve_ambiguous_lit_id(candidates, context, paper_metadata_by_id)
            resolved_ids = [resolved] if resolved else []
            if resolved is None:
                log.warning(
                    "正文夹注存在同名同年歧义,上下文无法消歧: %s",
                    match.group(0),
                )
        else:
            resolved_ids = []

        matched_ids: list[str] = []
        for lit_id in resolved_ids:
            paper = paper_metadata_by_id.get(lit_id)
            if paper is None or not paper.title or not paper.authors or not paper.year:
                log.warning("引用候选缺少完整文献元数据,跳过: %s", match.group(0))
                continue
            if lit_id not in matched_ids:
                matched_ids.append(lit_id)
        if not matched_ids:
            # 过长且没有“等/显式分隔符”的中文片段通常是正文被正则
            # 吞入作者组，而不是真正的作者(年份)夹注。不要剥掉它的年份。
            raw_authors = match.group("authors") or ""
            if (
                re.search(r"[\u4e00-\u9fff]", raw_authors)
                and len(_re_inject.sub(r"[^\u4e00-\u9fff]", "", raw_authors)) > 8
                and not re.search(r"等|、|,|，|和|与", raw_authors)
            ):
                continue
            # 同名同年无法唯一定位时不是幻觉:保留作者(年份)原文,由章节
            # 证据兜底补入明确 lit_id。年份必须剥掉,否则最终正文会留下
            # 一个看似正式、实际没有链接的作者年份引用。
            if ambiguous_match:
                ambiguous_spans.append((match.start(), match.end()))
                continue
            # 幻觉夹注:不在当前文献池,记录原文供上报,并延迟剥离其年份括号
            log.warning("正文夹注未匹配到当前文献池,剥离年份保留作者: %s", match.group(0))
            unmatched_spans.append((match.start(), match.end()))
            continue
        if sentence_end is None:
            log.warning("正文夹注后没有句末标点, 未注入: %s", match.group(0))
            continue
        existing = set(CITE_RE.findall(content[match.end():sentence_end]))
        for lit_id in matched_ids:
            # 同一篇文献可以在多个句子中再次出现。``cited_ids`` 是章节级
            # 去重清单，不能用它阻止当前句插入锚点，否则后续作者(年份)
            # 夹注会变成没有链接的裸引用，文章体检会判定为未绑定。
            if lit_id not in existing:
                # 数字引用必须紧跟作者观点的最后一个字，不能放在年份后面。
                # 例如“路世昌等(2023)提出……问题[8]”，而不是
                # “路世昌等(2023)[8]提出……”。句末仍保留给没有作者年份
                # 夹注的自动补证据句使用。
                position = sentence_end
                insertions.setdefault(position, [])
                if lit_id not in insertions[position]:
                    insertions[position].append(lit_id)
            if lit_id not in cited_ids:
                cited_ids.append(lit_id)

    # v9.6:插入与剥离统一基于「原始 content」的偏移,从后往前一次性应用。
    # 此前先插入锚点再按原偏移剥离,插入使文本右移后 content[start:end]
    # 切的是错误区段——正文被随机截断,下游整句删除也基于错误文本。
    edits: list[tuple[int, int, str]] = []  # (start, end, replacement)
    for position, lit_ids in insertions.items():
        anchors = "".join(f"[{lit_id}]" for lit_id in lit_ids)
        edits.append((position, position, anchors))
    dropped_unmatched: list[str] = []
    for start, end in reversed(unmatched_spans):
        segment = content[start:end]
        dropped_unmatched.append(segment)
        # 剥离未命中夹注的"（年份）"部分,保留作者名(如"张欣欣(2016)" -> "张欣欣"),
        # 避免正文残留无法追溯到参考文献的伪引用
        stripped = _re_inject.sub(r"[\uff08(]\s*\d{4}\s*[\uff09)]", "", segment).strip()
        edits.append((start, end, stripped))
    for start, end in reversed(ambiguous_spans):
        segment = content[start:end]
        stripped = _re_inject.sub(
            r"[\uff08(]\s*\d{4}\s*[\uff09)]", "", segment,
        ).strip()
        edits.append((start, end, stripped))
    for start, end, replacement in sorted(edits, key=lambda e: (e[0], e[1]), reverse=True):
        content = content[:start] + replacement + content[end:]
    return content, cited_ids, dropped_unmatched


def _remove_model_citation_tokens(content: str) -> str:
    """移除模型自行输出的锚点,避免它们绕过作者/年份映射。"""
    return CITE_RE.sub("", content or "")


def _min_citation_target(n_papers: int) -> int:
    """按本章文献池规模计算最低不同文献数。

    固定“8 篇”会让几十篇甚至几百篇候选最终只留下个位数引用，
    既不能覆盖证据，也会让主题章节与参考文献池脱节。目标采用分段比例
    并设置上限，兼顾覆盖面和正文可读性；小池子仍保留最低 2 篇规则。

    上限封顶(2026-10-30 策略矛盾修复):48 → 16。单章正文窗口
    (max_tokens=8000 ≈ 2000-3200 字)按学术密度(~1 引用/150 字)可自然
    容纳 12-16 篇;旧公式 70% 比例让 34/35 篇组要求 24-25 篇(70%),
    物理不可达 —— 诚实正文 10-22 篇也被判不足,重写 3 轮后整段替换成
    题录模板墙。修正方向是"要求上限封顶"而非放宽已有底线:下限 max(12)
    与 70% 比例照旧,中小组(≤22 篇)要求值完全不变。
    """
    if n_papers <= 0:
        return 0
    if n_papers < 5:
        return min(n_papers, max(2, n_papers // 2 + 1))
    if n_papers < 15:
        return min(n_papers, max(5, (n_papers + 1) // 2))
    # 15 篇以上至少覆盖约七成候选,但按单章正文容量封顶 16 篇;
    # 代表性文献仍由筛选阶段控制,章节不能只引用少数几篇“代表作”。
    return min(n_papers, max(12, min(16, math.ceil(n_papers * 0.70))))


def _unmatched_citation_feedback(unmatched: list[str]) -> str:
    """构造未匹配夹注(幻觉引用)的重写反馈。"""
    items = "\n".join(f"- {q}" for q in unmatched[:8])
    return (
        "1. 以下作者(年份)夹注在本章文献池中不存在,属于无效引用:\n"
        f"{items}\n"
        "   请把它们改写为文献池中真实文献的作者与年份,"
        "或将相应论断改为不依赖具体文献的一般性表述;\n"
    )


def _density_feedback(required: int, actual: int) -> str:
    """构造引用密度不足的重写反馈。"""
    return (
        f"2. 本稿只引用了 {actual} 篇不同文献,低于本章至少 {required} 篇的要求。\n"
        "   请扩充对文献池中其他文献的讨论与对比,"
        f"确保出现至少 {required} 处不同的作者(年份)夹注;\n"
    )


def _paragraph_citation_ids(text: str) -> list[list[str]]:
    """Return unique internal literature anchors for each non-empty paragraph."""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text or "") if p.strip()]
    return [list(dict.fromkeys(CITE_RE.findall(paragraph))) for paragraph in paragraphs]


_MIN_PROSE_PARAGRAPH_CHARS = 40


def _paragraph_density_failures(text: str, minimum: int = 3) -> list[int]:
    """Return zero-based paragraph indexes with fewer than ``minimum`` sources.

    超短段落(<40 字,LLM 残根/过渡句)不参与 3 源要求:给残根段凑 3 源
    只能靠重复引用(线上英文桶事故的重复模板句正是这么来的)。
    """
    return [
        index
        for index, (ids, paragraph) in enumerate(_paragraph_citation_ids_with_text(text))
        if len(paragraph) >= _MIN_PROSE_PARAGRAPH_CHARS and len(ids) < minimum
    ]


def _paragraph_citation_ids_with_text(text: str) -> list[tuple[list[str], str]]:
    """段落 → (去重引用 id 列表, 段落原文),供密度检查同时看两者。"""
    out: list[tuple[list[str], str]] = []
    for paragraph in [p.strip() for p in re.split(r"\n\s*\n", text or "") if p.strip()]:
        ids = list(dict.fromkeys(CITE_RE.findall(paragraph)))
        out.append((ids, paragraph))
    return out


def _ensure_paragraph_evidence(
    result: SectionResult,
    papers: list[Paper],
    minimum: int = 3,
    section_target: int | None = None,
) -> SectionResult:
    """Ensure every substantive theme paragraph has three traceable sources.

    This is deliberately domain-agnostic.  It uses only the current section's
    paper pool and never invents an author, year, finding, or topic.
    """
    if result.key == "comment" or not papers:
        return result
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", result.content or "") if p.strip()]
    if not paragraphs:
        return result
    if len(papers) < minimum:
        result.density_warning = True
        return result
    allowed = {p.lit_id: p for p in papers if p.lit_id}
    # 全节已出现的文献不得重复引用(线上事故:同一模板句在段内/段间
    # 重复三次 —— 每个缺源段落都重新引用同样的文献,且与节级补句互
    # 相不知情)。
    section_cited = set(CITE_RE.findall(result.content or ""))
    rebuilt: list[str] = []
    changed = False
    for paragraph in paragraphs:
        if len(paragraph) < _MIN_PROSE_PARAGRAPH_CHARS:
            # 残根段不补句:在 12 字残根上贴 3 条模板句就是线上事故原文
            rebuilt.append(paragraph)
            continue
        ids = list(dict.fromkeys(CITE_RE.findall(paragraph)))
        # Count distinct lead authors as well as works.  Three papers by the
        # same author do not satisfy the paragraph-level comparison rule.
        author_keys = set()
        for paper in papers:
            if paper.lit_id in ids:
                authors = [str(a).strip().lower() for a in (paper.authors or []) if str(a).strip()]
                if authors:
                    author_keys.add(authors[0])
        missing = minimum - min(len(ids), len(author_keys) or len(ids))
        if missing > 0:
            for paper in papers:
                if missing <= 0 or not paper.lit_id or paper.lit_id in ids:
                    continue
                if paper.lit_id in section_cited:
                    # 已在全节其他段落引用:重复引用违反"每篇只引一次",
                    # 宁可让该段落计不足(density_warning),也不复制模板句。
                    continue
                if section_target is not None and len(result.citations) >= section_target:
                    # 节级引用已达目标配额:继续补句会超出"每篇只引一次+
                    # 总量受控"的契约(回归用例:重建陈旧 citations 后应
                    # 恰好补到目标数,而不是多出一篇)。
                    break
                display = _paper_display_author_year(paper)
                if not display:
                    continue  # 首作者不可用(脏数据单字母),跳过
                lead = next((str(a).strip().lower() for a in (paper.authors or []) if str(a).strip()), paper.lit_id)
                if lead in author_keys:
                    continue
                phrase = _title_topic_phrase(paper.title or "")
                paragraph += (
                    f" {display}围绕{phrase}"
                    f"提供了相邻问题的研究证据[{paper.lit_id}]。"
                )
                ids.append(paper.lit_id)
                section_cited.add(paper.lit_id)
                author_keys.add(lead)
                missing -= 1
                changed = True
        rebuilt.append(paragraph)
    if changed:
        result.content = "\n\n".join(rebuilt)
    result.citations = list(dict.fromkeys(
        token for token in CITE_RE.findall(result.content or "") if token in allowed
    ))
    result.density_warning = bool(_paragraph_density_failures(result.content, minimum))
    return result


def _paper_display_author_year(paper: Paper) -> str:
    """生成仅用于自动补证据句的作者(年份)显示文本。

    首作者不可用(中文不足 2 个汉字;英文提取不出 ≥2 个字母的姓氏,
    例如脏数据只剩首字母 "N")时返回空串,调用方必须跳过该文献 ——
    宁可少一条补证据句,也不能产出 "N et al.（2022）" 这种伪作者名
    (线上 423 篇池英文桶实测)。
    """
    authors = [str(a).strip() for a in (paper.authors or []) if str(a).strip()]
    if not authors:
        return ""
    if re.search(r"[\u4e00-\u9fff]", authors[0]):
        first = re.sub(r"[^\u4e00-\u9fff]", "", authors[0])
        if len(first) < 2:
            return ""
        if len(authors) == 1:
            name = authors[0]
        elif len(authors) == 2:
            name = f"{authors[0]}和{authors[1]}"
        else:
            name = f"{authors[0]}等"
    else:
        raw = authors[0].rstrip(".").strip()
        if "," in raw:
            surname = raw.split(",", 1)[0].strip()
            parts = [surname]
        else:
            parts = raw.split()
        # PubMed 常用 “Family Initials”(Gauba P/Zhang Z)，OpenAlex
        # 常用 “Given Family”(Mingyang Lyu)。优先取 ≥2 个字母的 token;
        # 全是单字母(脏数据 "N"/"Z")时视为不可用,返回空串。
        solid = [
            tok for tok in parts
            if len(tok.rstrip(".")) >= 2
            and re.fullmatch(r"[A-Za-z][A-Za-z\-']*", tok.rstrip("."))
        ]
        if not solid:
            return ""
        surname = solid[-1] if parts.index(solid[-1]) > 0 else solid[0]
        # 全大写 2-4 字母是 PubMed 的姓名缩写("LB"/"JBB"),不是姓氏 ——
        # 照拼会产出 "LB et al.（2024）"(线上英文桶实测),视为不可用。
        if re.fullmatch(r"[A-Z]{2,4}", surname):
            return ""
        name = surname + (" et al." if len(authors) > 1 else "")
    return f"{name}（{paper.year}）" if paper.year else name


def _title_topic_phrase(title: str) -> str:
    """从标题提取主题短语(不带书名号),供确定性证据句使用。

    正文禁止用《》书名号复述论文标题(线上反馈:无摘要/补证据句把标题
    原文塞进书名号)。这里把标题清洗成"研究主题短语":剥掉基于/面向类
    引导语和 研究/分析/探讨 类尾缀,保留核心研究对象。
    """
    text = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9 \-]", " ", title or "")
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"^(?:基于|面向|关于|考虑)\s*", "", text)
    text = re.sub(r"(?:案例分析|研究|分析|探讨|综述|述评)+$", "", text).strip()
    if re.search(r"[A-Za-z]", text):
        words = [w for w in text.split() if w]
        # 只剥首尾停用词,保留句中功能词 —— "Prediction Based on Bayesian"
        # 剥掉 on 会留下 "Based Bayesian" 这种词沙(线上英文桶实测)。
        stop = {"a", "an", "the", "of", "on", "in", "for", "and", "to", "based"}
        while words and words[0].lower() in stop:
            words.pop(0)
        while words and words[-1].lower() in stop:
            words.pop()
        cap = min(10, len(words))
        while cap > 2 and words[cap - 1].lower() in stop:
            cap -= 1
        words = words[:cap]
        if len(words) >= 2:
            return " ".join(words)
        return "related issues"
    compact = re.sub(r"\s+", "", text)
    if not compact:
        return "相关议题"
    return compact[:14]


def _append_evidence_sentences(
    result: SectionResult,
    papers: list[Paper],
    required: int,
) -> SectionResult:
    """LLM 重写仍未达到密度时，用真实题录补足可追溯证据句。

    句子只陈述“该文围绕题名对应问题展开研究”，不替模型编造指标、
    算法或实验结论；锚点直接绑定真实 lit_id，后续编号阶段统一转换。
    """
    if result.key == "comment" or not papers:
        return result

    # Never trust the mutable ``citations`` field as the sole source of truth.
    # A repair/cleanup pass can replace ``content`` while leaving the old field
    # behind; in that case the orchestrator would believe the section is cited
    # even though no anchor survives into the final document.  Rebuild it from
    # anchors currently present in the actual section text first.
    allowed = {paper.lit_id for paper in papers if paper.lit_id}
    content_ids = [
        token for token in CITE_RE.findall(result.content or "") if token in allowed
    ]
    result.citations = list(dict.fromkeys(content_ids))
    required = max(required, _min_citation_target(len(papers)))
    existing = set(result.citations)
    additions: list[str] = []
    for paper in papers:
        if len(existing) >= required:
            break
        if not paper.lit_id or paper.lit_id in existing:
            continue
        display = _paper_display_author_year(paper)
        if not display:
            continue  # 首作者不可用(脏数据单字母),跳过,不产出伪作者名
        phrase = _title_topic_phrase(paper.title or "")
        additions.append(
            f"与本节问题相邻的研究中，{display}"
            f"围绕{phrase}讨论了相关对象与决策约束[{paper.lit_id}]。"
        )
        existing.add(paper.lit_id)
        result.citations.append(paper.lit_id)
    if additions:
        result.content = (result.content or "").rstrip() + "\n\n" + "\n".join(additions)
        result.density_warning = False
    return result


def ensure_section_evidence(
    result: SectionResult,
    papers: list[Paper],
    required: int | None = None,
) -> SectionResult:
    """Enforce the post-condition that a cited theme chapter has real evidence.

    This is intentionally deterministic and shared by synchronous and streaming
    orchestration.  It is the last defense after LLM cleanup/repair: for every
    non-comment chapter with a non-empty paper pool, the result must retain the
    real ``lit_id`` anchors and a visible author/year evidence sentence for at
    least the chapter's minimum citation target.  It never fabricates findings;
    the fallback sentence only names the paper and its title.
    """
    if result.key == "comment" or not papers:
        return result
    target = _min_citation_target(len(papers)) if required is None else required
    _append_evidence_sentences(result, papers, target)
    _ensure_paragraph_evidence(result, papers, minimum=3, section_target=target)
    result.density_warning = (
        len(result.citations) < target
        or bool(_paragraph_density_failures(result.content, minimum=3))
    )
    if result.density_warning:
        log.warning(
            "章节 %s 证据兜底仍不足: %d/%d 篇(文献池=%d)",
            result.key,
            len(result.citations),
            target,
            len(papers),
        )
    return result


def _repair_section_draft(
    *,
    system: str,
    user: str,
    draft: str,
    papers: list[Paper],
    required: int,
    unmatched: list[str],
) -> str:
    """请求模型修复一轮章节草稿，返回新的纯正文；失败时抛出。"""
    cited_hint = "、".join(_paper_display_author_year(p) for p in papers[:required])
    feedback_parts: list[str] = []
    if unmatched:
        feedback_parts.append(_unmatched_citation_feedback(unmatched))
    feedback_parts.append(_density_feedback(required, 0))
    repair_user = (
        f"{user}\n\n"
        "【章节自动体检未通过，请重写当前章节】\n"
        + "".join(feedback_parts)
        + "只允许使用本章真实文献清单中的作者与年份，不能输出 [lit_xxx] 锚点。"
        "请保留已有的比较、共识、分歧和研究缺口，不要把引用集中堆在段末。"
        "严禁输出 Let me/上一版草稿/自动检查/段落计划/文献清单/内部推理等元话语，"
        "严禁逐条复述 lit_id。\n"
        f"可优先补充的真实作者(年份)示例：{cited_hint}\n\n"
        "【当前草稿】\n"
        f"{draft}\n\n"
        "只输出修复后的章节正文。"
    )
    return _invoke_section_chain(system=system, user=repair_user)


# ============================================================================
# 段落级大纲→成文(2026-11-14 写作重构)
# 根因对应:① "每段对比≥3篇"的机械模板产出目录式罗列 → 改为论证大纲驱动;
# ② 引用密度按池规模定目标(34篇要求16处)与单章容量脱节 → 引用目标改为
#    大纲分配的文献集(claim-driven);③ 整章重写 545s 且必然失败 → 失败
#    只重写单段(~30s)。大纲失败或结构不合法时回退旧单稿路径,行为不变。
# ============================================================================

_OUTLINE_SYSTEM = (
    "你是学术文献综述的结构规划专家。为综述章节设计段落级论证大纲。\n"
    "只输出严格 JSON 对象,格式:\n"
    '{"paragraphs":[{"claim":"本段论点句(一句完整中文陈述)",'
    '"lit_ids":["lit_xxx", ...],"relation":"consensus|controversy|evolution|gap"}]}\n'
    "要求:\n"
    "- 4-7 个段落;第 1 段是领域地图(该主题下研究整体格局),最后 1 段是小结与缺口;\n"
    "- 中间每段一个独立论点,由该段文献支撑,relation 说明文献间关系:"
    "consensus=共识 / controversy=分歧 / evolution=演进脉络 / gap=研究缺口;\n"
    "- lit_ids 只能来自给定清单,按论证需要选取,不凑数;\n"
    "- claim 要写成可被后续段落展开的陈述句,不得是题目复述。"
)


def _outline_json_parse(raw: str) -> list[dict] | None:
    """容错解析大纲 JSON:直接 loads → 最大平衡花括号块 → None。"""
    from llm.client import normalize_model_output

    text = normalize_model_output(raw or "").strip()
    candidates = [text]
    brace_start = text.find("{")
    if brace_start >= 0:
        depth = 0
        end = None
        for i, ch in enumerate(text[brace_start:], start=brace_start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end:
            candidates.append(text[brace_start:end])
    for cand in candidates:
        try:
            data = json.loads(cand)
        except Exception:  # noqa: BLE001
            continue
        paras = data.get("paragraphs") if isinstance(data, dict) else None
        if isinstance(paras, list) and paras:
            return [p for p in paras if isinstance(p, dict)]
    return None


def _build_paragraph_outline(
    *,
    section: SectionSpec,
    topic: str,
    papers: list[Paper],
    allowed: set[str],
    is_comment: bool,
    research_questions: list[str] | None,
    review_goal: str | None,
    notes_block: str | None,
    model: str | None,
) -> list[dict] | None:
    """一次调用产出段落级论证大纲;失败/不合法返回 None(回退旧路径)。"""
    if is_comment or len(papers) < 3:
        return None
    allowed_papers = [p for p in papers if p.lit_id in allowed] or list(papers)
    catalog = "\n".join(
        f"- {p.lit_id} | {p.title} | {', '.join(p.authors)} | {p.year or 'N/A'}"
        for p in allowed_papers
    )
    parts = [
        f"章节标题:{section.title}",
        f"综述主题:{topic}",
    ]
    if research_questions:
        parts.append("研究问题:" + ";".join(research_questions[:5]))
    if review_goal:
        parts.append(f"综述目标:{review_goal}")
    if notes_block:
        parts.append("通读笔记(证据层):\n" + notes_block[:6000])
    parts.append("可选文献清单:\n" + catalog)
    parts.append("请输出段落级论证大纲 JSON。")
    raw = messages_create(
        system=_OUTLINE_SYSTEM,
        user="\n\n".join(parts),
        max_tokens=2500,
        model=model,
        tier="fast",
    )
    paras = _outline_json_parse(raw)
    if not paras or len(paras) < 3:
        return None
    cleaned: list[dict] = []
    for p in paras[:7]:
        claim = str(p.get("claim") or "").strip()
        ids = [x for x in (p.get("lit_ids") or []) if x in allowed]
        relation = str(p.get("relation") or "consensus").strip().lower()
        if relation not in {"consensus", "controversy", "evolution", "gap"}:
            relation = "consensus"
        if not claim:
            continue
        cleaned.append({"claim": claim, "lit_ids": ids, "relation": relation})
    if len(cleaned) < 3:
        return None
    return cleaned


_OUTLINE_PARA_SYSTEM = (
    "你是中文学术文献综述的写作专家,为综述章节撰写其中一个自然段。\n"
    "要求:\n"
    "- 只输出该段正文,纯中文书面学术语体;无标题、无列表、无英文解释;\n"
    "- 紧扣给定的论点句展开,引用给出的文献时用『作者(年份)』夹注;"
    "中文作者如『侯子楠等(2019)』,英文作者如『Galati et al.(2021)』;\n"
    "- 按 relation 组织文献:consensus 综合多篇指向同一结论的证据;"
    "controversy 并陈对立发现;evolution 按时间/方法演进叙述;gap 指出未覆盖处;\n"
    "- 不得罗列(一句一文献依次复述);一个论点统领整段,文献作为证据嵌入;\n"
    "- 350-500 字;严禁输出内部推理、计划、英文自我分析。"
)


def _paper_brief_for_para(p: Paper) -> str:
    authors = ", ".join(str(a).strip() for a in (p.authors or []) if str(a).strip())
    abstract = (p.abstract or "")[:220]
    return (
        f"- 作者:{authors or 'N/A'} | 年份:{p.year or 'N/A'} | 标题:{p.title}\n"
        f"  摘要节选:{abstract or 'N/A'}"
    )


def _write_paragraph_text(
    piece: dict,
    *,
    section: SectionSpec,
    topic: str,
    papers_by_id: dict[str, Paper],
    prev_tail: str | None,
) -> str:
    """按大纲片段写一个自然段;失败抛错由调用方决定保留或重试。"""
    ids = [x for x in piece.get("lit_ids", []) if x in papers_by_id]
    support = "\n".join(
        _paper_brief_for_para(papers_by_id[x]) for x in ids[:6]
    )
    parts = [
        f"章节标题:{section.title}(综述主题:{topic})",
        f"本段论点:{piece.get('claim', '')}",
        f"文献关系:{piece.get('relation', 'consensus')}",
    ]
    if support:
        parts.append("支撑文献:\n" + support)
    else:
        parts.append("该段无指定文献,依据章节主题用通用表述,不得虚构引用")
    if prev_tail:
        parts.append("上一段结尾(衔接用,不要重复):" + prev_tail[-120:])
    parts.append("请输出该段正文。")
    raw = messages_create(
        system=_OUTLINE_PARA_SYSTEM,
        user="\n\n".join(parts),
        max_tokens=1500,
    )
    return normalize_model_output(raw).strip()


# 模型自写的引用占位符(评审41/42 实测 22 处 `[...]` 悬空):
# 夹注回填只绑定"作者(年份)",这些占位没人认领就直通交付。
_CITE_PLACEHOLDER_RE = re.compile(r"\[\s*(?:\.\.\.|…|……)\s*\]")


def _strip_citation_placeholders(text: str) -> str:
    """删除模型自写的 `[...]` 引用占位符,返回 (清洗后文本, 删除数)。

    占位处通常是模型知道要引用但没给作者年;删除后同句的作者年夹注
    仍可被 _inject_citations_by_author_year 绑定,剩余空缺由补证据兜底。
    """
    if not text:
        return text, 0
    cleaned, n = _CITE_PLACEHOLDER_RE.subn("", text)
    return cleaned, n


def _para_has_citation(text: str) -> bool:
    """段落是否含作者(年份)夹注(中文括号/英文括号都认)。"""
    return bool(
        re.search(r"[\u4e00-\u9fffA-Za-z .]{1,40}(?:（|\()[12][0-9]{3}(?:）|\))", text)
    )


def _rewrite_paragraph_text(
    piece: dict,
    draft: str,
    *,
    section: SectionSpec,
    topic: str,
    papers_by_id: dict[str, Paper],
) -> str:
    """单段重写(引用缺失时点名作者);只重写该段。"""
    ids = [x for x in piece.get("lit_ids", []) if x in papers_by_id]
    expected = ";".join(
        _paper_display_author_year(papers_by_id[x]) for x in ids[:6]
    ) or "至少 1 位真实作者(年份)"
    raw = messages_create(
        system=_OUTLINE_PARA_SYSTEM,
        user=(
            f"章节标题:{section.title}(综述主题:{topic})\n"
            f"本段论点:{piece.get('claim', '')}\n"
            f"文献关系:{piece.get('relation', 'consensus')}\n"
            f"你上一版段落缺少『作者(年份)』夹注,必须自然嵌入: {expected}\n"
            f"【上一版】\n{draft[:1500]}\n\n"
            "只输出修正后的该段正文。"
        ),
        max_tokens=1500,
    )
    return normalize_model_output(raw).strip()


def _write_section_via_outline(
    *,
    section: SectionSpec,
    topic: str,
    papers: list[Paper],
    allowed: set[str],
    is_comment: bool,
    research_questions: list[str] | None,
    review_goal: str | None,
    notes_block: str | None,
    model: str | None,
    progress=None,
) -> tuple[str, int] | None:
    """大纲模式写整章:返回 (正文, 大纲引用目标数);不适用返回 None。

    progress: 可选回调,收到 {"step": "outline"/"para", ...} 事件供流式
    面板展示。段落并行写(≤4 线程),交付按大纲顺序拼接。
    """
    outline = _build_paragraph_outline(
        section=section, topic=topic, papers=papers, allowed=allowed,
        is_comment=is_comment, research_questions=research_questions,
        review_goal=review_goal, notes_block=notes_block, model=model,
    )
    if not outline:
        return None
    papers_by_id = {p.lit_id: p for p in papers}
    required = min(16, len({x for piece in outline for x in piece.get("lit_ids", [])}))

    def _emit(evt: dict) -> None:
        if progress is None:
            return
        try:
            progress(evt)
        except Exception:  # noqa: BLE001
            pass

    _emit({"step": "outline", "status": "ok",
           "message": f"论证大纲就绪:{len(outline)} 个段落",
           "paragraphs": len(outline), "required": required})

    workers = min(4, len(outline))
    para_results: dict[int, str] = {}

    def _run_one(idx_piece: tuple[int, dict]) -> tuple[int, str]:
        idx, piece = idx_piece
        _emit({"step": "para", "status": "start", "index": idx,
               "total": len(outline), "message": f"撰写第 {idx + 1}/{len(outline)} 段"})
        text = _write_paragraph_text(
            piece, section=section, topic=topic,
            papers_by_id=papers_by_id, prev_tail=None,
        )
        if not _para_has_citation(text) and piece.get("lit_ids"):
            try:
                text = _rewrite_paragraph_text(
                    piece, text, section=section, topic=topic,
                    papers_by_id=papers_by_id,
                )
            except Exception:  # noqa: BLE001  保留首稿,交由下游兜底
                log.warning("章节 %s 第 %d 段重试失败,保留首稿", section.key, idx)
        _emit({"step": "para", "status": "ok", "index": idx,
               "total": len(outline), "chars": len(text),
               "message": f"第 {idx + 1}/{len(outline)} 段完成({len(text)} 字)"})
        return idx, text

    if workers <= 1:
        for idx_piece in list(enumerate(outline)):
            idx, text = _run_one(idx_piece)
            para_results[idx] = text
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_run_one, ip) for ip in enumerate(outline)]
            for fut in futures:
                idx, text = fut.result()
                para_results[idx] = text

    content = "\n\n".join(
        para_results[i] for i in range(len(outline)) if para_results.get(i)
    )
    return content, required


# 整句边界判定字符(含换行,防止跨段误删)
_SENTENCE_CHARS = "。！？!?；;\n"


def _drop_sentences_with_quotes(content: str, unmatched: list[str]) -> str:
    """刀1最后防线:整句删除仍含未匹配夹注的句子,不留裸作者名。

    unmatched 记录的是「作者(年份)」原文;正文中的对应夹注已被剥离年份只剩
    裸作者名,因此按作者名定位,再扩展到句边界删除整句。
    """
    if not content or not unmatched:
        return content
    spans: list[tuple[int, int]] = []
    for quote in unmatched:
        author = _re_inject.sub(r"[\uff08(]\s*\d{4}\s*[\uff09)]", "", quote).strip()
        if len(author) < 2:
            continue
        for m in _re_inject.finditer(_re_inject.escape(author), content):
            pos = m.start()
            # 向前找句起点(最近句末标点之后)
            start = 0
            for i in range(pos - 1, -1, -1):
                if content[i] in _SENTENCE_CHARS:
                    start = i + 1
                    break
            # 向后找句终点(最近句末标点本身)
            end = len(content)
            for i in range(m.end(), len(content)):
                if content[i] in _SENTENCE_CHARS:
                    end = i + 1
                    break
            # Never delete a substantive paragraph merely because one
            # author/year could not be uniquely resolved.  The old behavior
            # removed the whole sentence, which could erase the model's entire
            # analytical draft and leave only generic fallback lines.  Keep
            # long/meaningful prose (the unmatched year has already been
            # stripped by _inject_citations_by_author_year); only remove a
            # short fragment that is effectively just a dangling citation.
            sentence = content[start:end].strip()
            sentence_without_author = sentence
            for token in (quote, author):
                sentence_without_author = sentence_without_author.replace(token, "")
            chinese_count = len(re.findall(r"[\u4e00-\u9fff]", sentence_without_author))
            if len(sentence) > 120 or chinese_count >= 12:
                continue
            spans.append((start, end))
    if not spans:
        return content
    # 合并重叠区间后从后往前删,避免位置偏移
    merged: list[tuple[int, int]] = []
    for start, end in sorted(set(spans)):
        if merged and start < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    for start, end in reversed(merged):
        content = content[:start] + content[end:]
    return _re_inject.sub(r"\n{3,}", "\n\n", content).strip()


def _finalize_section_result(
    section: SectionSpec,
    raw_content: str,
    allowed: set[str],
    alias_map: dict[str, str],
    is_comment: bool,
) -> SectionResult:
    content = normalize_model_output(raw_content)
    # 模型自写的 [...] 引用占位符在此统一剥离(单一咽喉点,两条写路径都过)
    content, _ph_removed = _strip_citation_placeholders(content)
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
    grades: dict[str, str] | None = None,
    synthesis_map: dict[str, dict] | None = None,
    research_questions: list[str] | None = None,
    review_goal: str | None = None,
    notes_block: str | None = None,
) -> SectionResult:
    """写一章。papers 是本章允许引用的全集,grades 是 lit_id -> 相关性等级。

    v9.9: synthesis_map(theme_key -> 综合条目)与 research_questions/review_goal
    可选注入;缺省行为与旧版完全一致。
    R1'(2026-10-27): notes_block 为通读笔记蒸馏证据层,由调用方
    (orchestrator)经 readthrough.section_notes_block 产出,失败传 None。
    """
    system, user, allowed, alias_map, is_comment = _prepare_section_context(
        section, topic, groups, papers, grades,
        synthesis_map=synthesis_map,
        research_questions=research_questions,
        review_goal=review_goal,
        notes_block=notes_block,
    )
    required = 0 if is_comment else _min_citation_target(len(papers))
    # 大纲模式优先(2026-11-14):段落级论证大纲→逐段成文;不适用回退单稿。
    outline_res = _write_section_via_outline(
        section=section, topic=topic, papers=list(papers), allowed=allowed,
        is_comment=is_comment, research_questions=research_questions,
        review_goal=review_goal, notes_block=notes_block, model=model,
    )
    if outline_res is not None:
        raw, required = outline_res
    else:
        raw = _invoke_section_chain(system=system, user=user, model=model)
    initial_meta = _looks_like_non_article(raw)
    if initial_meta:
        try:
            if outline_res is not None:
                # 大纲模式:整章被污染时回退单稿重生成一次(1 次调用),
                # 不走 3 轮整章重写链(2026-11-14 重构)
                raw = _invoke_section_chain(system=system, user=user, model=model)
            else:
                raw = _repair_section_draft(
                    system=system,
                    user=user,
                    draft=raw[:16000],
                    papers=list(papers),
                    required=required,
                    unmatched=[],
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("章节 %s 检测到模型内部推理泄漏,首轮清理失败: %s", section.key, exc)
        initial_meta = _looks_like_non_article(raw)
    unmatched: list[str] = []
    if not is_comment and papers:
        raw = _remove_model_citation_tokens(raw)
        raw, _auto, unmatched = _inject_citations_by_author_year(raw, list(papers))
        if unmatched:
            raw = _drop_sentences_with_quotes(raw, unmatched)
    result = _finalize_section_result(section, raw, allowed, alias_map, is_comment)
    result.dropped_citations.extend(unmatched)
    needs_repair = initial_meta or _looks_like_non_article(result.content)
    # 大纲模式:引用缺失已由段级验收(单段重写)兜住,不再按密度触发整章重写
    if not outline_res and required and (len(result.citations) < required or unmatched):
        needs_repair = True
    if needs_repair:
        try:
            repaired = _repair_section_draft(
                system=system,
                user=user,
                draft=result.content[:16000],
                papers=list(papers),
                required=required,
                unmatched=unmatched,
            )
            repaired = _remove_model_citation_tokens(repaired)
            repaired, _, repaired_unmatched = _inject_citations_by_author_year(
                repaired, list(papers)
            )
            if repaired_unmatched:
                repaired = _drop_sentences_with_quotes(repaired, repaired_unmatched)
            repaired_result = _finalize_section_result(
                section, repaired, allowed, alias_map, is_comment
            )
            repaired_result.dropped_citations.extend(repaired_unmatched)
            if _article_quality_score(
                repaired_result.content, len(repaired_result.citations)
            ) > _article_quality_score(result.content, len(result.citations)):
                result = repaired_result
        except Exception as exc:  # noqa: BLE001
            log.warning("章节 %s 自动重写失败,保留首稿并使用真实题录补证据: %s", section.key, exc)
        if _looks_like_non_article(result.content):
            cleaned = _strip_obvious_meta_lines(result.content)
            if cleaned and not _looks_like_non_article(cleaned):
                result.content = cleaned
            else:
                result.content = (
                    f"本章围绕“{topic}”梳理相关研究的主要问题、方法差异与证据边界。"
                )
        ensure_section_evidence(result, list(papers), required)

    # ---- v9.11: agent 自我评估 + 迭代改进(不是流水线,是闭环) ----
    result = _self_evaluate_and_improve(
        result, topic, papers, system, user,
        section=section, allowed=allowed, alias_map=alias_map,
        is_comment=is_comment, model=model,
    )
    return result


_SELF_EVAL_SYSTEM = (
    "你是文献综述质量评审专家。评价以下章节是否达到硕士论文文献综述标准。\n"
    "逐项打分(1-10):\n"
    "1. 分析深度: 是否比较了不同文献的观点差异,而非简单罗列\n"
    "2. 逻辑结构: 是否有清晰的论证脉络,而非平铺直叙\n"
    "3. 批判性: 是否指出了现有研究的不足或争议\n"
    "4. 引用自然: 文献引用是否融入论述,而非堆砌\n"
    "5. 语言规范: 是否符合学术写作规范\n"
    "严格输出 JSON:\n"
    '{"scores": {"分析深度": N, "逻辑结构": N, "批判性": N, "引用自然": N, "语言规范": N},\n'
    ' "total": N, "verdict": "合格|需要改进",\n'
    ' "issues": ["具体问题1", "具体问题2"],\n'
    ' "rewrite_instructions": ["改写指令1", "改写指令2"]}\n'
    "total ≤ 18 或 verdict=需要改进 时,必须给出具体的 rewrite_instructions。"
    "只输出 JSON,不要其他文字。"
)

_SELF_EVAL_PASS_TOTAL = 30  # 50 分制,≥30 视为达标
_SELF_EVAL_MAX_ROUNDS = 2


def self_eval_enabled() -> bool:
    """自评闭环总开关(WRITING_SELF_EVAL,默认开;=0 关闭)。"""
    import os
    return os.environ.get("WRITING_SELF_EVAL", "1").strip() != "0"


def _self_evaluate_and_improve(
    result: SectionResult,
    topic: str,
    papers: list,
    system: str,
    user: str,
    *,
    section: SectionSpec,
    allowed: set[str],
    alias_map: dict[str, str],
    is_comment: bool,
    model: str | None = None,
    max_rounds: int = _SELF_EVAL_MAX_ROUNDS,
) -> SectionResult:
    """Agent 自我评估 + 迭代改进:LLM 评审自己的输出,不合格就带着反馈重写。

    与 _retry_section_quality 的区别:后者只做结构检查(模板句/密度),
    本函数用 LLM 做**语义质量评估**(分析深度/批判性/逻辑结构),
    评估→发现问题→带具体指令重写→再评,构成真正的 agent 闭环。
    重写稿走 _finalize_section_result 完整过滤,防止越界引用混入。
    WRITING_SELF_EVAL=0 可关闭(单元测试/快速模式)。
    """
    if not self_eval_enabled():
        return result

    best = result
    for round_no in range(max_rounds):
        content = best.content or ""
        if len(content) < 100:
            break  # 太短不值得评
        try:
            eval_raw = messages_create(
                system=_SELF_EVAL_SYSTEM,
                user=f"研究主题:{topic}\n\n章节正文:\n{content[:12000]}",
                max_tokens=1500,
                temperature=0.2,
                response_format={"type": "json_object"},
                timeout=90.0,
                max_retries=1,
            )
        except Exception as exc:
            log.warning("章节 %s 自评调用失败(第 %d 轮): %s", best.key, round_no + 1, exc)
            break
        try:
            eval_data = json.loads(
                re.sub(r"```(?:json)?|```", "", eval_raw).strip()
            )
            total = int(eval_data.get("total", 10))
            verdict = str(eval_data.get("verdict", ""))
            issues = [str(x) for x in (eval_data.get("issues") or [])]
            instructions = [str(x) for x in (eval_data.get("rewrite_instructions") or [])]
        except Exception as exc:
            log.warning("章节 %s 自评输出解析失败: %s", best.key, exc)
            break
        log.info(
            "章节 %s 自评(第 %d 轮): total=%d verdict=%s issues=%s",
            best.key, round_no + 1, total, verdict, issues[:2],
        )
        if total >= _SELF_EVAL_PASS_TOTAL or verdict == "合格":
            break  # 质量达标,闭环结束
        feedback = "\n".join(f"- {x}" for x in (instructions or issues))
        if not feedback:
            break  # 评审没给出可执行的改进点,重写也无的放矢
        rewrite_user = (
            f"{user}\n\n--- 改写要求 ---\n"
            f"你此前写的初稿经评审发现以下问题:\n{feedback}\n"
            f"请重写本章节解决上述问题。要求:保持引用标注格式 [lit_x] 不变,"
            f"不得引入清单之外的文献;论点之间要有比较和递进,不要罗列。\n"
            f"--- 初稿 ---\n{content[:8000]}"
        )
        try:
            improved_raw = _invoke_section_chain(
                system=system, user=rewrite_user, model=model
            )
        except Exception as exc:
            log.warning("章节 %s 改写调用失败(第 %d 轮): %s", best.key, round_no + 1, exc)
            break
        # 不剥 [lit_x]:重写稿保持锚点格式,合法性由 _finalize_section_result
        # 按 allowed 集过滤(越界引用剥除并记 dropped)
        improved_result = _finalize_section_result(
            section, improved_raw, allowed, alias_map, is_comment
        )
        if not is_comment and papers:
            improved_result.content, _, new_unmatched = _inject_citations_by_author_year(
                improved_result.content, list(papers)
            )
            improved_result.dropped_citations.extend(new_unmatched)
            improved_result.citations = list(dict.fromkeys(
                CITE_RE.findall(improved_result.content)
            ))
        # 接受判据:重写稿不是"非正文",且引用没有塌方(≥初稿一半)
        if _looks_like_non_article(improved_result.content):
            log.warning("章节 %s 改写稿疑似非正文,保留初稿", best.key)
            break
        if len(improved_result.citations) * 2 < len(best.citations):
            log.warning(
                "章节 %s 改写稿引用塌方(%d→%d),保留初稿",
                best.key, len(best.citations), len(improved_result.citations),
            )
            break
        best = improved_result
    return best


def write_section_stream(
    section: SectionSpec,
    topic: str,
    groups: list[Group],
    papers: list[Paper],
    model: str | None = None,
    grades: dict[str, str] | None = None,
    synthesis_map: dict[str, dict] | None = None,
    research_questions: list[str] | None = None,
    review_goal: str | None = None,
    notes_block: str | None = None,
) -> Generator[dict, None, None]:
    """流式写一章:yield 事件 dict,最后附带最终章节结果。

    事件契约(唯一消费方 orchestrator.generate_review_stream):
    - {"kind": "step", "step": str, "status": "start"|"ok"|"fail",
       "message": str, "seconds"?: float} 工具调用过程,供前端步骤面板
    - {"kind": "reasoning", "delta": str} 思考过程增量,仅展示,绝不入正文
    - {"kind": "token", "delta": str} 正文增量
    - {"kind": "done", "result": SectionResult} 终稿
    R1'(2026-10-27): notes_block 为通读笔记蒸馏证据层(调用方产出,
    失败传 None,行为与旧版一致)。
    """
    prepare_t0 = time.monotonic()
    yield {"kind": "step", "step": "prepare_context", "status": "start",
           "message": f"正在构建《{section.title}》的写作上下文与引用约束"}
    system, user, allowed, alias_map, is_comment = _prepare_section_context(
        section, topic, groups, papers, grades,
        synthesis_map=synthesis_map,
        research_questions=research_questions,
        review_goal=review_goal,
        notes_block=notes_block,
    )
    yield {"kind": "step", "step": "prepare_context", "status": "ok",
           "seconds": round(time.monotonic() - prepare_t0, 1),
           "message": f"写作上下文就绪,本章候选文献 {len(papers)} 篇"}
    stream_t0 = time.monotonic()

    # 大纲模式优先(2026-11-14):段落级论证大纲→逐段并行成文。
    # 大纲/段落为整段调用(非 token 流),事件在阶段完成后批量上报;
    # 大纲不可用时回退旧流式路径(逐 token 直播),行为不变。
    outline_events: list[dict] = []
    outline_res = None
    try:
        outline_res = _write_section_via_outline(
            section=section, topic=topic, papers=list(papers), allowed=allowed,
            is_comment=is_comment, research_questions=research_questions,
            review_goal=review_goal, notes_block=notes_block, model=model,
            progress=outline_events.append,
        )
    except Exception as exc:  # noqa: BLE001  大纲链路故障回退旧路径
        log.warning("章节 %s 大纲模式失败,回退单稿流式: %s", section.key, exc)
        outline_res = None
    for evt in outline_events:
        if evt.get("step") == "outline":
            yield {"kind": "step", "step": "outline", "status": evt.get("status", "ok"),
                   "message": evt.get("message", ""), "seconds": None}
        else:
            yield {"kind": "step", "step": "para", "status": evt.get("status", "ok"),
                   "message": evt.get("message", "")}
    if outline_res is not None:
        raw, required = outline_res
        yield {"kind": "step", "step": "llm_stream", "status": "ok",
               "seconds": round(time.monotonic() - stream_t0, 1),
               "message": f"段落级成文完成,共 {len(raw)} 字(大纲 {required} 篇引用目标)"}
    else:
        yield {"kind": "step", "step": "llm_stream", "status": "start",
               "message": "模型正在思考并流式生成正文"}
        raw_parts: list[str] = []

        def _pump() -> Generator[dict, None, None]:
            """单次流式拉取,正文增量累积进 raw_parts。"""
            raw_parts.clear()
            for kind, piece in messages_stream_ex(system=system, user=user, max_tokens=8000, model=model):
                if kind == "reasoning":
                    yield {"kind": "reasoning", "delta": piece}
                    continue
                raw_parts.append(piece)
                yield {"kind": "token", "delta": piece}

        for evt in _pump():
            yield evt
        raw = "".join(raw_parts)
        if _section_stream_anomaly(raw):
            # 网关缓冲/推理回声类异常流(2026-11-12 事故:单块 45766 字符的
            # 系统提示词回声+错位重复流)不可用作正文,整段重新生成一次,
            # 而不是把废料送进夹注回填与重写链再浪费两轮调用
            yield {"kind": "step", "step": "llm_stream", "status": "fail",
                   "message": f"检测到异常输出({len(raw)} 字),正在重新生成本章"}
            for evt in _pump():
                yield evt
            raw = "".join(raw_parts)
        yield {"kind": "step", "step": "llm_stream", "status": "ok",
               "seconds": round(time.monotonic() - stream_t0, 1),
               "message": f"正文流式生成完成,共 {len(raw)} 字"}
    unmatched: list[str] = []
    if not is_comment and papers:
        raw = _remove_model_citation_tokens(raw)
        inject_t0 = time.monotonic()
        yield {"kind": "step", "step": "citation_inject", "status": "start",
               "message": "正在回填作者-年份夹注引用"}
        raw, _, unmatched = _inject_citations_by_author_year(raw, list(papers))
        # 流式版无法隐藏地重写一轮(token 已推送前端),
        # 只做整句删除兜底,不留裸作者名
        if unmatched:
            raw = _drop_sentences_with_quotes(raw, unmatched)
        yield {"kind": "step", "step": "citation_inject",
               "status": "ok",
               "seconds": round(time.monotonic() - inject_t0, 1),
               "message": (f"夹注回填完成,{len(unmatched)} 处未匹配已整句兜底删除"
                           if unmatched else "夹注回填完成,全部命中")}
    result = _finalize_section_result(
        section,
        raw,
        allowed,
        alias_map,
        is_comment,
    )
    result.dropped_citations.extend(unmatched)
    outline_mode = outline_res is not None
    if not outline_mode:
        required = 0 if is_comment else _min_citation_target(len(papers))
        needs_repair = _looks_like_non_article(result.content)
        if required and (len(result.citations) < required or unmatched):
            needs_repair = True
    else:
        # 大纲模式:引用缺失已在段级验收处理(单段重写),不再整章重写;
        # 仅当拼装结果仍是"非正文"(事故防线)才回退单稿重生成一次。
        needs_repair = _looks_like_non_article(result.content)
        if needs_repair:
            yield {"kind": "step", "step": "repair_draft", "status": "start",
                   "message": "大纲拼装结果异常,回退单稿重新生成"}
            try:
                fallback_raw = _invoke_section_chain(system=system, user=user, model=model)
                fallback_raw = _remove_model_citation_tokens(fallback_raw)
                fallback_raw, _, fb_unmatched = _inject_citations_by_author_year(
                    fallback_raw, list(papers)
                )
                if fb_unmatched:
                    fallback_raw = _drop_sentences_with_quotes(fallback_raw, fb_unmatched)
                fb_result = _finalize_section_result(
                    section, fallback_raw, allowed, alias_map, is_comment
                )
                fb_result.dropped_citations.extend(fb_unmatched)
                required = _min_citation_target(len(papers))
                if not _looks_like_non_article(fb_result.content) or _article_quality_score(
                    fb_result.content, len(fb_result.citations),
                ) > _article_quality_score(result.content, len(result.citations)):
                    result = fb_result
                    unmatched = fb_unmatched
                yield {"kind": "step", "step": "repair_draft", "status": "ok",
                       "message": f"单稿重生成完成,本章引用 {len(result.citations)} 处"}
                needs_repair = _looks_like_non_article(result.content)
            except Exception as exc:  # noqa: BLE001
                log.warning("章节 %s 大纲回退单稿失败: %s", section.key, exc)
                needs_repair = False  # 保留大纲拼装结果,如实进入 QA
    if needs_repair and not outline_mode:
        repair_t0 = time.monotonic()
        yield {"kind": "step", "step": "repair_draft", "status": "start",
               "message": f"引用密度或正文形态未达标(要求 {required} 篇),正在自动重写"}
        try:
            repaired = _repair_section_draft(
                system=system,
                user=user,
                draft=result.content[:16000],
                papers=list(papers),
                required=required,
                unmatched=unmatched,
            )
            repaired = _remove_model_citation_tokens(repaired)
            repaired, _, repaired_unmatched = _inject_citations_by_author_year(
                repaired, list(papers)
            )
            if repaired_unmatched:
                repaired = _drop_sentences_with_quotes(repaired, repaired_unmatched)
            repaired_result = _finalize_section_result(
                section, repaired, allowed, alias_map, is_comment
            )
            repaired_result.dropped_citations.extend(repaired_unmatched)
            if _article_quality_score(
                repaired_result.content, len(repaired_result.citations)
            ) > _article_quality_score(result.content, len(result.citations)):
                result = repaired_result
            yield {"kind": "step", "step": "repair_draft", "status": "ok",
                   "seconds": round(time.monotonic() - repair_t0, 1),
                   "message": f"自动重写完成,本章引用 {len(result.citations)} 处"}
        except Exception as exc:  # noqa: BLE001
            log.warning("章节 %s 流式自动重写失败,使用真实题录补证据: %s", section.key, exc)
            yield {"kind": "step", "step": "repair_draft", "status": "fail",
                   "seconds": round(time.monotonic() - repair_t0, 1),
                   "message": f"自动重写失败,改用真实题录补证据:{exc}"}
        if _looks_like_non_article(result.content):
            cleaned = _strip_obvious_meta_lines(result.content)
            result.content = cleaned or "本章围绕研究主题梳理相关研究的主要问题、方法差异与证据边界。"
        ensure_section_evidence(result, list(papers), required)
        if result.density_warning:
            log.warning(
                "章节 %s 引用密度仍不足: %d/%d 篇",
                section.key, len(result.citations), required,
            )
    yield {"kind": "done", "result": result}
