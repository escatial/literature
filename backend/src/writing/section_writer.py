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

import logging
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
            catalog = _build_abstract_catalog(short_list, alias_map)
        else:
            catalog = _build_papers_catalog_alias(short_list, alias_map)

    system = render(
        "literature-review:section",
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


# v6.3 override:按句解析作者(年份),并将真实 lit_id 插入对应句末。
# 该定义位于旧版兼容实现之后,确保运行时使用下面的确定性逻辑。
_LATIN_AUTHOR = r"A-Za-z\u00c0-\u024f"
_LATIN_TOKEN = rf"[{_LATIN_AUTHOR}][{_LATIN_AUTHOR}'-]*(?:\.)?"
_AUTHOR_YEAR_PATTERNS_V63 = (
    _re_inject.compile(
        rf"(?P<authors>[\u4e00-\u9fff]{{2,8}}(?:\s*(?:、|,|和)\s*[\u4e00-\u9fff]{{2,8}})*(?:\s*等)?)[\uff08(]\s*(?P<year>\d{{4}})\s*[\uff09)]"
    ),
    _re_inject.compile(
        rf"(?P<authors>{_LATIN_TOKEN}(?:\s+{_LATIN_TOKEN})?\s+et\s+al\.?)[\uff08(]\s*(?P<year>\d{{4}})\s*[\uff09)]",
        _re_inject.IGNORECASE,
    ),
    _re_inject.compile(
        rf"(?P<authors>{_LATIN_TOKEN}(?:\s+{_LATIN_TOKEN})?\s+(?:and|&)\s+{_LATIN_TOKEN})[\uff08(]\s*(?P<year>\d{{4}})\s*[\uff09)]",
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
        return {
            _normalize_author_key_v63(compact),
            _normalize_author_key_v63(compact[:1]),
        }
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


def _citation_author_keys_v63(author_text: str) -> set[str]:
    text = _re_inject.sub(
        r"\s*(?:等|et\s+al\.?|and|&)\s*",
        ",",
        author_text,
        flags=_re_inject.IGNORECASE,
    )
    parts = [part.strip() for part in _re_inject.split(r"[,、和]", text) if part.strip()]
    keys: set[str] = set()
    for part in parts:
        keys.update(_author_surname_keys_v63(part))
    return keys


def _inject_citations_by_author_year(content, papers):
    """只给命中当前文献池的作者(年份)夹注补真实 lit_id,位置固定在句末。

    返回 (content, cited_ids, dropped_unmatched):
    - cited_ids: 命中并注入锚点的 lit_id(去重,按出现顺序)
    - dropped_unmatched: 未命中文献池的夹注原文(幻觉引用),用于向上游上报;
      正文中这类夹注的"（年份）"会被剥离、仅保留作者名,避免残留伪引用。
    """
    if not content or not papers:
        return content, [], []
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
    for match in unique_matches:
        try:
            year = int(match.group("year"))
        except (TypeError, ValueError):
            continue
        matched_ids: list[str] = []
        for author_key in _citation_author_keys_v63(match.group("authors")):
            candidates = surname_index.get((author_key, year), [])
            if len(candidates) != 1:
                if candidates:
                    log.warning(
                        "正文夹注存在同名同年歧义,跳过自动引用: %s",
                        match.group(0),
                    )
                continue
            lit_id = candidates[0]
            paper = paper_metadata_by_id.get(lit_id)
            if paper is None or not paper.title or not paper.authors or not paper.year:
                log.warning("引用候选缺少完整文献元数据,跳过: %s", match.group(0))
                continue
            if lit_id not in matched_ids:
                matched_ids.append(lit_id)
        if not matched_ids:
            # 幻觉夹注:不在当前文献池,记录原文供上报,并延迟剥离其年份括号
            log.warning("正文夹注未匹配到当前文献池,剥离年份保留作者: %s", match.group(0))
            unmatched_spans.append((match.start(), match.end()))
            continue
        sentence_end = next((pos for pos in sentence_ends if pos >= match.end()), None)
        if sentence_end is None:
            log.warning("正文夹注后没有句末标点, 未注入: %s", match.group(0))
            continue
        existing = set(CITE_RE.findall(content[match.end():sentence_end]))
        for lit_id in matched_ids:
            if lit_id in cited_ids:
                continue
            if lit_id not in existing:
                insertions.setdefault(sentence_end, [])
                if lit_id not in insertions[sentence_end]:
                    insertions[sentence_end].append(lit_id)
            if lit_id not in cited_ids:
                cited_ids.append(lit_id)

    for position in sorted(insertions, reverse=True):
        anchors = "".join(f"[{lit_id}]" for lit_id in insertions[position])
        content = content[:position] + anchors + content[position:]

    # 剥离未命中夹注的"（年份）"部分,保留作者名(如"张欣欣(2016)" -> "张欣欣"),
    # 避免正文残留无法追溯到参考文献的伪引用。从后往前替换,避免位置偏移。
    dropped_unmatched: list[str] = []
    for start, end in reversed(unmatched_spans):
        segment = content[start:end]
        dropped_unmatched.append(segment)
        stripped = _re_inject.sub(r"[\uff08(]\s*\d{4}\s*[\uff09)]", "", segment).strip()
        if stripped:
            content = content[:start] + stripped + content[end:]
    return content, cited_ids, dropped_unmatched


def _remove_model_citation_tokens(content: str) -> str:
    """移除模型自行输出的锚点,避免它们绕过作者/年份映射。"""
    return CITE_RE.sub("", content or "")


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
    if not is_comment and papers:
        raw = _remove_model_citation_tokens(raw)
        raw, _auto, unmatched = _inject_citations_by_author_year(raw, list(papers))
    else:
        unmatched = []
    result = _finalize_section_result(section, raw, allowed, alias_map, is_comment)
    result.dropped_citations.extend(unmatched)
    return result


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
    raw = "".join(raw_parts)
    unmatched: list[str] = []
    if not is_comment and papers:
        raw = _remove_model_citation_tokens(raw)
        raw, _, unmatched = _inject_citations_by_author_year(raw, list(papers))
    result = _finalize_section_result(
        section,
        raw,
        allowed,
        alias_map,
        is_comment,
    )
    result.dropped_citations.extend(unmatched)
    yield "", True, result
