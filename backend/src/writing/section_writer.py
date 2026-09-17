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
import re
import unicodedata
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
    # 刀2:重写后引用密度仍不达标时的标记(SSE 下发给前端展示)
    density_warning: bool = False


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
    "我先检查", "上一版草稿", "自动检查", "内部推理", "思考过程",
)
_FALLBACK_EVIDENCE_PREFIXES = (
    "补充证据显示",
    "与本节问题相邻的研究中",
)


def _is_fallback_only(text: str) -> bool:
    """识别章节是否含有题录兜底句。"""
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return any(line.startswith(_FALLBACK_EVIDENCE_PREFIXES) for line in lines)


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
    return (
        marker_hits >= 1
        or catalog_lines >= 3
        or leaked_lit_ids >= 3
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
        "只写与文献清单一致的作者（年份）夹注,不要输出任何 [lit_xxx] 锚点。"
        "后端会根据作者、年份和真实文献元数据的映射自动在句末注入锚点;"
        "未命中的夹注不生成链接,也不得编造文献。"
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


def _prepare_section_context(
    section: SectionSpec,
    topic: str,
    groups: list[Group],
    papers: list[Paper],
    grades: dict[str, str] | None = None,
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

            "【章节正文里不要写 [lit_xxx] 锚点 — 后端会自动从你写的「作者(年份)」夹注里识别并在句末插入】\n"

            "你只负责写「作者(年份)」夹注(GB/T 7714 强制)。\n"

            "夹注格式:周爱莲等(2020) / 周爱莲,蒋利(2020) / Sun et al.(2025) / Smith(2024)"

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
    user = (
        f"研究主题:{topic}\n\n"
        f"章节:{section.title}\n\n"
        f"分组概览:{', '.join(g.name for g in groups) if groups else '无分组'}\n\n"
        "请输出本章节正文。"
    )
    return system, user, allowed, alias_map, is_comment




# === v6.2: 自动从"作者(年份)"夹注派生 [lit_xxx] 锚点 ===
# LLM 只负责写正文中的“作者(年份)”夹注, 后端以正则查询表查 paper 自动补 [lit_xxx]。
import re as _re_inject

AUTHOR_YEAR_PATTERNS = [
    _re_inject.compile(r'([\u4e00-\u9fff]{2,4})等[\uff08(]\s*(\d{4})\s*[\uff09)]'),
    _re_inject.compile(r'([\u4e00-\u9fff]{2,4})(?:,[\u4e00-\u9fff]{2,4})*[、,]?([\u4e00-\u9fff]{2,4})[\uff08(]\s*(\d{4})\s*[\uff09)]'),
    _re_inject.compile(r'\b([A-Z][a-zA-Z\u00c0-\u017f\.\-]+)\s+et\s+al\.?[\uff08(]\s*(\d{4})\s*[\uff09)]'),
    _re_inject.compile(r'\b([A-Z][a-zA-Z\u00c0-\u017f\.\-]+)\s+and\s+([A-Z][a-zA-Z\u00c0-\u017f\.\-]+)[\uff08(]\s*(\d{4})\s*[\uff09)]'),
    _re_inject.compile(r'\b([A-Z][a-zA-Z\u00c0-\u017f\.\-]+)[\uff08(]\s*(\d{4})\s*[\uff09)]'),
]

def _extract_surname(author_str):
    s = _re_inject.sub(r'\s+et\s+al\.?', '', author_str)
    s = _re_inject.sub(r'\s+and\s+\S+', '', s)
    s = s.strip().rstrip('.')
    if not s: return ''
    if _re_inject.match(r'[A-Za-z\u00c0-\u017f]', s[0]):
        return s.split()[-1] if s.split() else s
    return s[0]

def _build_surname_year_index(papers):
    idx = {}
    for p in papers:
        p_year = int(p.year) if isinstance(p.year, (int, float)) and p.year else 0
        if not p_year: continue
        for a in (p.authors or []):
            s = _extract_surname(a)
            if not s: continue
            idx.setdefault((s, p_year), []).append(getattr(p, 'lit_id', ''))
    return idx

def _inject_citations_by_author_year(content, papers):
    if not content or not papers:
        return content, []
    surname_idx = _build_surname_year_index(papers)
    if not surname_idx:
        return content, []
    paragraphs = content.split('\n')
    out_paragraphs = []
    seen_lit_ids = []
    for para in paragraphs:
        if not para.strip():
            out_paragraphs.append(para)
            continue
        last_lit_id = None
        last_match_end = -1
        for pat in AUTHOR_YEAR_PATTERNS:
            for m in pat.finditer(para):
                groups = m.groups()
                if len(groups) == 3:
                    author = groups[0]; year = int(groups[2])
                else:
                    author = groups[0]; year = int(groups[1])
                surname = _extract_surname(author)
                lit_ids = surname_idx.get((surname, year), [])
                if not lit_ids: continue
                lid = lit_ids[0]
                if m.end() > last_match_end:
                    last_match_end = m.end()
                    last_lit_id = lid
        if last_lit_id and '[' not in para[last_match_end:]:
            tail = para[last_match_end:]
            end_idx = -1
            for ch in ['。', '!', '?', ';', '.']:
                idx = tail.rfind(ch)
                if idx > end_idx: end_idx = idx
            if end_idx > 0:
                insert_pos = last_match_end + end_idx
                para = para[:insert_pos] + f'[{last_lit_id}]' + para[insert_pos:]
                if last_lit_id not in seen_lit_ids:
                    seen_lit_ids.append(last_lit_id)
        out_paragraphs.append(para)
    return '\n'.join(out_paragraphs), seen_lit_ids


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
                # 同一分句只有 1~2 个引用时保留句末注释的传统写法；
                # 当模型在同一句堆叠 3 篇以上时，把每个锚点贴在对应作者
                # 年份之后，避免段末出现无法辨认的长串 [1][2][3]。
                sentence_match_count = sum(1 for other in unique_matches if (
                    next((pos for pos in sentence_ends if pos >= other.end()), None)
                    == sentence_end
                ))
                position = match.end() if sentence_match_count >= 3 else sentence_end
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
    """
    if n_papers <= 0:
        return 0
    if n_papers < 5:
        return min(n_papers, max(2, n_papers // 2 + 1))
    if n_papers < 15:
        return min(n_papers, max(5, (n_papers + 1) // 2))
    # 15 篇以上至少覆盖约七成候选,并设置 48 篇上限控制正文可读性。
    # 代表性文献仍由筛选阶段控制,章节不能只引用少数几篇“代表作”。
    return min(n_papers, max(12, min(48, math.ceil(n_papers * 0.70))))


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


def _paper_display_author_year(paper: Paper) -> str:
    """生成仅用于自动补证据句的作者(年份)显示文本。"""
    authors = [str(a).strip() for a in (paper.authors or []) if str(a).strip()]
    if not authors:
        name = "相关研究"
    elif re.search(r"[\u4e00-\u9fff]", authors[0]):
        if len(authors) == 1:
            name = authors[0]
        elif len(authors) == 2:
            name = f"{authors[0]}和{authors[1]}"
        else:
            name = f"{authors[0]}等"
    else:
        first = authors[0].rstrip(".").split(",", 1)[0].strip().split()[-1]
        name = first + (" et al." if len(authors) > 1 else "")
    return f"{name}（{paper.year}）" if paper.year else name


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
        title = (paper.title or "相关问题").strip().rstrip("。.!！?？")
        additions.append(
            f"与本节问题相邻的研究中，{_paper_display_author_year(paper)}"
            f"围绕《{title}》讨论了相关对象与决策约束[{paper.lit_id}]。"
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
    result.density_warning = len(result.citations) < target
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
) -> SectionResult:
    """写一章。papers 是本章允许引用的全集,grades 是 lit_id -> 相关性等级。"""
    system, user, allowed, alias_map, is_comment = _prepare_section_context(
        section, topic, groups, papers, grades,
    )
    required = 0 if is_comment else _min_citation_target(len(papers))
    raw = _invoke_section_chain(system=system, user=user, model=model)
    initial_meta = _looks_like_non_article(raw)
    if initial_meta:
        try:
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
    if required and (len(result.citations) < required or unmatched):
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
    return result


def write_section_stream(
    section: SectionSpec,
    topic: str,
    groups: list[Group],
    papers: list[Paper],
    model: str | None = None,
    grades: dict[str, str] | None = None,
) -> Generator[tuple[str, bool, SectionResult | None], None, None]:
    """流式写一章:逐 token 返回,最后附带最终章节结果。"""
    system, user, allowed, alias_map, is_comment = _prepare_section_context(
        section, topic, groups, papers, grades,
    )
    raw_parts: list[str] = []
    for piece in messages_stream(system=system, user=user, max_tokens=8000, model=model):
        raw_parts.append(piece)
        yield piece, False, None
    raw = "".join(raw_parts)
    unmatched: list[str] = []
    if not is_comment and papers:
        raw = _remove_model_citation_tokens(raw)
        raw, _, unmatched = _inject_citations_by_author_year(raw, list(papers))
        # 流式版无法隐藏地重写一轮(token 已推送前端),
        # 只做整句删除兜底,不留裸作者名
        if unmatched:
            raw = _drop_sentences_with_quotes(raw, unmatched)
    result = _finalize_section_result(
        section,
        raw,
        allowed,
        alias_map,
        is_comment,
    )
    result.dropped_citations.extend(unmatched)
    required = 0 if is_comment else _min_citation_target(len(papers))
    needs_repair = _looks_like_non_article(result.content)
    if required and (len(result.citations) < required or unmatched):
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
            log.warning("章节 %s 流式自动重写失败,使用真实题录补证据: %s", section.key, exc)
        if _looks_like_non_article(result.content):
            cleaned = _strip_obvious_meta_lines(result.content)
            result.content = cleaned or "本章围绕研究主题梳理相关研究的主要问题、方法差异与证据边界。"
        ensure_section_evidence(result, list(papers), required)
        if result.density_warning:
            log.warning(
                "章节 %s 引用密度仍不足: %d/%d 篇",
                section.key, len(result.citations), required,
            )
    yield "", True, result
