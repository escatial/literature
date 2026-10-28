"""通读笔记层(R1/R1'):先通读全文做笔记,再定主题/写正文。

2026-10-27 用户核心需求:「先通读全文、做好笔记,再定主题;写作同样
先读后写」。此前 90 篇池直接拼 24 万字符大文档丢给定轴调用,MiniMax
对超长语料放弃 JSON 输出,轴规划跌入散文旁路(T01 已用单收口点根治
产出质量)。本模块把「通读」变成显式的第一遍(R1):

- 确定性乱序(seed=hash(topic)):治「两批轴」的语料分段 —— 固定顺序
  下语料按入库批次聚集,模型按段划轴;乱序后每块都是全池缩影。仅排序
  不换 identity,池顺序与 relevance_report 口径不动;
- 按块(≤READTHROUGH_CHUNK_PAPERS 篇 / ≤READTHROUGH_CHUNK_CHARS 字符)
  逐块产出行协议笔记 —— 非 JSON,MiniMax 长上下文下 JSON 纪律不可靠,
  逐行文本天然抗截断;
- R2 定轴输入 = 笔记目录 + 题录短目录(PLAN_NOTES_CHARS 预算),不再
  喂 24 万字符原文;R2 产出一律过 classifier._normalize_axes 单收口点;
- R1'(写作侧):每节写作前通读本节文献蒸馏笔记,作为蒸馏证据层进
  section prompt;笔记行自带「作者(年份)」,经既有夹注回填机制自动
  落成 [lit_xxx] 锚点 —— 锚点纪律、allowed 过滤、QA 闭环全部不动。

无感降级:R1 某块解析失败 → 该块篇目在 R2 目录附原文观点卡;R1'
失败 → 该节用既有证据层。任何失败都不阻塞主流程。
"""
from __future__ import annotations

import hashlib
import logging
import random
import re
from dataclasses import dataclass

from src.retrieval.types import Paper
# 单实例纪律:本模块被 classifier(src 树)与 orchestrator(writing 树)
# 共同消费,内部依赖一律写死 src 前缀,保证 llm_trace 的 contextvar 与
# 本模块的 patch 点(src.writing.readthrough.messages_create)只有一份。
from src.writing.llm_trace import record as _trace_record
from src.llm.client import messages_create

logger = logging.getLogger(__name__)

# R1/R1' 行协议:#<编号>|<作者(年份)>|<研究问题>|<方法>|<结论要点>|<主题线索>
_NOTE_LINE_RE = re.compile(r"^#?\s*(\d{1,4})\s*[.、]?\s*\|(.+)$")


def readthrough_enabled() -> bool:
    """通读笔记唯一总开关:WRITING_READING_NOTES != "0" 即开启(默认开)。

    0 是紧急逃生口:关闭时分类与写作行为与既有链路完全一致。
    """
    import os

    return (os.environ.get("WRITING_READING_NOTES") or "1").strip() != "0"


def _env_int(name: str, default: int) -> int:
    import os

    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def chunk_paper_limit() -> int:
    return max(1, _env_int("READTHROUGH_CHUNK_PAPERS", 12))


def chunk_char_limit() -> int:
    return max(1000, _env_int("READTHROUGH_CHUNK_CHARS", 30000))


def axis_catalog_budget() -> int:
    """R2 定轴目录(笔记+题录)总字符预算。"""
    return max(20000, _env_int("PLAN_NOTES_CHARS", 120000))


@dataclass
class ReadNote:
    """单篇文献的通读笔记(行协议解析产物)。"""

    lit_id: str = ""
    author_year: str = ""
    question: str = ""
    method: str = ""
    finding: str = ""
    clue: str = ""

    def to_compact(self) -> dict:
        """紧凑 dict(进度事件/持久化摘要用,不含 method/finding 全文)。"""
        return {
            "lit_id": self.lit_id,
            "author_year": self.author_year,
            "question": self.question,
            "clue": self.clue,
        }


def _paper_chars(paper: Paper) -> int:
    return len((getattr(paper, "title", "") or "")) + len(
        (getattr(paper, "abstract", "") or "")
    )


def deterministic_shuffle(papers: list[Paper], topic: str) -> list[Paper]:
    """确定性乱序:seed 取 topic 的 md5(跨进程稳定,hash() 不保证)。

    仅重排引用顺序,不复制、不换 lit_id —— 上游口径(relevance_report、
    归组编号)完全不受影响;乱序只发生在 R1 分块内部。
    """
    seed = int(hashlib.md5((topic or "").encode("utf-8")).hexdigest()[:12], 16)
    ordered = list(papers)
    random.Random(seed).shuffle(ordered)
    return ordered


def chunk_for_readthrough(
    papers: list[Paper],
    *,
    max_papers: int | None = None,
    max_chars: int | None = None,
) -> list[list[Paper]]:
    """按篇数与字符双上限把(已乱序的)文献切成通读块,保持相对顺序。

    单篇超字符上限时独占成块(不拆篇、不丢文献)。
    """
    if not papers:
        return []
    limit_papers = max_papers if max_papers is not None else chunk_paper_limit()
    limit_chars = max_chars if max_chars is not None else chunk_char_limit()
    chunks: list[list[Paper]] = []
    current: list[Paper] = []
    current_chars = 0
    for paper in papers:
        cost = _paper_chars(paper)
        if current and (
            len(current) >= limit_papers or current_chars + cost > limit_chars
        ):
            chunks.append(current)
            current, current_chars = [], 0
        current.append(paper)
        current_chars += cost
    if current:
        chunks.append(current)
    return chunks


def _display_author_year(paper: Paper, language: str) -> str:
    """作者(年份)显示串;笔记行与写作夹注同构,供引用锚点回填。"""
    authors = [str(a).strip() for a in (getattr(paper, "authors", None) or []) if str(a).strip()]
    year = getattr(paper, "year", None)
    year_part = f"（{year}）" if year else ""
    if not authors:
        return f"佚名{year_part}" if language == "zh" else f"Anonymous{year_part}"
    first = authors[0]
    if language == "zh":
        name = first if len(first) >= 2 else "佚名"
        if len(authors) > 1:
            name += "等"
        return f"{name}{year_part}"
    tokens = [t for t in re.findall(r"[A-Za-z][A-Za-z\-']*", first) if len(t) >= 2]
    name = tokens[-1] if tokens else (first or "Anonymous")
    return f"{name} et al.{year_part}" if len(authors) > 1 else f"{name}{year_part}"


def _notes_system_prompt(language: str) -> str:
    lang_name = "中文" if (language or "zh") == "zh" else "English"
    return (
        "你是文献通读笔记员。逐篇阅读下面清单中的文献(标题+摘要),"
        f"为每篇输出一行{lang_name}笔记,概括你从摘要中读到的研究要素。\n"
        "输出协议(机器逐行解析,每篇恰好一行,严禁 JSON、严禁解释文字):\n"
        "#<编号>|<作者(年份)>|<研究问题一句话>|<方法>|<结论要点>|<主题线索>\n"
        "- 编号逐字取文献清单行首的 #N;作者(年份)照抄清单里的作者与年份\n"
        "- 每个字段用简短短语概括,不写整段分析;摘要信息不足的字段写\"未提及\"\n"
        "- 除这些行外不得输出任何其他文字\n"
    )


def _chunk_user_prompt(
    chunk: list[Paper], no_of: dict[str, int], abstract_limit: int, language: str
) -> str:
    lines = []
    for paper in chunk:
        no = no_of[paper.lit_id]
        abstract = (getattr(paper, "abstract", "") or "").strip()
        if len(abstract) > abstract_limit:
            abstract = abstract[:abstract_limit] + "…"
        year = getattr(paper, "year", None) or "N/A"
        lines.append(
            f"#{no} | {paper.title} | {_display_author_year(paper, language)} | {year} | 摘要:{abstract or '(缺失)'}"
        )
    return "文献清单(逐篇各输出一行笔记):\n" + "\n".join(lines)


def parse_note_lines(
    raw: str, meta_by_no: dict[str, tuple[str, str]]
) -> list[ReadNote] | None:
    """解析行协议笔记。返回 None 表示没有任何合法行(触发该块降级)。

    部分行可解析时只收合法行(模型漏写个别篇目不拖垮整块);
    编号在清单之外的行(模型幻觉编号)丢弃,绝不猜测映射。
    """
    notes: list[ReadNote] = []
    for line in str(raw or "").replace("｜", "|").splitlines():
        m = _NOTE_LINE_RE.match(line.strip())
        if not m:
            continue
        meta = meta_by_no.get(m.group(1))
        if meta is None:
            continue
        lit_id, fallback_author = meta
        parts = [p.strip() for p in m.group(2).split("|")]
        while len(parts) < 5:
            parts.append("")
        author_year = parts[0] or fallback_author
        notes.append(
            ReadNote(
                lit_id=lit_id,
                author_year=author_year[:60],
                question=parts[1][:120],
                method=parts[2][:100],
                finding=parts[3][:160],
                clue=parts[4][:80],
            )
        )
    return notes or None


def plan_readthrough_notes(
    papers: list[Paper],
    topic: str,
    language: str,
    *,
    progress=None,
) -> tuple[list[Paper], list[ReadNote]]:
    """R1 主流程:确定性乱序 → 分块 → 逐块 LLM 通读产出行协议笔记。

    返回 (乱序全序, 笔记列表)。本函数不抛异常:单块 LLM 失败或解析
    失败只计 failed,该块篇目由 R2 目录附原文观点卡(无感降级)。
    progress 事件(additive):readthrough_started / readthrough_chunk_done
    (带 done/total) / readthrough_done(带紧凑笔记,供持久化)。
    """
    if not papers:
        return [], []
    ordered = deterministic_shuffle(papers, topic)
    no_of = {p.lit_id: i + 1 for i, p in enumerate(ordered)}
    chunks = chunk_for_readthrough(ordered)
    if progress:
        progress({
            "kind": "readthrough_started",
            "papers": len(papers),
            "chunks": len(chunks),
            "message": f"正在分 {len(chunks)} 块通读 {len(papers)} 篇文献并记录笔记...",
        })
    system = _notes_system_prompt(language)
    notes: list[ReadNote] = []
    failed_chunks = 0
    for index, chunk in enumerate(chunks, start=1):
        meta_by_no = {
            str(no_of[p.lit_id]): (
                p.lit_id,
                _display_author_year(p, language),
            )
            for p in chunk
        }
        user = _chunk_user_prompt(chunk, no_of, abstract_limit=800, language=language)
        label = f"chunk{index}"
        try:
            raw = messages_create(
                system=system,
                user=user,
                # 思考预算随块内篇数线性放大(每篇一行笔记约 60-200 token)
                max_tokens=1500 + 250 * len(chunk),
                temperature=0.1,
                timeout=120.0,
                max_retries=1,
            )
        except Exception as exc:  # noqa: BLE001
            failed_chunks += 1
            logger.warning("通读笔记块 %d/%d LLM 失败(该块降级原文直通): %s",
                           index, len(chunks), exc)
            if progress:
                progress({
                    "kind": "readthrough_chunk_done",
                    "done": index, "total": len(chunks),
                    "notes": len(notes), "failed": True,
                })
            continue
        _trace_record("readthrough", label, f"{system}\n\n{user}", raw)
        parsed = parse_note_lines(raw, meta_by_no)
        if parsed is None:
            failed_chunks += 1
            logger.warning("通读笔记块 %d/%d 解析失败(该块降级原文直通)", index, len(chunks))
        else:
            notes.extend(parsed)
        if progress:
            progress({
                "kind": "readthrough_chunk_done",
                "done": index, "total": len(chunks),
                "notes": len(notes),
                "failed": parsed is None,
            })
    if progress:
        progress({
            "kind": "readthrough_done",
            "papers": len(papers),
            "chunks": len(chunks),
            "failed_chunks": failed_chunks,
            "notes": len(notes),
            # 紧凑笔记随事件带出,orchestrator 桥接后写进规划快照(可追溯)
            "items": [n.to_compact() for n in notes],
            "message": (
                f"通读完成:{len(notes)}/{len(papers)} 篇笔记"
                + (f",{failed_chunks} 块降级原文直通" if failed_chunks else "")
            ),
        })
    return ordered, notes


def build_axis_catalog_with_notes(
    ordered: list[Paper],
    notes_by_lit: dict[str, ReadNote],
    budget_chars: int | None = None,
) -> str:
    """R2 定轴语料:笔记目录 + 未通读篇目原文观点卡 + 题录短目录。

    与旧大文档(_build_corpus_document)同位替换:同一模板变量、同一
    调用点;总长受 budget_chars(PLAN_NOTES_CHARS)约束,超限时按
    观点卡 → 笔记次要字段的顺序压缩,题录目录永不压缩。
    """
    budget = budget_chars if budget_chars is not None else axis_catalog_budget()
    no_of = {p.lit_id: i + 1 for i, p in enumerate(ordered)}
    language = "zh"

    note_lines: list[str] = []
    card_lines: list[str] = []
    for paper in ordered:
        no = no_of[paper.lit_id]
        note = notes_by_lit.get(paper.lit_id)
        if note is not None:
            note_lines.append(
                f"#{no} | {note.author_year or _display_author_year(paper, language)} "
                f"| {note.question or '未提及'} | {note.method or '未提及'} "
                f"| {note.finding or '未提及'} | {note.clue or '未提及'}"
            )
        else:
            abstract = (getattr(paper, "abstract", "") or "").strip()
            card = f"{_display_author_year(paper, language)}认为：{abstract[:300] or '(摘要缺失)'}"
            card_lines.append(f"#{no}. {card}")

    title_lines = [
        f"#{no_of[p.lit_id]} | {p.title} | {getattr(p, 'year', None) or 'N/A'}"
        for p in ordered
    ]

    # 预算分配:笔记 60% / 观点卡 25% / 题录 15%;题录永不压缩
    notes_budget = int(budget * 0.6)
    cards_budget = int(budget * 0.25)
    while card_lines and sum(len(x) for x in card_lines) > cards_budget:
        # 压缩观点卡:先截半摘要,仍超则整行丢弃(该篇退化为纯题录)
        card_lines = [
            line[: len(line) // 2] + "…" if "认为：" in line else line
            for line in card_lines[:-1]
        ] if any("认为：" in x for x in card_lines) else []
    while note_lines and sum(len(x) for x in note_lines) > notes_budget:
        note_lines = note_lines[:-1]

    blocks = []
    if note_lines:
        blocks.append(
            "【通读笔记】(每行一篇:#编号|作者(年份)|研究问题|方法|结论要点|主题线索)\n"
            + "\n".join(note_lines)
        )
    if card_lines:
        blocks.append(
            "【未通读篇目原文观点卡】\n" + "\n".join(card_lines)
        )
    blocks.append("【题录目录】\n" + "\n".join(title_lines))
    return "\n\n".join(blocks)


def parse_section_note_lines(
    raw: str, meta_by_no: dict[str, tuple[str, str]]
) -> list[ReadNote] | None:
    """R1' 行协议解析(与 R1 同协议;空 author_year 用清单元数据兜底)。"""
    return parse_note_lines(raw, meta_by_no)


def section_notes_block(
    papers: list[Paper],
    topic: str,
    section_title: str,
    grades: dict[str, str] | None = None,
    language: str = "zh",
) -> str | None:
    """R1':通读单节文献,蒸馏出进 section prompt 的笔记文本块。

    返回 None 表示通读失败(调用方无感降级到既有证据层)。
    本函数不抛异常;一次 LLM 调用,节内篇数 >30 时按相关性截取前 30。
    """
    if not papers:
        return None
    ordered = list(papers)
    if grades:
        rank = {"high": 0, "medium": 1, "low": 2}
        ordered.sort(key=lambda p: rank.get(grades.get(p.lit_id, "medium"), 1))
    ordered = ordered[:30]
    no_of = {p.lit_id: i + 1 for i, p in enumerate(ordered)}
    meta_by_no = {
        str(no_of[p.lit_id]): (p.lit_id, _display_author_year(p, language))
        for p in ordered
    }
    system = _notes_system_prompt(language)
    user = (
        f"章节《{section_title}》(研究主题:{topic})待写作,"
        "先通读下面本节文献,逐篇各输出一行笔记:\n"
        + _chunk_user_prompt(ordered, no_of, abstract_limit=600, language=language)
    )
    try:
        raw = messages_create(
            system=system,
            user=user,
            max_tokens=1500 + 250 * len(ordered),
            temperature=0.1,
            timeout=120.0,
            max_retries=1,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("章节《%s》通读笔记 LLM 失败,降级既有证据层: %s", section_title, exc)
        return None
    _trace_record("section_notes", section_title, f"{system}\n\n{user}", raw)
    parsed = parse_section_note_lines(raw, meta_by_no)
    if parsed is None:
        logger.warning("章节《%s》通读笔记解析失败,降级既有证据层", section_title)
        return None
    by_no = {no_of[n.lit_id]: n for n in parsed}
    lines = []
    for paper in ordered:
        no = no_of[paper.lit_id]
        note = by_no.get(no)
        if note is None:
            continue
        lines.append(
            f"{no}. {note.author_year} | 研究问题:{note.question or '未提及'} "
            f"| 方法:{note.method or '未提及'} | 结论:{note.finding or '未提及'} "
            f"| 线索:{note.clue or '未提及'}"
        )
    if not lines:
        return None
    return (
        "【本节文献通读笔记(先读后写;引用锚点素材——正文用「作者(年份)认为...」"
        "句式引用下列文献,系统会自动回填引用锚点)】\n" + "\n".join(lines)
    )


__all__ = [
    "ReadNote",
    "readthrough_enabled",
    "deterministic_shuffle",
    "chunk_for_readthrough",
    "parse_note_lines",
    "plan_readthrough_notes",
    "build_axis_catalog_with_notes",
    "section_notes_block",
]
