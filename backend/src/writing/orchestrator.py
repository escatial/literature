"""综述写作总控:筛选 → 分类 → 分章写作 → 汇总引文清单。

提供两种入口:
- generate_review:一次性返回完整 ReviewResult(向后兼容,测试用)
- generate_review_stream:生成器,按事件 yield 进度(供 SSE 流式接口)
"""
from __future__ import annotations

import json
import logging
import math
import os
import urllib.request
import uuid
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Generator

import re as _re
from writing.citeproc_renderer import format_citation_via_citeproc as _citeproc_render
from agent.plan_agent import classify_agent_stream
from agent.harness import (
    AgentStage,
    AgentState,
    HarnessPolicy,
    completion_payload,
    inspect_theme_groups,
    run_repair_loop,
)
from retrieval.types import Paper, Source
from screening.llm_filter import screen_batch, screen_batch_stream
from writing import classifier as _classifier_mod
from writing.classifier import Group, classify, _title_similarity, _pool_dominant_language
from writing.relevance import (
    RelevanceScore,
    build_concept_groups,
    build_relevance_report,
    grade_papers,
    sort_by_relevance,
)
from writing.section_writer import (
    SectionResult,
    ensure_section_evidence,
    write_section,
    write_section_stream,
)
from writing.settings import (
    SECTION_COMMENT_INSTRUCTION,
    SECTION_COMMENT_TITLE,
    SECTION_LOCALE_INSTRUCTION_TEMPLATE,
    SECTION_THEME_INSTRUCTION_TEMPLATE,
)
from writing.templates import SectionSpec

log = logging.getLogger(__name__)


_CHINESE_NUMBERS = ["一", "二", "三", "四", "五", "六", "七", "八", "九", "十"]
# 引用配额(可由环境变量覆盖)
def _int_env(name: str, default: int) -> int:
    import os
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        v = int(raw)
    except ValueError:
        return default
    return v


_REFERENCE_LIMIT_MIN = _int_env("WRITING_REF_MIN", 70)        # 综述最低引用数
_REFERENCE_LIMIT_MAX = _int_env("WRITING_REF_MAX", 90)        # 综述最高引用数，与 QA 上限一致
_REFERENCE_LIMIT_DEFAULT = _int_env("WRITING_REF_TARGET", 80) # 综述默认目标数
_CHINESE_RATIO_MIN = 2 / 3                                   # 中文文献占比下限(硬约束 ≥ 2/3)
# 英文上限:1/3 略多约 3% 内,即 ≤ 36.7%。超过即违反"中文占主导"原则
_ENGLISH_RATIO_MAX = 1 / 3 + 1 / 30                           # ≈ 36.7%
_CHINESE_REFERENCE_TARGET = _int_env("WRITING_CHINESE_TARGET", 60)
_ENGLISH_REFERENCE_TARGET = max(0, _REFERENCE_LIMIT_DEFAULT - _CHINESE_REFERENCE_TARGET)


def _format_chinese_index(idx: int) -> str:
    """将 1-based 索引格式化为中文序数(超出十则回退到阿拉伯数字)。"""
    if 1 <= idx <= len(_CHINESE_NUMBERS):
        return _CHINESE_NUMBERS[idx - 1]
    return str(idx)


def build_review_sections(
    classify_mode: str,
    groups: list[Group],
    topic: str = "",
    pool_language: str | None = None,
) -> list[SectionSpec]:
    """根据分类结果构造综述章节。

    标题硬约束(防御性二次过滤,即便 classify 已过滤,这里再过一遍):
      - 必须是 1~12 字的学术名词短语
      - 不接受叙述性连接词("近年来"/"然而"/"综上所述" 等)
      - 不接受英文碎片("英文单词+相关研究"式拼接标题)
    不合格时拒绝进入写作，避免把“主题5”或关键词碎片包装成正式章节。
    """
    instruction_template = (
        SECTION_THEME_INSTRUCTION_TEMPLATE
        if classify_mode == "theme"
        else SECTION_LOCALE_INSTRUCTION_TEMPLATE
    )
    sections: list[SectionSpec] = []
    # 内部导入避免循环依赖
    from writing.classifier import _clean_group_name, _group_name_acceptable

    used_names: set[str] = set()

    for idx, group in enumerate(groups, start=1):
        clean_name = _clean_group_name(group.name)
        from writing.classifier import _zh_theme_name_too_short
        if (
            not _group_name_acceptable(clean_name, pool_language=pool_language)
            or _zh_theme_name_too_short(clean_name, pool_language)
        ) or clean_name in used_names:
            raise ValueError(f"第 {idx} 个主题名称不可靠，请重新划分主题：{group.name or '空名称'}")
        used_names.add(clean_name)
        sections.append(
            SectionSpec(
                key=f"theme_{idx}",
                title=f"{_format_chinese_index(idx)}、{clean_name}",
                instruction=instruction_template.format(name=clean_name),
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
    reference_list: str = ""
    screened_out_ids: list[str] = field(default_factory=list)
    dropped_citations: list[str] = field(default_factory=list)
    # 《文献相关性分级清单》:每篇文献的等级、四维得分、核心匹配点
    relevance_report: dict = field(default_factory=dict)
    # v2 全流程核查报告(可选;WRITING_QA_ENABLED=1 时填充)
    qa_report: dict | None = None


def build_review_blueprint(
    topic: str,
    papers: list[Paper],
    classify_mode: str,
    groups: list[Group],
    screened_out_ids: list[str] | None = None,
    research_questions: list[str] | None = None,
    review_goal: str | None = None,
) -> dict:
    """Build a review design card and evidence matrix from verified metadata."""
    screened = set(screened_out_ids or [])
    group_of = {lid: group.name for group in groups for lid in group.lit_ids}
    paper_by_id = {p.lit_id: p for p in papers}

    def group_diagnostic(group: Group) -> dict:
        """Create a lightweight, evidence-linked explanation of a theme boundary."""
        texts = [
            f"{paper_by_id[lid].title} {paper_by_id[lid].abstract or ''}"
            for lid in group.lit_ids if lid in paper_by_id
        ]
        terms: dict[str, int] = {}
        for text_value in texts:
            tokens = _re.findall(r"[A-Za-z][A-Za-z0-9-]{2,}|[\u4e00-\u9fff]{2,}", text_value.lower())
            for token in set(tokens):
                if token in {
                "研究", "分析", "方法", "问题", "相关", "本文", "结果", "结论", "基于",
                "基于此", "因此", "此外", "然而", "其次", "首先", "最后", "综上", "同时",
                "但是", "由于", "通过", "对于", "随着", "认为", "指出", "提出", "进行",
                "以及", "其中", "之间", "方面", "要素", "影响", "作用", "情况", "现状",
                "study", "studies", "research", "analysis", "method", "methods", "approach",
                "this", "that", "these", "those", "with", "without", "based", "and", "the",
                "are", "for", "from", "into", "is", "it", "its", "of", "on", "or", "to", "using",
                "used", "use", "can", "could", "may", "might", "will", "would", "also", "than",
                "results", "result", "paper", "papers", "work", "works", "shown", "show", "find", "findings",
                "such", "data", "future", "challenge", "challenges", "learning", "new", "two", "one",
                "were", "was", "have", "has", "had", "been", "being", "however", "through",
                "among", "their", "they", "which", "while", "both", "more", "most", "other",
                "others", "between", "during", "about", "how", "why", "where", "but", "not",
                "when", "what", "who", "across", "within", "over", "under", "after", "before",
                }:
                    continue
                terms[token] = terms.get(token, 0) + 1
        repeated = [term for term, count in sorted(terms.items(), key=lambda item: (-item[1], item[0])) if count >= 2][:5]
        method_hits = sorted({
            term for term in ("问卷", "访谈", "实验", "案例", "面板", "回归", "仿真", "算法", "qualitative", "quantitative")
            if any(term.lower() in text_value.lower() for text_value in texts)
        })
        return {
            "shared_problem_terms": repeated,
            "boundary": f"围绕“{'、'.join(repeated[:3])}”共同问题归组" if repeated else "摘要证据不足，需精读确认主题边界",
            "comparison_axes": ["研究问题", "方法与数据", "结论差异"],
            "relation_hint": "同一问题下比较不同方法、场景与结论" if not method_hits else f"可比较方法：{'、'.join(method_hits[:4])}",
        }

    def evidence_extract(abstract: str, title: str) -> dict[str, str]:
        """从题名/摘要抽取可核查的轻量证据，绝不伪造未报告信息。"""
        text = " ".join(part.strip() for part in (title, abstract) if part and part.strip())
        sentences = [s.strip() for s in _re.split(r"(?<=[。！？!?;；.])\s+|\n+", text) if s.strip()]
        question_markers = ("目的", "旨在", "研究", "探讨", "分析", "解决", "关注", "aim", "objective", "investigat", "address", "focus")
        question = next((s for s in sentences if any(m.lower() in s.lower() for m in question_markers)), "")
        methods = [term for term in (
            "问卷", "访谈", "实验", "案例", "面板", "回归", "仿真", "算法", "优化", "模型", "综述",
            "qualitative", "quantitative", "experiment", "simulation", "regression", "survey", "case study",
            "optimization", "algorithm", "machine learning", "deep learning", "reinforcement learning",
        ) if term.lower() in text.lower()]
        sample_patterns = (
            r"(?:样本|案例|数据集|数据|实验对象|研究对象)[：:]?[^。；;\n]{0,80}",
            r"(?:sample|dataset|data|case study|participants?|instances?)[：:]?[^.；;\n]{0,80}",
        )
        sample = ""
        for pattern in sample_patterns:
            match = _re.search(pattern, text, flags=_re.IGNORECASE)
            if match:
                sample = match.group(0).strip()
                break
        finding_markers = ("结果", "发现", "表明", "结论", "显示", "results", "findings", "conclude", "show", "demonstrate")
        finding = next((s for s in sentences if any(m.lower() in s.lower() for m in finding_markers)), "")
        limitation_markers = ("局限", "不足", "挑战", "未来研究", "limitations", "limitation", "challenge", "future work", "further research")
        limitation = next((s for s in sentences if any(m.lower() in s.lower() for m in limitation_markers)), "")
        concept_terms = []
        for pattern in (r"(?:基于|采用|引入|构建|提出|using|based on|framework|model of)\s*[^，。；;\n]{2,40}",):
            concept_terms.extend(m.strip() for m in _re.findall(pattern, text, flags=_re.IGNORECASE))
        return {
            "research_question": question or "题名/摘要未明确研究问题，需精读全文",
            "theory_or_concept": "；".join(dict.fromkeys(concept_terms[:3])) or "题名/摘要未明确理论或核心概念",
            "method": "、".join(dict.fromkeys(methods)) or "题名/摘要未明确研究方法",
            "data_or_sample": sample or "题名/摘要未报告数据或样本",
            "core_findings": finding or (abstract[:240] if abstract else "摘要缺失，无法提取核心发现"),
            "limitation": limitation or "题名/摘要未报告局限，需结合全文批判性评述",
        }
    matrix: list[dict] = []
    for paper in papers:
        abstract = (paper.abstract or "").strip()
        evidence = evidence_extract(abstract, paper.title or "")
        matrix.append({
            "lit_id": paper.lit_id,
            "title": paper.title,
            "authors": paper.authors[:3],
            "year": paper.year or None,
            "source": getattr(paper.source, "value", str(paper.source)),
            **evidence,
            "relation": "已纳入" if paper.lit_id not in screened else "已筛除",
            "group": group_of.get(paper.lit_id, "未归组"),
        })
    years = [p.year for p in papers if p.year]
    research_question_field = topic.strip()
    rqs = [str(q).strip() for q in (research_questions or []) if str(q).strip()]
    return {
        "research_question": research_question_field,
        # v9.9: RQ 与综述目标独立存档,不再拿 topic 顶着 research_question
        "research_questions": rqs,
        "review_goal": (review_goal or "").strip() or None,
        "scope": {
            "time_range": [min(years) if years else None, max(years) if years else None],
            "sources": sorted({getattr(p.source, "value", str(p.source)) for p in papers}),
            "included_count": len(papers),
            "screened_out_count": len(screened),
        },
        "inclusion_criteria": ["与研究主题存在实质关联", "具备可核查的题名、作者或摘要信息", "能够支撑主题比较或研究现状判断"],
        "exclusion_criteria": ["主题无实质关联", "摘要与元数据不足以判断研究内容", "重复记录或无法核验的条目"],
        "organization": {
            "mode": "主题式" if classify_mode == "theme" else "国内外对照式",
            "principle": "以主题/研究对象聚类，在章节内部比较方法、结论与局限",
            "groups": [{"name": g.name, "count": len(g.lit_ids), "lit_ids": g.lit_ids, **group_diagnostic(g)} for g in groups],
        },
        "matrix": matrix,
    }


def _finish_screening(
    papers: list[Paper],
    decisions: list[dict],
    enforce_source_mix: bool = True,
) -> tuple[list[Paper], list[str]]:
    # #region debug-point A:screening-state
    try:
        _dbg_env = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), ".dbg", "auto-cnki-retrieval.env")
        _dbg_cfg = dict(line.strip().split("=", 1) for line in open(_dbg_env, encoding="utf-8") if "=" in line)
        urllib.request.urlopen(urllib.request.Request(_dbg_cfg.get("DEBUG_SERVER_URL", ""), data=json.dumps({"sessionId": _dbg_cfg.get("DEBUG_SESSION_ID", "auto-cnki-retrieval"), "runId": "pre", "hypothesisId": "A", "location": "orchestrator._finish_screening", "msg": "[DEBUG] screening state", "data": {"input_count": len(papers), "decision_count": len(decisions), "sources": dict(Counter(str(p.source.value) for p in papers)), "decision_relevant": sum(d.get("relevant") is True for d in decisions), "decision_abstract_ok": sum(d.get("abstract_ok") is True for d in decisions)}}).encode(), headers={"Content-Type": "application/json"}), timeout=1).read()
    except Exception:
        pass
    # #endregion
    kept_ids = {
        decision["lit_id"]
        for decision in decisions
        if decision.get("relevant") is True and decision.get("abstract_ok") is True
    }
    screened_out = [paper.lit_id for paper in papers if paper.lit_id not in kept_ids]
    kept_papers = [paper for paper in papers if paper.lit_id in kept_ids]
    if not kept_papers:
        # #region debug-point B:screening-empty
        try:
            urllib.request.urlopen(urllib.request.Request(_dbg_cfg.get("DEBUG_SERVER_URL", ""), data=json.dumps({"sessionId": _dbg_cfg.get("DEBUG_SESSION_ID", "auto-cnki-retrieval"), "runId": "pre", "hypothesisId": "B", "location": "orchestrator._finish_screening", "msg": "[DEBUG] screening empty", "data": {"kept_count": 0, "chinese_count": 0}}).encode(), headers={"Content-Type": "application/json"}), timeout=1).read()
        except Exception:
            pass
        # #endregion
        reason_counts = Counter(
            str(decision.get("reason") or "未说明原因")
            for decision in decisions
        )
        reason_summary = "；".join(
            f"{reason} {count} 篇"
            for reason, count in reason_counts.most_common(5)
        )
        raise ValueError(
            f"文献筛选后没有可用于写作的文献：{reason_summary}。"
            "请重新检索包含完整摘要的文献，或检查历史记录是否来自旧版快照"
        )
    if enforce_source_mix:
        _enforce_chinese_source_mix(kept_papers)
    return kept_papers, screened_out


def _required_chinese_count(papers: list[Paper]) -> int:
    chinese_sources = {Source.CNKI, Source.USER_IMPORTED}
    chinese_count = sum(paper.source in chinese_sources for paper in papers)
    return max(0, _CHINESE_REFERENCE_TARGET - chinese_count)


def _allow_chinese_supplement(papers: list[Paper]) -> bool:
    """仅对正常规模综述池自动补充中文文献。

    用户明确只选择少量文献时，补充检索会改变其输入集合并破坏可追溯性；
    小池子应原样完成写作，并由 QA 提示数量/语种比例不足。
    """
    return len(papers) >= 10


def _rank_by_relevance(
    papers: list[Paper],
    scores: dict[str, RelevanceScore] | None,
) -> list[Paper]:
    """把候选按相关性等级降序排布，低相关排到末位仅作兜底。

    对应「优先使用高相关性文献」原则：进入配额的顺序即高→中→低。
    """
    if not scores:
        return papers
    return sort_by_relevance(papers, scores)


def _select_reference_quota(
    papers: list[Paper],
    scores: dict[str, RelevanceScore] | None = None,
) -> list[Paper]:
    """按综述引用区间选择文献：总量 70-90 篇，中文不少于 2/3，英文可略超 1/3。

    业务规则：
    - 综述引用总数在 [70, 90] 之间，受候选数限制。
    - 中文文献占比必须 ≥ 2/3。
    - 英文文献占比允许略多于 1/3（即不严格限制），但优先让中文占满。
    - 传入 scores 时，中英文各自内部按相关性等级+得分降序取用，
      低相关文献只有在高/中相关不足以凑满配额时才会被取到。
    """
    chinese_sources = {Source.CNKI, Source.USER_IMPORTED}
    ranked = _rank_by_relevance(papers, scores)
    chinese = [paper for paper in ranked if paper.source in chinese_sources]
    english = [paper for paper in ranked if paper.source not in chinese_sources]
    total_available = len(chinese) + len(english)
    # 目标总量：在 [70, 90] 之间，受候选数限制
    target_total = min(_REFERENCE_LIMIT_MAX, max(_REFERENCE_LIMIT_MIN, total_available))
    # 在该总量下，中文占比刚好 ≥ 2/3 所需的最少中文数
    min_chinese = math.ceil(target_total * _CHINESE_RATIO_MIN)
    # 若候选中文不足，则降总量到候选数能支撑的最小总量
    while target_total > _REFERENCE_LIMIT_MIN and len(chinese) < min_chinese:
        target_total -= 1
        min_chinese = math.ceil(target_total * _CHINESE_RATIO_MIN)
    # 中文只占「满足 2/3 硬约束」所需的份额,剩余名额留给英文;
    # 英文候选不足时再由中文回填,避免中文候选充裕就把英文全部挤掉。
    chinese_used = min(len(chinese), max(min_chinese, target_total - len(english)))
    english_used = min(len(english), target_total - chinese_used)
    backfill = target_total - chinese_used - english_used
    if backfill > 0:
        chinese_used = min(len(chinese), chinese_used + backfill)
    selected = chinese[:chinese_used]
    selected.extend(english[:english_used])
    return selected[: min(_REFERENCE_LIMIT_MAX, len(selected))]


def _select_theme_writing_pool(
    papers: list[Paper], groups: list[Group],
) -> tuple[list[Paper], list[Group]]:
    """Explicitly sample a representative writing pool when a caller requests it.

    The interactive two-stage workflow no longer calls this helper implicitly:
    after the user confirms themes, every screened paper must remain available
    to the chapter writers. Keeping this helper preserves a bounded-pool option
    for batch callers that consciously opt into representative sampling.
    """
    if not groups:
        return papers, groups
    by_id = {paper.lit_id: paper for paper in papers}

    # 主题只按研究问题边界形成，语言是文献属性，不能成为拆分或强制混组
    # 条件。此前为了让每组“中英文混合”而跨簇搬运文献，直接破坏了主题
    # 纯度。这里只合并样本不足、难以形成比较论证的小簇。
    min_group_size = 5
    max_by_size = max(1, len(papers) // min_group_size)
    max_groups = max(1, min(len(groups), max_by_size))

    def group_evidence(group: Group) -> str:
        return " ".join(
            f"{by_id[lid].title} {(by_id[lid].abstract or '')[:160]}"
            for lid in group.lit_ids[:5]
            if lid in by_id
        )

    while len(groups) > max_groups:
        small_idx = min(range(len(groups)), key=lambda i: len(groups[i].lit_ids))
        source = groups.pop(small_idx)
        if not groups:
            groups.append(source)
            break
        target = max(groups, key=lambda g: _title_similarity(group_evidence(source), group_evidence(g)))
        target.lit_ids.extend(lid for lid in source.lit_ids if lid not in target.lit_ids)

    invalid = [g for g in groups if len(g.lit_ids) < min_group_size]
    while invalid and len(groups) > 1:
        source = invalid.pop(0)
        if source not in groups:
            continue
        groups.remove(source)
        target = max(groups, key=lambda g: (_title_similarity(group_evidence(source), group_evidence(g)), len(g.lit_ids)))
        target.lit_ids.extend(lid for lid in source.lit_ids if lid not in target.lit_ids)
        invalid = [g for g in groups if len(g.lit_ids) < min_group_size]
    if invalid:
        raise ValueError("相关文献不足，无法形成至少 5 篇、可进行比较评述的主题簇")
    target = min(_REFERENCE_LIMIT_DEFAULT, _REFERENCE_LIMIT_MAX, len(papers))
    total = sum(len(g.lit_ids) for g in groups) or 1
    quotas = [min(len(g.lit_ids), max(min_group_size, round(target * len(g.lit_ids) / total))) for g in groups]
    while sum(quotas) > target:
        idx = max(range(len(groups)), key=lambda i: (quotas[i], len(groups[i].lit_ids)))
        if quotas[idx] <= 1:
            break
        quotas[idx] -= 1
    while sum(quotas) < target:
        candidates = [i for i, g in enumerate(groups) if quotas[i] < len(g.lit_ids)]
        if not candidates:
            break
        idx = max(candidates, key=lambda i: len(groups[i].lit_ids) - quotas[i])
        quotas[idx] += 1

    chosen_ids: set[str] = set()
    selected_groups: list[Group] = []
    for group, quota in zip(groups, quotas):
        candidates = [by_id[lid] for lid in group.lit_ids if lid in by_id]
        # 摘要完整、被引更多的文献优先成为该主题的代表文献。
        candidates.sort(key=lambda p: (bool((p.abstract or "").strip()), p.cited_by_count or 0), reverse=True)
        # 语言不改变主题归属；仅在同一主题内部按质量排序选代表文献。
        ids = [p.lit_id for p in candidates[:quota] if p.lit_id not in chosen_ids]
        if ids:
            chosen_ids.update(ids)
            selected_groups.append(Group(name=group.name, lit_ids=ids))
    selected = [paper for paper in papers if paper.lit_id in chosen_ids]
    return selected, selected_groups


def _validate_reference_quota(papers: list[Paper]) -> None:
    chinese_sources = {Source.CNKI, Source.USER_IMPORTED}
    chinese_count = sum(paper.source in chinese_sources for paper in papers)
    if len(papers) >= _REFERENCE_LIMIT_MIN and chinese_count * 3 < 2 * len(papers):
        # 设计变更:e2e/批量场景下若 CNKI 因 cookie/IP 限流返回 0 篇,
        # 不应让整篇综述生成失败。降级为 warning,继续生成(并在 review.md
        # / qa.json 里报告该问题)。
        log.warning(
            "可用文献未达到 2/3 中文占比的硬约束:综述需要至少 %d 篇文献且"
            "中文占比不低于 2/3,当前 %d 篇中仅有 %d 篇中文文献。继续生成综述。",
            _REFERENCE_LIMIT_MIN, len(papers), chinese_count,
        )


def _retrieve_chinese_papers(topic: str, target_count: int) -> list[Paper]:
    """调用现有知网检索能力补足中文文献缺口。

    设计变更:e2e 脚本/前端批量写作场景下,CNKI 爬虫偶尔因为 cookie 失效 / IP 限流
    返回 0 篇(saved=0)而非抛错。这种情况下原版会 raise 让整篇综述生成失败。
    改为:知网真没拿到时返回空 list + 打 warning,由上游配额校验抛具体错误
    (或者 e2e 用 LLM fallback 在中英文比例不达标时降级)。
    """
    if target_count <= 0:
        return []
    from api.cnki import _cnki_papers_from_pool
    try:  # 与面板 crawler_admin 同源：automation.* 优先（避免双单例分裂）
        from automation.cnki_adapter import run_cnki_full_auto
    except ImportError:  # 仅 backend/src 不在 sys.path 的独立脚本兜底
        from src.automation.cnki_adapter import run_cnki_full_auto
    from retrieval.query_planner import plan_query_strings

    # v9.7:给本次补充一个专属 pool_task_id,落库与回读都按它隔离。
    # 此前不传 → 落库 task_id=None、回读捞全库 cnki 行,历史任务/别次
    # 补充的中文文献会混进本次写作输入(数据跨任务泄漏)。
    supplement_task_id = f"cnki-supplement-{uuid.uuid4().hex[:12]}"
    try:
        planned = plan_query_strings(topic)
        queue = __import__("asyncio").Queue()
        result = __import__("asyncio").run(
            run_cnki_full_auto(
                topic=topic,
                expert_query=planned["queries_cnki"][0],
                expert_queries=planned["queries_cnki"],
                target_count=min(target_count, 500),
                max_pages=10,
                queue=queue,
                pool_task_id=supplement_task_id,
            )
        )
    except Exception as exc:
        # plan 失败或 asyncio 异常 → 安静降级,让上游决定是否继续
        log.warning("_retrieve_chinese_papers 知网任务异常: %s", exc)
        return []

    if result.get("status") != "succeeded" or not result.get("saved"):
        log.warning(
            "_retrieve_chinese_papers 知网未返回可用文献: %s",
            result.get("error") or result.get("reason") or "saved=0",
        )
        return []
    return _cnki_papers_from_pool(supplement_task_id)


def _apply_relevance_quota(
    topic: str,
    candidates: list[Paper],
    kept_papers: list[Paper],
    decisions: list[dict],
) -> tuple[list[Paper], list[str], dict]:
    """四维度打分 → 三级分级 → 按等级优先排布 → 取配额 → 出分级清单。

    v8.3:先由 LLM 把主题拆成中英概念组,关键词重合度走语义级匹配,
    修复「英文文献字面恒 0 分」「权利/权力寻租一字之差全灭」。
    返回 (入选文献, 被排除的 lit_id, 《文献相关性分级清单》)。
    """
    concept_groups = build_concept_groups(topic)
    scores = grade_papers(topic, kept_papers, decisions, concept_groups)
    quota_papers = _select_reference_quota(kept_papers, scores)
    _validate_reference_quota(quota_papers)
    quota_ids = {paper.lit_id for paper in quota_papers}
    report = build_relevance_report(quota_papers, scores)
    report["excluded_low_relevance"] = [
        row for row in build_relevance_report(
            [p for p in kept_papers if p.lit_id not in quota_ids], scores,
        )["items"]
        if row["grade"] == "low"
    ]
    log.info(
        "相关性分级完成：高相关 %d 篇，中相关 %d 篇，低相关 %d 篇（入选 %d 篇）",
        report["summary"]["high"], report["summary"]["medium"],
        report["summary"]["low"], len(quota_papers),
    )
    excluded = [p.lit_id for p in candidates if p.lit_id not in quota_ids]
    return quota_papers, excluded, report


def _grade_map(report: dict) -> dict[str, str]:
    """从分级清单提取 lit_id -> 等级,供写作阶段约束引用优先级。"""
    return {row["lit_id"]: row["grade"] for row in report.get("items", [])}


def _dedupe_pool(papers: list[Paper]) -> tuple[list[Paper], list[str]]:
    """同题同年重复条目只保留一条(accuracy ACCURACY_DUPLICATE 的源头治理)。

    _dedup_papers 只按 lit_id 去重;同一条文献从两个源各导一次(同题同年
    不同 lit_id)会一路进池,终局 QA 判 FAIL。按(题名归一,年份)再挡一层。
    """
    seen: set[tuple[str, int]] = set()
    kept: list[Paper] = []
    dup_ids: list[str] = []
    for p in papers:
        title = (p.title or "").strip().lower()
        key = (title, int(p.year or 0))
        if title and key in seen:
            dup_ids.append(p.lit_id)
            continue
        if title:
            seen.add(key)
        kept.append(p)
    return kept, dup_ids


def _backfill_user_raw_citation(papers: list[Paper]) -> int:
    """USER_IMPORTED 缺 raw_citation 时用其自身元数据合成 GB/T 条目。

    accuracy 核查把「中文条目缺原始引文」判 FAIL(无法核验真实性);用户
    粘贴题录时常漏引文串。合成只用用户已提供的作者/题名/期刊/年份,
    不编造数据;元数据残缺合成不出的,交给 QA 如实报告。
    """
    filled = 0
    for p in papers:
        if p.source != Source.USER_IMPORTED or (p.raw_citation or "").strip():
            continue
        authors = [a.strip() for a in (p.authors or []) if a and a.strip()]
        title = (p.title or "").strip()
        if not authors or not title or not p.year:
            continue
        author_part = ", ".join(authors[:3]) + ("等" if len(authors) > 3 else "")
        journal_part = f"{p.journal.strip()}, " if (p.journal or "").strip() else ""
        p.raw_citation = f"{author_part}. {title}[J]. {journal_part}{p.year}."
        filled += 1
    return filled


def _screen_papers(
    topic: str,
    papers: list[Paper],
    do_screening: bool,
) -> tuple[list[Paper], list[str], dict]:
    """执行写作前唯一允许的文献入口筛选。"""
    if not do_screening:
        raise ValueError("写作必须先完成文献筛选,不允许跳过筛选阶段")
    if not papers:
        return [], [], build_relevance_report([], {})

    # v9.6:同步筛选路径此前漏调 _dedup_papers(流式路径 _screen_papers_stream 有),
    # 重复 lit_id 进入配额并触发 QA 判 FAIL——此处与流式路径对齐
    deduped, dropped = _dedup_papers(papers)
    if dropped:
        log.warning(
            "screening 输入去重:移除 %d 个重复 lit_id(%d → %d)",
            len(dropped), len(papers), len(deduped),
        )
    papers = deduped

    decisions = screen_batch(papers, topic)
    kept_papers, screened_out = _finish_screening(
        papers, decisions, enforce_source_mix=False,
    )
    missing_chinese = _required_chinese_count(kept_papers) if _allow_chinese_supplement(papers) else 0
    if missing_chinese <= 0:
        return _apply_relevance_quota(topic, papers, kept_papers, decisions)

    log.info("中文文献不足，自动检索补充 %d 篇", missing_chinese)
    added = _retrieve_chinese_papers(topic, missing_chinese)
    by_id = {paper.lit_id: paper for paper in papers}
    for paper in added:
        by_id.setdefault(paper.lit_id, paper)
    merged = list(by_id.values())
    merged_decisions = screen_batch(merged, topic)
    kept, merged_screened_out = _finish_screening(
        merged, merged_decisions, enforce_source_mix=False,
    )
    return _apply_relevance_quota(topic, merged, kept, merged_decisions)


def _dedup_papers(papers: list[Paper]) -> tuple[list[Paper], list[str]]:
    """按 lit_id 去重,保留信息更完整的那一篇。

    「更完整」的判定:摘要长度更长 + 必要字段(title / authors / journal / year / doi)更全。
    完全相同的 paper 保留第一条(行为兼容)。

    Returns:
        (deduped_papers, dropped_lit_ids)
    """
    best: dict[str, Paper] = {}
    first_seen: dict[str, Paper] = {}
    for p in papers:
        if p.lit_id not in first_seen:
            first_seen[p.lit_id] = p
            best[p.lit_id] = p
            continue
        cur = best[p.lit_id]
        if _paper_completeness(p) > _paper_completeness(cur):
            best[p.lit_id] = p
    dropped: list[str] = []
    seen: set[str] = set()
    deduped: list[Paper] = []
    for p in papers:
        if p.lit_id in seen:
            dropped.append(p.lit_id)
            continue
        seen.add(p.lit_id)
        deduped.append(best[p.lit_id])
    return deduped, dropped


def _paper_completeness(p: Paper) -> int:
    """用一个简单分值量化 paper 的字段完整度,用于去重时择优。"""
    score = len((p.abstract or "").strip())
    if p.title.strip():
        score += 50
    if p.authors:
        score += 30
    if p.journal.strip():
        score += 20
    if p.year:
        score += 10
    if p.doi:
        score += 30
    if p.volume:
        score += 5
    if p.issue:
        score += 5
    if p.pages:
        score += 5
    return score


def _screen_papers_stream(
    topic: str,
    papers: list[Paper],
    do_screening: bool,
) -> Generator[dict, None, tuple[list[Paper], list[str], dict]]:
    """流式筛选文献,在每个批次完成后把进度交给 SSE。"""
    if not do_screening:
        raise ValueError("写作必须先完成文献筛选,不允许跳过筛选阶段")
    if not papers:
        return [], [], build_relevance_report([], {})

    # 数据层去重:同一 lit_id 多次入库时(前端重复勾选 / 多次翻页累积),
    # 在进入 screening 前按 lit_id 收敛,保留信息更完整的那一条。
    deduped, dropped = _dedup_papers(papers)
    if dropped:
        log.warning(
            "screening 输入去重:移除 %d 个重复 lit_id(%d → %d)",
            len(dropped), len(papers), len(deduped),
        )
    papers = deduped

    decisions: list[dict] | None = None
    for event in screen_batch_stream(papers, topic):
        if event["status"] == "finished":
            decisions = event["results"]
        else:
            yield event
    if decisions is None:
        raise ValueError("筛选未产生完整结果")
    kept_papers, screened_out = _finish_screening(
        papers, decisions, enforce_source_mix=False,
    )
    missing_chinese = _required_chinese_count(kept_papers) if _allow_chinese_supplement(papers) else 0
    if missing_chinese <= 0:
        return _apply_relevance_quota(topic, papers, kept_papers, decisions)

    yield {
        "status": "started",
        "batch": 0,
        "total_batches": 0,
        "processed": 0,
        "total": missing_chinese,
        "message": f"中文文献比例不足，正在自动补充至少 {missing_chinese} 篇中文文献...",
    }
    added = _retrieve_chinese_papers(topic, missing_chinese)
    by_id = {paper.lit_id: paper for paper in papers}
    for paper in added:
        by_id.setdefault(paper.lit_id, paper)
    merged = list(by_id.values())
    merged_decisions: list[dict] | None = None
    for event in screen_batch_stream(merged, topic):
        if event["status"] == "finished":
            merged_decisions = event["results"]
        else:
            yield event
    if merged_decisions is None:
        raise ValueError("自动补充中文文献后未产生完整筛选结果")
    kept, merged_screened_out = _finish_screening(
        merged, merged_decisions, enforce_source_mix=False,
    )
    return _apply_relevance_quota(topic, merged, kept, merged_decisions)


def _enforce_chinese_source_mix(papers: list[Paper]) -> None:
    """确保进入写作的筛选结果中,中文来源至少占三分之二。"""
    chinese_sources = {Source.CNKI, Source.USER_IMPORTED}
    chinese_count = sum(paper.source in chinese_sources for paper in papers)
    total_count = len(papers)
    # #region debug-point C:source-mix
    try:
        urllib.request.urlopen(urllib.request.Request(_dbg_cfg.get("DEBUG_SERVER_URL", ""), data=json.dumps({"sessionId": _dbg_cfg.get("DEBUG_SESSION_ID", "auto-cnki-retrieval"), "runId": "pre", "hypothesisId": "C", "location": "orchestrator._enforce_chinese_source_mix", "msg": "[DEBUG] source mix check", "data": {"kept_count": total_count, "chinese_count": chinese_count, "required_ratio": "2/3", "sources": dict(Counter(str(p.source.value) for p in papers))}}).encode(), headers={"Content-Type": "application/json"}), timeout=1).read()
    except Exception:
        pass
    # #endregion
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


def _paper_payload(p: Paper) -> dict:
    """Paper → 前端镜像 dict(与 api.writing.PaperIn 字段对齐)。

    两阶段模式下 plan 阶段把它随 plan_complete 下发,前端暂存后于阶段2原样回传。
    """
    return {
        "lit_id": p.lit_id,
        "source": p.source.value if isinstance(p.source, Source) else str(p.source),
        "title": p.title,
        "authors": list(p.authors or []),
        "journal": p.journal,
        "year": p.year,
        "volume": p.volume,
        "issue": p.issue,
        "pages": p.pages,
        "abstract": p.abstract,
        "doi": p.doi,
        # 兜底 None→"":域类型是 str 默认 "",但 dataclass 无运行时校验,
        # 上游塞 None 会让阶段2 PaperIn 反序列化直接炸
        "source_url": p.source_url or "",
        # 两阶段模式:阶段1下发、阶段2回传,GB/T 7714 引文渲染依赖该字段
        "raw_citation": p.raw_citation,
        "cited_by_count": p.cited_by_count,
        "journal_level": p.journal_level,
        "relevance_score": p.relevance_score,
    }


def _sanitize_confirmed_groups(
    confirmed_groups: list[Group], papers: list[Paper],
) -> list[Group]:
    """用户确认后的主题分组防御性清洗(两阶段模式的阶段2入口)。

    - lit_id 不在当前文献池内的引用剔除(避免章节写作引用悬空)
    - 空组、空名组剔除
    - 全部为空时抛错:用户把组删光了就没有写作输入
    """
    known = {p.lit_id for p in papers}
    groups: list[Group] = []
    assigned: set[str] = set()
    for grp in confirmed_groups:
        ids = [i for i in grp.lit_ids if i in known and i not in assigned]
        if grp.name and ids:
            groups.append(Group(name=grp.name, lit_ids=ids))
            assigned.update(ids)
    if not groups:
        raise ValueError("确认后的主题分组为空(或文献不在当前文献池),无法写作")
    return groups


# === 全流程核查 hook(v2) ===
# 触发节点:apply_citation_numbering 之后,final summary 之前。
# 通过环境变量 WRITING_QA_ENABLED=1 启用(默认开启),可在测试中显式关闭避免被 fail_fast 阻断。
def _qa_enabled() -> bool:
    raw = os.environ.get("WRITING_QA_ENABLED", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _placeholder_section_result(spec, topic: str, papers: list[Paper]):
    """LLM 章节生成全失败时的占位章节:基于真实题录生成最小可读章节。

    返回值与 write_section 同结构(SectionResult)。citations 用真实 lit_id,
    内容由论文题录拼装而成,质量远低于 LLM 章节,但能让流程继续并保留可追溯的引用。
    """
    from writing.section_writer import SectionResult

    # v9.6:文献述评章节(key="comment")按写作规则不得引用任何文献(section_writer
    # _build_section_role 明确禁止 [lit_xxx] 与作者(年份)夹注),占位内容不能塞
    # 全池题录清单——否则凭空扩大参考文献列表
    if getattr(spec, "key", "") == "comment":
        body = (
            f"本章为「{topic}」综述的文献述评。由于 LLM 章节生成在本次运行中不可用,"
            "无法完成对国内外研究共识、分歧与 Research Gap 的综合评述。"
            "请结合前述章节人工核验补充,或重新运行写作流程。"
        )
        return SectionResult(
            key=getattr(spec, "key", "comment"),
            title=getattr(spec, "title", "文献述评"),
            content=body,
            citations=[],
            dropped_citations=[],
        )

    lines = [
        f"本章围绕「{topic}」展开,基于已检索到的 {len(papers)} 篇真实文献题录构建综述。",
        "由于 LLM 章节生成在本次运行中不可用,本章节以结构化题录形式呈现,"
        "用于保留可追溯的引文锚点;请参考最终引用列表核验文献真实性。",
        "",
        "本章节候选文献(按相关度排序):",
    ]
    citations: list[str] = []
    for i, p in enumerate(papers, 1):
        authors = ", ".join((p.authors or [])[:3])
        if len(p.authors or []) > 3:
            authors += " 等"
        meta = f"[{p.lit_id}] 《{p.title or '(无题名)'}》/{authors or '未知作者'}"
        if p.journal:
            meta += f"/{p.journal}"
        if p.year:
            meta += f",{p.year}"
        lines.append(f"{i}. {meta}")
        if p.lit_id:
            citations.append(p.lit_id)

    body = "\n".join(lines)
    return SectionResult(
        key=getattr(spec, "key", "section"),
        title=getattr(spec, "title", "本章节"),
        content=body,
        citations=citations,
        dropped_citations=[],
    )


def _evidence_recovery_section_result(spec, topic: str, papers: list[Paper]):
    """生成可交付的确定性证据稿，替代截断/题录清单式模型输出。

    该恢复稿只使用当前章节真实文献的题名、作者和年份，每条证据单独
    绑定一个 ``lit_id``，再由统一编号流程转换为 [N]。它不是领域模板，
    因而适用于任意主题；作用是保证 LLM 连续失败时仍不会交付半句话或
    未绑定的作者年份引用。
    """
    from writing.section_writer import (
        SectionResult, _paper_display_author_year, _title_topic_phrase,
    )

    if getattr(spec, "key", "") == "comment":
        return _placeholder_section_result(spec, topic, papers)
    evidence = []
    citations: list[str] = []
    for paper in papers:
        if not paper.lit_id:
            continue
        author_year = _paper_display_author_year(paper)
        if not author_year:
            continue  # 首作者不可用(脏数据缩写/单字母),跳过,不产出伪作者名
        phrase = _title_topic_phrase(paper.title or "")
        evidence.append(
            f"{author_year}研究了{phrase}相关议题，题录已核验[{paper.lit_id}]。"
        )
        citations.append(paper.lit_id)
    if evidence:
        intro = (
            f"本章围绕“{getattr(spec, 'title', topic)}”组织证据。以下研究均来自当前文献池，"
            "按题名与摘要所呈现的问题、方法和应用对象进行归纳；不同文献之间的差异，"
            "应结合其完整全文进一步核验。"
        )
        body = intro + "\n\n" + "\n\n".join(evidence)
    else:
        body = f"本章围绕“{topic}”展开，但当前章节没有可绑定的文献证据。"
    return SectionResult(
        key=getattr(spec, "key", "section"),
        title=getattr(spec, "title", "本章节"),
        content=body,
        citations=citations,
        dropped_citations=[],
    )


_RECOVERY_DRAFT_SIGNATURES = (
    "题录已核验",
    "提供了相邻问题的研究证据",
    "讨论了相关对象与决策约束",
)


def _recovery_draft_ratio(sections: list[SectionResult]) -> float:
    """主题章节中「纯题录恢复稿」的占比(非述评章)。

    判定为恢复稿(任一):
    - 含恢复稿导语"以下研究均来自当前文献池"(该导语是恢复稿独有的指纹);
    - ≥3 句且 8 成以上句子是补证据模板句。
    只补了两三条证据句的正文薄段不算恢复稿(那由 QA FALLBACK_ONLY 明细
    覆盖),避免把可交付的薄稿误升级成硬失败。
    """
    theme_sections = [s for s in sections if s.key != "comment"]
    if not theme_sections:
        return 0.0
    bad = 0
    for section in theme_sections:
        content = section.content or ""
        if "以下研究均来自当前文献池" in content:
            bad += 1
            continue
        sentences = [
            x.strip() for x in _re.split(r"[。！？!?\n]", content) if x.strip()
        ]
        if len(sentences) < 3:
            continue
        hits = sum(
            1 for sentence in sentences
            if any(sig in sentence for sig in _RECOVERY_DRAFT_SIGNATURES)
        )
        if hits / len(sentences) >= 0.8:
            bad += 1
    return bad / len(theme_sections)


def _polish_and_verify_section(
    result: SectionResult,
    papers: list[Paper],
    *,
    progress=None,
) -> SectionResult:
    """润色后的章节级核查+自动修复闭环(用户策略:agent 应自我纠错)。

    流程: humanize(3轮,引用保持) → article_lint → 自动修复 → 再查
    不合格的段落在此修复,而不是等到最终 QA 才暴露。
    修复失败时保留原稿(density_warning 如实标记),不伪造通过。
    """
    from humanizer.agent import humanize_three_rounds
    from qa.article_lint import check_article_text, repair_article_text

    # -- Phase 1: humanize 3 轮(引用保持由 humanize 内部保证) --
    def llm_call(system, user, max_tokens=8000, **kw):
        from llm.client import messages_create
        return messages_create(system, user, max_tokens=max_tokens)

    try:
        polished, _diffs = humanize_three_rounds(text=result.content or "", caller=llm_call)
        if polished and polished != result.content:
            result.content = polished
    except Exception as exc:
        log.warning("章节 %s 润色失败,保留原文: %s", result.key, exc)

    # -- Phase 2: 章节级核查 + 自动修复 --
    for attempt in range(2):
        check = check_article_text([result], papers=papers)
        if check.status.value == "pass":
            break
        # 确定性修复(article_lint 自带)
        repair_article_text([result], papers=papers)
        re_check = check_article_text([result], papers=papers)
        if re_check.status.value == "pass":
            break
        if attempt == 0:
            # 还有问题 → 再修一轮(修复函数是幂等的,二次调用安全)
            repair_article_text([result], papers=papers)

    # -- Phase 3: 最终校验 --
    final = check_article_text([result], papers=papers)
    result.density_warning = final.status.value == "fail"
    return result


def _section_requires_quality_retry(result: SectionResult) -> bool:
    """判断章节是否仍是占位/题录兜底稿，需要重新请求模型。"""
    try:
        from writing.section_writer import _is_fallback_only, _looks_like_non_article
        return bool(
            _looks_like_non_article(result.content or "")
            or _is_fallback_only(result.content or "")
            or result.density_warning
        )
    except Exception:
        return bool(result.density_warning)


def _retry_section_quality(
    *, spec, topic: str, groups: list[Group], papers: list[Paper], grades: dict[str, str],
    result: SectionResult,
    synthesis_map: dict[str, dict] | None = None,
    research_questions: list[str] | None = None,
    review_goal: str | None = None,
) -> SectionResult:
    """在 QA 前对低质量章节做一次独立重写，避免把半成品直接交付。"""
    if not _section_requires_quality_retry(result):
        return result
    try:
        from writing.section_writer import write_section, ensure_section_evidence
        from writing.section_writer import _article_quality_score
        old_score = _article_quality_score(result.content or "", len(result.citations))
        # 模型偶发输出截断句，连续重试两次；每次都重新做证据绑定。
        for attempt in range(2):
            retry = write_section(
                spec, topic, groups, papers, grades=grades,
                synthesis_map=synthesis_map,
                research_questions=research_questions,
                review_goal=review_goal,
            )
            ensure_section_evidence(retry, papers)
            new_score = _article_quality_score(retry.content or "", len(retry.citations))
            if new_score >= old_score and not _section_requires_quality_retry(retry):
                log.info("章节 %s 通过 QA 前自动重写(attempt=%d)", getattr(spec, "key", "?"), attempt + 1)
                return retry
            old_score = max(old_score, new_score)
    except Exception as exc:  # noqa: BLE001
        log.warning("章节 %s QA 前自动重写失败: %s", getattr(spec, "key", "?"), exc)
    # 重试仍失败时，使用真实题录证据稿，保证正文完整、作者年份与锚点
    # 一一对应，并让最终 QA 能明确反映剩余的元数据风险。
    recovered = _evidence_recovery_section_result(spec, topic, papers)
    if not _section_requires_quality_retry(recovered):
        log.warning("章节 %s 使用确定性证据稿完成恢复", getattr(spec, "key", "?"))
        return recovered
    return result


_QA_LLM_FIXABLE_CODES = frozenset({
    # article_lint 中确定性修复(runner 内)处理不了的语义类问题,
    # 需要 LLM 带着问题清单定向重写章节
    "ARTICLE_EMPTY_SECTION",
    "ARTICLE_META_REASONING",
    "ARTICLE_FALLBACK_ONLY",
    "ARTICLE_RAW_NUMBER_ANCHOR",
    "ARTICLE_UNBOUND_AUTHOR_YEAR",
    "ARTICLE_PARAGRAPH_CITATION_DENSITY",
    "ARTICLE_CITATION_DENSITY",
    "ARTICLE_JOURNAL_LEAK",
    "ARTICLE_SPLIT_AUTHOR",
    "ARTICLE_STUCK_AUTHOR_YEAR",
    "ARTICLE_DENSE_CITATIONS",
})

_QA_REPAIR_SYSTEM = (
    "你是学术综述修订专家。修订一段文献综述章节,修复质量体检发现的具体问题。\n"
    "硬规则:\n"
    "1. 只输出修订后的完整正文,不要任何解释、标题或列表符号。\n"
    "2. 保留全部现有 [lit_xxx] 引用锚点(不得删除、不得改写编号);"
    "可以从本章文献清单补充新锚点,严禁使用清单之外的 lit_id。\n"
    "3. 每个自然段至少引用 3 篇不同文献:作者年份夹注后紧跟 [lit_xxx] 锚点。\n"
    "4. 删除一切模板化兜底句(如「补充证据显示」「题录已核验」),"
    "把题录信息改写为连贯的比较性论述。\n"
    "5. 不要出现期刊名称、模型内部推理、书名号;引用标注置于句末标点之前。"
)


def _llm_repair_failed_sections(
    sections: list[SectionResult],
    papers: list[Paper],
    qa_summary: dict,
) -> list[dict]:
    """QA FAIL 后的 LLM 定向重写:按章节聚集体检问题,逐章带问题清单修复。

    确定性修复(runner 内)只能处理标点/粘连/期刊泄漏类排版问题;段落引用
    密度不足、模板兜底句、裸数字锚点、内部推理泄漏必须改写正文才能消除。
    这是终局核查的 agent 闭环:QA 发现 → 定向修复 → 复查,而不是把 84 项
    问题原样抛给用户人工复核。
    """
    issues_by_section: dict[str, list[dict]] = {}
    for r in qa_summary.get("results", []):
        if r.get("status") != "fail":
            continue
        for issue in r.get("issues", []):
            if (issue.get("code") in _QA_LLM_FIXABLE_CODES
                    and issue.get("severity") == "fail"
                    and issue.get("section_key")):
                issues_by_section.setdefault(issue["section_key"], []).append(issue)
    if not issues_by_section:
        return []

    from llm.client import messages_create
    from writing.section_writer import (
        CITE_RE,
        _article_quality_score,
        _looks_like_non_article,
    )

    # runner 交付出来的正文是 [N] 编号形态;LLM 修复要求 [lit_x] 锚点形态
    restore_internal_citation_anchors(sections)
    by_id = {p.lit_id: p for p in papers}
    repairs: list[dict] = []
    for s in sections:
        s_issues = issues_by_section.get(s.key)
        if not s_issues or s.key == "comment":
            continue
        old_content = s.content or ""
        old_cites = list(dict.fromkeys(CITE_RE.findall(old_content)))
        # 本章文献清单:已引用的排前,再补池内未引用的(控制 prompt 体积)
        cited_set = set(old_cites)
        catalog = [by_id[c] for c in old_cites if c in by_id]
        catalog += [p for p in papers if p.lit_id not in cited_set][:40]
        cat_lines = [
            f"{p.lit_id} | {'、'.join((p.authors or [])[:3])}({p.year}) | {(p.title or '')[:60]}"
            for p in catalog
        ]
        issue_lines = []
        for i in s_issues[:12]:
            snippet = (i.get("snippet") or "").strip().replace("\n", " ")[:120]
            line = f"- [{i.get('code')}] {i.get('message', '')}"
            if snippet:
                line += f" | 片段:{snippet}"
            issue_lines.append(line)
        issue_block = "\n".join(issue_lines)
        catalog_block = "\n".join(cat_lines)
        user = (
            f"章节《{s.title}》正文:\n{old_content[:14000]}\n\n"
            f"质量体检发现的问题(逐条修复):\n{issue_block}\n\n"
            f"本章文献清单(lit_id | 作者(年份) | 题名):\n{catalog_block}"
        )
        try:
            repaired_raw = messages_create(
                system=_QA_REPAIR_SYSTEM, user=user,
                max_tokens=8000, temperature=0.2,
                timeout=180.0, max_retries=1,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("章节 %s QA 修复 LLM 调用失败: %s", s.key, exc)
            continue

        # 越界锚点剥除(编造的 lit_id 不得混入),再做接受判据
        cleaned = _re.sub(
            r"\[(lit_[A-Za-z0-9_]+|hash:[A-Za-z0-9_]+)\]",
            lambda m: m.group(0) if m.group(1) in by_id else "",
            repaired_raw or "",
        ).strip()
        new_cites = list(dict.fromkeys(CITE_RE.findall(cleaned)))
        if not cleaned or _looks_like_non_article(cleaned):
            log.warning("章节 %s QA 修复稿疑似非正文,保留原稿", s.key)
            continue
        if len(new_cites) < max(2, int(len(old_cites) * 0.8)):
            log.warning(
                "章节 %s QA 修复稿引用塌方(%d→%d),保留原稿",
                s.key, len(old_cites), len(new_cites),
            )
            continue
        if (_article_quality_score(cleaned, len(new_cites))
                < _article_quality_score(old_content, len(old_cites))):
            log.warning("章节 %s QA 修复稿质量分下降,保留原稿", s.key)
            continue
        s.content = cleaned
        # 立即按出现顺序同步 citations:后续 restore/renumber 依赖它反解 [N],
        # 滞后同步会把新锚点误映射回旧文献
        s.citations = new_cites
        repairs.append({
            "section_key": s.key,
            "changes": [f"LLM 定向修复 {len(s_issues)} 项体检问题"],
        })
    return repairs


def _run_post_write_qa(
    *,
    papers: list[Paper],
    sections: list[SectionResult],
    reference_list: str,
    grade_map: dict[str, str],
) -> dict:
    """执行全流程核查;失败时由 on_fail 回调决定是否抛错。

    为了不在写作主链路引入硬依赖,本函数对 ImportError 宽容。
    """
    try:
        from qa.hooks import run_post_write_qa
        from qa.runner import QARunner
        from qa.rules import QARuleSet, QuotaThresholds, default_rule_set
        from llm.client import (
            get_fallback_order,
            get_provider_health,
            select_providers,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("qa 模块不可用,跳过全流程核查: %s", exc)
        return {"skipped": True, "reason": str(exc)}

    rule_overrides = os.environ.get("WRITING_QA_RULESET_OVERRIDES")
    ruleset = default_rule_set
    if rule_overrides:
        # 允许通过 env json 覆盖示例:{ "required_pass_rate": 0.95 }
        try:
            overrides = json.loads(rule_overrides)
            merged = {**ruleset.to_dict(), **overrides}
            # to_dict()/asdict 会把嵌套的 quota 转成 plain dict;
            # 不还原的话 check_literature_quota 访问 thresholds.total_min
            # 抛 AttributeError → 该项被 runner 记为 SKIPPED(静默失效)
            if isinstance(merged.get("quota"), dict):
                merged["quota"] = QuotaThresholds(**merged["quota"])
            ruleset = QARuleSet(**merged)
        except Exception as exc:  # noqa: BLE001
            log.warning("解析 WRITING_QA_RULESET_OVERRIDES 失败,使用默认: %s", exc)
            ruleset = default_rule_set

    def _soft_on_fail(summary) -> None:  # noqa: ANN001
        # 失败不会静默交付：外层会先执行确定性/LLM 修复并重新核查。
        log.warning("QA 首轮未通过，准备自动修复: %s",
                    ", ".join(r.name for r in summary.results
                              if getattr(r.status, "value", r.status) == "fail"))

    payload = None
    auto_repairs = []
    # 三级修复闭环(用户策略:QA 失败必须先自动修复,修不掉的才上报):
    #   每轮 QA 内 runner 先做确定性修复(标点/粘连/期刊泄漏/夹注绑定);
    #   仍 FAIL 时第一轮补一遍显式确定性修复,第二轮动用 LLM 定向重写;
    #   轮次用尽仍失败的问题如实进报告,不伪造通过。
    from qa.article_lint import repair_article_text
    for attempt in range(3):
        payload = run_post_write_qa(
            runner=QARunner(ruleset),
            papers=papers,
            sections=sections,
            reference_list=reference_list,
            grades=grade_map,
            on_fail=_soft_on_fail,
        )
        summary = payload.get("summary", {}) if payload else {}
        internal_repairs = []
        for item in summary.get("results", []) if isinstance(summary, dict) else []:
            if item.get("check_id") == "article_lint_001":
                internal_repairs = item.get("metrics", {}).get("auto_repairs", []) or []
                break
        if internal_repairs:
            auto_repairs.extend(internal_repairs)
        # 正文发生变化后，编号和参考文献映射必须重新生成，防止报告读取旧引用。
        if internal_repairs:
            reference_list, _ = apply_citation_numbering(sections, papers)
        if summary.get("overall") != "fail":
            break
        if attempt == 0 and not internal_repairs:
            # runner 没修出东西:显式再跑一遍确定性修复(幂等),有变化就复查
            repairs = repair_article_text(sections, papers=papers)
            if repairs:
                auto_repairs.extend(repairs)
                reference_list, _ = apply_citation_numbering(sections, papers)
                continue
        if attempt <= 1:
            # LLM 定向重写仍不达标的章节(段落引用密度/兜底句/裸锚点等)
            llm_repairs = _llm_repair_failed_sections(sections, papers, summary)
            if llm_repairs:
                auto_repairs.extend(llm_repairs)
                reference_list, _ = apply_citation_numbering(sections, papers)
                continue
        break  # 无可修复项或轮次用尽,如实上报剩余问题
    if payload is None:
        return {"skipped": True, "reason": "QA 未执行"}
    if auto_repairs:
        payload.setdefault("summary", {}).setdefault("auto_repairs", []).extend(auto_repairs)
    return payload


_THEME_STRATEGIES = ("default", "minimal_abstracts", "local_fallback")


def _run_theme_synthesis(
    topic: str,
    groups: list[Group],
    papers: list[Paper],
    research_questions: list[str] | None,
) -> tuple[dict[str, dict], dict]:
    """主题综合:全池证据抽取 + 按主题共识/争议/方法差异/空白综合。

    失败由调用方捕获后降级为直接写作(空 synthesis_map),不阻塞主流程。
    """
    from evidence.extractor import extract_pool_evidence
    from synthesis.agent import build_all_syntheses
    evidence_map = extract_pool_evidence(papers, topic)
    synthesis_map, meta = build_all_syntheses(
        groups, papers, evidence_map, topic, research_questions,
    )
    meta = dict(meta or {})
    meta["evidence_extracted"] = sum(
        1 for e in evidence_map.values() if e.get("extracted")
    )
    return synthesis_map, meta


def _synthesis_active(classify_mode: str, groups: list[Group]) -> bool:
    """主题综合总开关:仅主题模式且开启 WRITING_SYNTHESIS(默认开)时执行。

    locale(国内外对照)模式没有可比较的主题轴,综合层暂不参与。
    """
    if classify_mode != "theme" or not groups:
        return False
    from synthesis.agent import synthesis_enabled
    return synthesis_enabled()


def _run_theme_classification_loop(
    topic: str,
    papers: list[Paper],
    *,
    progress=None,
    check_stop=None,
) -> tuple[list[Group], dict]:
    """主题阶段 harness:分类 → 通用结构核查 → 升级策略重试 → 确定性兜底。

    三级策略逐级降级:default(完整摘要) → minimal_abstracts(短摘要) →
    local_fallback(本地确定性语义聚类,不依赖 LLM)。任何一级通过通用
    结构检查即交付;全部失败时返回 best effort 部分结果和 blocked 状态,
    由调用方决定是否以结构检查错误终止流程。LLM 的真实失败原因会通过
    progress 事件和 meta 一并带出,前端可直接展示。
    """
    state = AgentState(task_id=f"theme:{topic}:{len(papers)}", topic=topic)
    # require_pass=False:不让 AgentLoopError 以裸异常形式逃逸;流程是否
    # 终止由调用方根据 meta.status 和结构检查 issues 决定,错误文案可控。
    policy = HarnessPolicy(max_attempts=3, require_pass=False, allow_partial_output=True)
    strategies: list[str] = []
    best_groups: list[Group] = []
    last_issues: list[str] = []

    def _classify(strategy: str) -> list[Group]:
        strategies.append(strategy)
        if check_stop:
            check_stop()
        try:
            return _classifier_mod.classify_by_theme(
                papers, topic, progress=progress, _strategy=strategy,
            )
        except RuntimeError:
            # 用户停止等控制流信号不许被吞掉
            raise
        except Exception as exc:  # noqa: BLE001
            if progress:
                progress({
                    "kind": "theme_strategy_failed",
                    "strategy": strategy,
                    "message": f"主题分类策略 {strategy} 失败：{exc}",
                })
            return []

    def _track_best(groups: list[Group]) -> None:
        nonlocal best_groups
        covered = len({lid for g in groups for lid in g.lit_ids})
        best_covered = len({lid for g in best_groups for lid in g.lit_ids})
        if groups and covered > best_covered:
            best_groups = list(groups)

    def produce(_state: AgentState) -> list[Group]:
        return _classify(_THEME_STRATEGIES[0])

    def inspect(groups, _state: AgentState):
        nonlocal last_issues
        checks = inspect_theme_groups(groups, papers)
        _track_best(list(groups))
        last_issues = [issue for check in checks for issue in check.issues]
        if progress:
            progress({
                "kind": "theme_validation_failed" if last_issues else "theme_validation_passed",
                "message": (
                    "主题方案结构核查未通过，正在升级重试策略..."
                    if last_issues else "主题方案结构核查通过。"
                ),
                "issues": last_issues,
                "strategy": strategies[-1] if strategies else None,
            })
        return checks

    def repair(_groups, _checks, _state: AgentState):
        # 下一级策略;最后一级由 classify_by_theme 内部走确定性本地聚类。
        strategy = _THEME_STRATEGIES[min(_state.attempt, len(_THEME_STRATEGIES) - 1)]
        if progress:
            progress({
                "kind": "theme_strategy_escalated",
                "strategy": strategy,
                "message": f"主题分类升级到策略 {strategy} 重试...",
            })
        return _classify(strategy)

    output = run_repair_loop(
        state, stage=AgentStage.THEME, policy=policy,
        produce=produce, inspect=inspect, repair=repair,
    )
    groups = list(output) if isinstance(output, list) else []
    if not groups and best_groups:
        # 全部策略都未通过结构核查时,把覆盖最高的部分结果一并带出,
        # 供前端展示与人工确认,而不是静默丢弃。
        groups = best_groups
        if progress:
            progress({
                "kind": "partial_groups",
                "reason": "all_strategies_failed",
                "groups": [{"name": g.name, "lit_ids": list(g.lit_ids)} for g in groups],
                "covered": len({lid for g in groups for lid in g.lit_ids}),
                "expected": len(papers),
            })
    if state.error and "用户已停止写作" in str(state.error):
        raise RuntimeError("用户已停止写作")
    meta = completion_payload(state)
    meta["strategies"] = strategies
    meta["issues"] = last_issues
    return groups, meta


def plan_review_stream(
    topic: str,
    papers: list[Paper],
    classify_mode: str,
    stop_event=None,
    progress=None,
    research_questions: list[str] | None = None,
    review_goal: str | None = None,
) -> Generator[str, None, None]:
    """两阶段·阶段1 主题规划:筛选 + 相关性分级 + 分类,停在主题确认点,不写正文。

    plan_complete 事件携带:
    - groups: 分类结果(前端展示并允许编辑:改组名/删组/调整文献归属)
    - section_titles: 按当前分组预览的章节标题
    - papers: 筛选后文献(含自动补充的中文文献),前端暂存供阶段2原样回传
    - screened_out_ids: 被剔除文献
    阶段2 由 generate_review_stream(confirmed_groups=...) 接力。
    """
    try:
        def check_stop():
            if stop_event is not None and stop_event.is_set():
                raise RuntimeError("用户已停止写作")

        check_stop()
        yield _sse_event("start", {
            "topic": topic,
            "total_papers": len(papers),
            "classify_mode": classify_mode,
        })
        # 无论采用何种章节组织方式，都必须先执行同一套相关性和摘要质量
        # 筛选。章节“按主题”不等于候选文献可以绕过纳入/排除标准；此前
        # 主题模式直接聚类全部候选，导致只共享机器学习等泛词的无关文献混入。
        yield _sse_event("screening_started", {
            "total": len(papers),
            "message": "正在进行主题相关性与摘要质量筛选,每批 36 篇,最多 4 批并行...",
        })
        screening_iterator = iter(_screen_papers_stream(topic, papers, True))
        while True:
            check_stop()
            try:
                screening_progress = next(screening_iterator)
            except StopIteration as finished:
                papers, screened_out, relevance_report = finished.value
                break
            yield _sse_event("screening_progress", screening_progress)
        yield _sse_event("screening_done", {
            "kept": len(papers),
            "screened_out": screened_out,
        })
        yield _sse_event("relevance_report", relevance_report)
        yield _sse_event("classify_started", {
            "classify_mode": classify_mode,
            "total": len(papers),
        })
        check_stop()
        theme_meta: dict | None = None
        if classify_mode == "theme":
            # 主题模式走 harness 循环:default → minimal_abstracts →
            # local_fallback 三级策略,任一级通过结构核查即交付;
            # 全部失败时以结构检查错误终止,issues 里带真实原因。
            groups, theme_meta = _run_theme_classification_loop(
                topic, papers, progress=progress, check_stop=check_stop,
            )
            if theme_meta.get("status") != "passed":
                issues = theme_meta.get("issues") or []
                detail = "；".join(issues) if issues else "三级分类策略均未通过结构核查"
                raise ValueError(f"主题方案未通过结构检查：{detail}")
        else:
            groups = classify(papers, topic, classify_mode, progress=progress)
        # 主题聚类阶段必须保留全部“筛选后纳入”的文献：这一阶段的产出是
        # 面向全量证据的知识地图和可编辑主题归属，不能提前抽样成 70~90 篇
        # 写作池。提前抽样会造成主题覆盖、相关性报告和证据矩阵互相矛盾，
        # 也会让用户确认的主题不再代表实际纳入文献。引用配额只应在正文
        # 生成/引文选择阶段处理。
        # 主题模式且文献量足够时,嵌入 AI 质检 agent:LLM 自主查组感知 → 反思 → 提交终版
        agent_meta: dict | None = None
        # 大文献池的初版分类已经按 50 篇分块完成；再把数百篇全文送入质检
        # 会造成超长上下文和十几分钟阻塞。大池子改由代码完整性校验，避免主流程被二次 LLM 卡住。
        if classify_mode == "locale" and 15 <= len(papers) <= 120:
            agent_iterator = iter(classify_agent_stream(topic, papers, groups))
            while True:
                try:
                    agent_progress = next(agent_iterator)
                except StopIteration as finished:
                    groups, agent_meta = finished.value
                    break
                evt_name, evt_data = agent_progress
                yield _sse_event("classify_progress", {"kind": evt_name, **evt_data})
                check_stop()
        yield _sse_event("classify_done", {
            "groups": [{"name": g.name, "lit_ids": g.lit_ids} for g in groups],
            "agent": agent_meta,
            "harness": (
                {k: theme_meta[k] for k in ("status", "strategies", "issues")}
                if theme_meta else None
            ),
        })
        section_specs = build_review_sections(
            classify_mode, groups, topic,
            pool_language=_pool_dominant_language(papers),
        )
        yield _sse_event("plan_complete", {
            "groups": [{"name": g.name, "lit_ids": g.lit_ids} for g in groups],
            "section_titles": [spec.title for spec in section_specs],
            "papers": [_paper_payload(p) for p in papers],
            "screened_out_ids": screened_out,
            # LLM 分组失败时 classify 会降级单组兜底:告知前端醒目提示重新划分
            # ≥9 篇的池单组都不符合 _min_groups 下限,一律提示
            "classify_fallback": classify_mode == "theme" and len(papers) >= 9 and len(groups) <= 1,
            # v9.10: 分类策略透明化 —— 降级到本地聚类时前端显著提示
            # (LLM 主题轴方案未交付,本次分组与命名均为快速聚类质量)。
            "classification": {
                "strategies": (theme_meta or {}).get("strategies")
                or (["llm"] if agent_meta is None else ["llm", "llm_agent"]),
                "degraded": "local_fallback" in ((theme_meta or {}).get("strategies") or []),
            },
            "blueprint": build_review_blueprint(
                topic, papers, classify_mode, groups, screened_out,
                research_questions=research_questions,
                review_goal=review_goal,
            ),
        })
    except RuntimeError as e:
        if str(e) == "用户已停止写作":
            log.info("plan_review_stream 已按用户请求停止")
            yield _sse_event("stopped", {"message": "已停止写作任务"})
        else:
            raise
    except ValueError as e:
        log.warning("plan_review_stream 已终止: %s", e)
        yield _sse_event("error", {"message": str(e)})
    except Exception as e:
        log.exception("plan_review_stream 失败")
        yield _sse_event("error", {"message": str(e)})


def generate_review_stream(
    topic: str,
    papers: list[Paper],
    classify_mode: str,
    do_screening: bool = True,
    confirmed_groups: list[Group] | None = None,
    relevance_report: dict | None = None,
    stop_event=None,
    research_questions: list[str] | None = None,
    review_goal: str | None = None,
) -> Generator[str, None, None]:
    """流式生成综述,按事件 yield SSE 字符串。

    两阶段模式:confirmed_groups 非 None 时为阶段2(主题已由用户确认,直接写正文)。
    此时 papers 须传主题规划阶段(plan_review_stream)筛选后的文献,
    relevance_report 传规划阶段返回的分级清单(缺失时写作不带分级提示)。
    v9.9: research_questions / review_goal 注入写作与述评;主题模式在写作前
    先跑主题综合(证据抽取 + 共识/争议结构化),失败自动降级。
    """
    input_count = len(papers)
    try:
        def check_stop():
            if stop_event is not None and stop_event.is_set():
                raise RuntimeError("用户已停止写作")

        check_stop()
        yield _sse_event("start", {
            "topic": topic,
            "total_papers": len(papers),
            "classify_mode": classify_mode,
        })

        if confirmed_groups is not None:
            # 两阶段·阶段2:主题已确认,跳过筛选与分类(筛选不可重入,重复跑
            # 既浪费 LLM 调用,也可能二次剔文献打乱用户确认过的分组)。
            papers, _pool_dups = _dedupe_pool(papers)
            _backfill_user_raw_citation(papers)
            groups = _sanitize_confirmed_groups(confirmed_groups, papers)
            # 主题确认后的文献池是用户确认过的证据全集，不能再次静默抽样。
            # 候选配额已在阶段一完成；阶段二必须让每篇入选文献都可被章节引用。
            screened_out: list[str] = []
            if relevance_report is None:
                relevance_report = build_relevance_report([], {})
            yield _sse_event("classify_done", {
                "groups": [{"name": g.name, "lit_ids": g.lit_ids} for g in groups],
                "phase": "writing",
            })
            yield _sse_event("writing_started", {
                "message": "主题已确认，开始生成综述正文。",
                "total_sections": len(build_review_sections(
            classify_mode, groups, topic,
            pool_language=_pool_dominant_language(papers),
        )),
            })
        else:
            yield _sse_event("screening_started", {
                "total": len(papers),
                "message": "正在进行主题相关性与摘要质量筛选,每批 36 篇,最多 4 批并行...",
            })
            screening_iterator = iter(_screen_papers_stream(topic, papers, do_screening))
            while True:
                check_stop()
                try:
                    screening_progress = next(screening_iterator)
                except StopIteration as finished:
                    papers, screened_out, relevance_report = finished.value
                    # 源头治理:同题同年跨源重复与用户题录缺引文串,
                    # 不留给终局 QA 报 FAIL
                    papers, dedup_ids = _dedupe_pool(papers)
                    if dedup_ids:
                        log.info(
                            "池内同题同年重复条目剔除 %d 篇: %s",
                            len(dedup_ids), dedup_ids[:5],
                        )
                    backfilled = _backfill_user_raw_citation(papers)
                    if backfilled:
                        log.info("为 %d 篇用户导入文献合成缺失的原始引文", backfilled)
                    break
                yield _sse_event("screening_progress", screening_progress)
            yield _sse_event("screening_done", {
                "kept": len(papers),
                "screened_out": screened_out,
            })
            # 《文献相关性分级清单》：等级 + 四维得分 + 核心匹配点
            yield _sse_event("relevance_report", relevance_report)

            yield _sse_event("classify_started", {
                "classify_mode": classify_mode,
                "total": len(papers),
            })
            check_stop()
            groups = classify(papers, topic, classify_mode)
            yield _sse_event("classify_done", {
                "groups": [{"name": g.name, "lit_ids": g.lit_ids} for g in groups],
            })

        grade_map = _grade_map(relevance_report)

        # === 主题综合层(v9.9):证据抽取 + 共识/争议结构化,写作前完成 ===
        synthesis_map: dict[str, dict] = {}
        synthesis_meta: dict | None = None
        if _synthesis_active(classify_mode, groups):
            yield _sse_event("synthesis_started", {
                "themes": len(groups),
                "message": "正在进行主题级证据抽取与共识/争议综合推理...",
            })
            try:
                synthesis_map, synthesis_meta = _run_theme_synthesis(
                    topic, groups, papers, research_questions,
                )
                yield _sse_event("synthesis_done", {
                    **(synthesis_meta or {}),
                    "themes": sorted(synthesis_map.keys()),
                })
            except Exception as exc:  # noqa: BLE001
                synthesis_map = {}
                synthesis_meta = None
                yield _sse_event("synthesis_skipped", {
                    "reason": f"主题综合失败,已降级为直接写作: {exc}",
                })

        # v9.6:补传 topic——漏传时组名不合格的标题退化成通用名「主题研究进展」,
        # 与阶段 1 预览(L741)/同步路径(L980)不一致
        section_specs = build_review_sections(
            classify_mode, groups, topic,
            pool_language=_pool_dominant_language(papers),
        )
        sections: list[SectionResult] = []
        all_dropped: list[str] = []
        for idx, spec in enumerate(section_specs):
            check_stop()
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
            # v9.6:流式路径单章失败此前无占位兜底(非流式 L989 起有),LLM 全失败
            # 时第 N 章异常会把前 N-1 章整体报废——对齐同步路径的占位章节语义
            completed = False
            try:
                for piece, done, res in write_section_stream(
                    spec, topic, groups, section_papers, grades=grade_map,
                    synthesis_map=synthesis_map,
                    research_questions=research_questions,
                    review_goal=review_goal,
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
                        # Final deterministic post-condition shared with the
                        # synchronous path.  The stream may have already sent
                        # raw model tokens, but the delivered section result
                        # must still retain real anchors/evidence after repair.
                        ensure_section_evidence(res, section_papers)
                        res = _retry_section_quality(
                            spec=spec,
                            topic=topic,
                            groups=groups,
                            papers=section_papers,
                            grades=grade_map,
                            result=res,
                            synthesis_map=synthesis_map,
                            research_questions=research_questions,
                            review_goal=review_goal,
                        )
                        # 润色+核查闭环(用户策略:agent 自我纠错)
                        res = _polish_and_verify_section(res, section_papers)
                        completed = True
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
            except Exception as exc:
                if completed:
                    raise
                log.warning(
                    "章节 %s 流式写作失败,生成占位章节: %s",
                    getattr(spec, "key", "?"), exc,
                )
                res = _placeholder_section_result(spec, topic, section_papers)
                sections.append(res)
                yield _sse_event("section_done", {
                    "index": idx,
                    "total": len(section_specs),
                    "key": res.key,
                    "title": res.title,
                    "content": res.content,
                    "citations": res.citations,
                    "dropped_citations": res.dropped_citations,
                })

        # 交付门禁:若多数主题章节只是题录恢复稿(LLM 写作连续失败的产物),
        # 明确报错让用户重试,而不是把一堵模板墙静默交付(v9.7 软门禁只
        # 覆盖"局部兜底",覆盖不了"全线失败"的极端场景)。
        if _recovery_draft_ratio(sections) >= 0.5:
            import sys as _sys
            print("GATE-DEBUG ratio:", _recovery_draft_ratio(sections),
                  {s.key: s.content[:60] for s in sections}, file=_sys.stderr)
            raise ValueError(
                "写作引擎连续失败，各章节仅能生成题录清单而非正文。"
                "文献池与主题划分不受影响，请稍后重试写作。"
            )

        # 打通正文锚点与参考文献编号:分配全局编号、替换正文 [lit_xxx] -> [N]
        ref, _number_map = apply_citation_numbering(sections, papers)
        finalized_sections = [
            {
                "key": s.key,
                "title": s.title,
                "content": s.content,
                "citations": s.citations,
                "density_warning": s.density_warning,
            }
            for s in sections
        ]
        yield _sse_event("reference_started", {"count": len(collect_cited_ids(sections))})
        yield _sse_event("sections_finalized", {"sections": finalized_sections})
        yield _sse_event("reference_list", {"reference_list": ref})

        # === 全流程核查触发节点:apply_citation_numbering 之后 ===
        qa_payload: dict | None = None
        qa_failure: str | None = None
        if _qa_enabled():
            # v8.2 修复:此前本函数直接调用 get_fallback_order 等三个名字,
            # 但它们只在 _run_post_write_qa 内部 import 过,主流程作用域没有,
            # 导致 6 章写完后在 qa_started 处 NameError 全流程报废。
            from llm.client import (
                get_fallback_order,
                get_provider_health,
                select_providers,
            )
            yield _sse_event("qa_started", {
                "checks": ["accuracy", "binding", "quota", "citation_format"],
                "llm_fallback_order": list(get_fallback_order()),
                "llm_active_order": list(select_providers(None)),
                "llm_health": {
                    pid: (get_provider_health()["providers"].get(pid) or {})
                    for pid in (get_provider_health()["active_fallback_order"] or [])
                },
            })
            try:
                qa_payload = _run_post_write_qa(
                    papers=papers,
                    sections=sections,
                    reference_list=ref,
                    grade_map=grade_map,
                )
            except Exception as exc:  # noqa: BLE001
                # 核查子系统崩溃也不销毁已成文的综述:正文/参考文献均已交付
                qa_failure = f"核查流程异常(不影响已生成内容): {exc}"

            if qa_payload is not None:
                overall = qa_payload.get("summary", {}).get("overall", "pass")
                auto_repair_count = len(
                    qa_payload.get("summary", {}).get("auto_repairs", []) or []
                )
                yield _sse_event("qa_done", {
                    "overall": overall,
                    "pass_rate": qa_payload["summary"]["pass_rate"],
                    "auto_repair_count": auto_repair_count,
                    "checks": [
                        {
                            "check_id": r["check_id"],
                            "name": r["name"],
                            "status": r["status"],
                            "fail_count": r["metrics"].get("fail_count", 0),
                            "warn_count": r["metrics"].get("warn_count", 0),
                        }
                        for r in qa_payload["summary"]["results"]
                    ],
                    "issues": qa_payload["summary"]["issues"],
                    "llm_fallback_order": list(get_fallback_order()),
                    "llm_active_order": list(select_providers(None)),
                    "llm_health": get_provider_health(),
                })
                if overall == "fail":
                    # v9.7 软门禁 + 修复闭环:FAIL 已先经过确定性修复与 LLM
                    # 定向重写(auto_repair_count 为实际修复量),修不掉的
                    # 剩余问题如实上报,不伪造通过、不静默交付。
                    yield _sse_event("qa_failed", {
                        "message": (
                            f"全流程质量核查未通过:已自动修复 {auto_repair_count} 项问题,"
                            "剩余问题超出自动修复能力,请结合报告人工复核。"
                        ),
                    })

        yield _sse_event("complete", {
            "screened_out_ids": screened_out,
            "dropped_citations": all_dropped,
            "qa_report": qa_payload,
            "qa_error": qa_failure,
            # v9.9: PRISMA 式流程统计(识别→筛选→纳入→主题→章节→综合)
            "flow_stats": {
                "input_papers": input_count,
                "screened_out": len(screened_out),
                "included": len(papers),
                "themes": len(groups),
                "sections": len(sections),
                "synthesis_themes": len(synthesis_map),
            },
        })

    except RuntimeError as e:
        if str(e) == "用户已停止写作":
            log.info("generate_review_stream 已按用户请求停止")
            yield _sse_event("stopped", {"message": "已停止写作任务"})
        else:
            raise
    except ValueError as e:
        log.warning("generate_review_stream 已终止: %s", e)
        yield _sse_event("error", {"message": str(e)})
    except Exception as e:
        log.exception("generate_review_stream 失败")
        yield _sse_event("error", {"message": str(e)})


def generate_review(
    topic: str,
    papers: list[Paper],
    classify_mode: str,
    do_screening: bool = True,
    confirmed_groups: list[Group] | None = None,
    relevance_report: dict | None = None,
    research_questions: list[str] | None = None,
    review_goal: str | None = None,
) -> ReviewResult:
    """一次性生成完整综述(测试/向后兼容用)。

    confirmed_groups 非 None 时为两阶段模式阶段2:跳过筛选与分类直接写作。
    """
    if classify_mode not in ("locale", "theme"):
        raise ValueError(f"unknown classify_mode: {classify_mode}")

    if confirmed_groups is not None:
        papers, _pool_dups = _dedupe_pool(papers)
        _backfill_user_raw_citation(papers)
        groups = _sanitize_confirmed_groups(confirmed_groups, papers)
        # 与流式阶段二保持一致：用户确认后的分组不再被隐式缩减。
        screened_out: list[str] = []
        relevance_report = relevance_report or build_relevance_report([], {})
    else:
        papers, screened_out, relevance_report = _screen_papers(topic, papers, do_screening)
        papers, _pool_dups = _dedupe_pool(papers)
        _backfill_user_raw_citation(papers)
        groups = classify(papers, topic, classify_mode)

    # 主题综合层(与流式路径一致:失败降级,不阻塞写作)
    synthesis_map: dict[str, dict] = {}
    if _synthesis_active(classify_mode, groups):
        try:
            synthesis_map, _ = _run_theme_synthesis(
                topic, groups, papers, research_questions,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("主题综合失败,已降级为直接写作: %s", exc)
            synthesis_map = {}

    section_specs = build_review_sections(
            classify_mode, groups, topic,
            pool_language=_pool_dominant_language(papers),
        )
    sections: list[SectionResult] = []
    all_dropped: list[str] = []
    grade_map = _grade_map(relevance_report)
    for spec in section_specs:
        section_papers = _papers_for_section(spec, groups, papers)
        # 单章节写入失败不应阻塞整篇综述:e2e/批量场景下,如果 LLM 全部
        # provider 都不可用,允许用一份基于真实文献题录的占位章节继续。
        # 质量会通过 qa.json 暴露给用户。
        try:
            res = write_section(
                spec,
                topic,
                groups,
                section_papers,
                grades=grade_map,
                synthesis_map=synthesis_map,
                research_questions=research_questions,
                review_goal=review_goal,
            )
            ensure_section_evidence(res, section_papers)
            # 章节完成后先做一次本地质量门禁；题录兜底稿、内部推理泄漏稿
            # 或引用密度不足稿必须重新请求，不能等最终文件生成后才暴露。
            res = _retry_section_quality(
                spec=spec,
                topic=topic,
                groups=groups,
                papers=section_papers,
                grades=grade_map,
                result=res,
                synthesis_map=synthesis_map,
                research_questions=research_questions,
                review_goal=review_goal,
            )
            res = _polish_and_verify_section(res, section_papers)
        except Exception as exc:
            log.warning(
                "章节 %s 写作失败,生成占位章节: %s",
                getattr(spec, "key", "?"), exc,
            )
            res = _placeholder_section_result(spec, topic, section_papers)
        sections.append(res)
        all_dropped.extend(res.dropped_citations)

    # 交付门禁(与流式路径一致):全模板墙不静默交付
    if _recovery_draft_ratio(sections) >= 0.5:
        raise ValueError(
            "写作引擎连续失败，各章节仅能生成题录清单而非正文。"
            "文献池与主题划分不受影响，请稍后重试写作。"
        )

    # 统一编号:正文 [lit_xxx] -> [N],与参考文献列表顺序一致(调用方渲染列表)
    reference_list, _ = apply_citation_numbering(sections, papers)
    qa_report: dict | None = None
    if _qa_enabled():
        qa_report = _run_post_write_qa(
            papers=papers,
            sections=sections,
            reference_list=reference_list,
            grade_map=grade_map,
        )

    return ReviewResult(
        topic=topic,
        classify_mode=classify_mode,
        groups=groups,
        sections=sections,
        reference_list=reference_list,
        screened_out_ids=screened_out,
        dropped_citations=all_dropped,
        relevance_report=relevance_report,
        qa_report=qa_report,
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
_DOI_FIELD_RE = _re.compile(
    r"(?:,?\s*(?:doi\s*:\s*)?https?://(?:dx\.)?doi\.org/10\.\d{4,9}/[^\s\]\[;,。；]+)"
    r"|(?:,?\s*doi\s*:\s*10\.\d{4,9}/[^\s\]\[;,。；]+)"
    r"|(?:,?\s*10\.\d{4,9}/[^\s\]\[;,。；]+)",
    _re.IGNORECASE,
)


def _strip_doi(text: str) -> str:
    """Remove DOI labels, DOI URLs, and bare DOI identifiers from a citation."""
    value = _DOI_FIELD_RE.sub("", text or "")
    value = _re.sub(r"\s{2,}", " ", value)
    value = _re.sub(r"\s+([,.;。；])", r"\1", value)
    value = _re.sub(r"([,;，；])\s*([.。])", r"\2", value)
    return value.strip()






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
      - OPENALEX / PUBMED / CROSSREF: citeproc-py 规则渲染

    兼顾中文手入库与英文 API 两种来源。中文条目没有 raw_citation 时，
    使用已存在的结构化元数据生成最小可追溯条目，避免正文生成后因元数据缺口返回 500。
    """
    if p.source in (Source.CNKI, Source.USER_IMPORTED):
        raw = _clean_dirty(getattr(p, "raw_citation", None))
        if raw:
            return _strip_doi(raw).rstrip(".") + "."
        authors = ", ".join(a.strip() for a in (p.authors or []) if a and a.strip()) or "佚名"
        title = _clean_dirty(p.title) or "未命名"
        journal = _clean_dirty(p.journal) or "来源不详"
        year = str(p.year) if p.year else "n.d."
        return _strip_doi(f"{authors}. {title}[J]. {journal}, {year}.")

    if p.source in (Source.OPENALEX, Source.PUBMED, Source.CROSSREF):
        rendered = _citeproc_render(p)
        if rendered:
            rendered = _strip_doi(rendered).rstrip(".")
            # 英文文献的真实性核查依赖 DOI:条目末尾统一带 DOI,用户可以
            # 直接粘贴到 doi.org / Google Scholar 验证,而不是逐词手敲标题。
            doi = (getattr(p, "doi", "") or "").strip()
            bare = _re.sub(r"^(?:https?://(?:dx\.)?doi\.org/)", "", doi, flags=_re.IGNORECASE)
            if bare:
                return f"{rendered}. DOI:{bare}."
            return f"{rendered}."
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


_NUMBERED_CITE_RE = _re.compile(r"\[(\d{1,3})\]")


def restore_internal_citation_anchors(sections: list[SectionResult]) -> None:
    """把已编号正文中的 [N] 无损还原成内部 [lit_x] 锚点。

    QA 修复链(article_lint/binding/density)只认识内部锚点;若在编号之后
    直接修复,[N] 会被当成模型残留的伪锚点剥掉,造成引用丢失。编号流程
    写完 citations 后,各章节 citations 里的 lit_id 恰好按全局编号升序排列
    (由 apply_citation_numbering 的同步逻辑保证),据此可以完整重建
    编号 → lit_id 映射,再逐个把 [N] 换回 [lit_x]。已含内部锚点的章节跳过。
    """
    queue: list[str] = []
    seen: set[str] = set()
    for s in sections:
        for lid in getattr(s, "citations", None) or []:
            lid = str(lid)
            if lid.startswith(("lit_", "hash:")) and lid not in seen:
                seen.add(lid)
                queue.append(lid)
    if not queue:
        return
    number_to_lid = {index + 1: lid for index, lid in enumerate(queue)}

    def _restore(match: _re.Match) -> str:
        lid = number_to_lid.get(int(match.group(1)))
        return f"[{lid}]" if lid else match.group(0)

    for s in sections:
        content = s.content or ""
        if _CITE_ANCHOR_RE.search(content):
            continue
        s.content = _NUMBERED_CITE_RE.sub(_restore, content)


def apply_citation_numbering(
    sections: list[SectionResult],
    papers: list[Paper],
) -> tuple[str, dict[str, int]]:
    """打通正文锚点与参考文献编号两套体系。

    - 以正文实际锚点为单一事实来源,按出现顺序分配全局编号 N(1, 2, 3, ...);
    - 把各章节正文中的 [lit_xxx] 原地替换为 [N];
    - 按同一顺序渲染参考文献列表(编号 [N] 连续);
    - 同步回写每个章节的 citations,使之和正文真实锚点一致;
      避免 s.citations 与正文实际 [N] 不一致触发 BINDING_LISTED_NOT_CITED。

    返回 (reference_list, lit_id -> N 映射)。
    """
    # 幂等保护:上一轮编号已把锚点替换成 [N],直接重跑会把 [N] 当伪锚点
    # 剥掉导致引用丢失。先用 citations 还原内部锚点,再走统一编号流程。
    restore_internal_citation_anchors(sections)
    by_id = {p.lit_id: p for p in papers}
    # 单一事实来源:正文里真实出现的 [lit_xxx] 锚点(按章节、按出现顺序)
    cited_ids: list[str] = []
    seen: set[str] = set()
    # 保留每章节原始 lit_id 集合,替换 [lit_xxx] -> [N] 后回填 s.citations 用
    _SECTION_LIT_IDS: dict[int, list[object]] = {}
    for s in sections:
        ids: list[object] = []
        ids_seen: set[str] = set()
        for m in _CITE_ANCHOR_RE.finditer(s.content or ""):
            lid = m.group(1)
            if lid in ids_seen:
                continue
            ids_seen.add(lid)
            ids.append(m)
            if lid in seen:
                continue
            if lid not in by_id:
                continue
            cited_ids.append(lid)
            seen.add(lid)
        _SECTION_LIT_IDS[id(s)] = ids
    number_map = {cid: i + 1 for i, cid in enumerate(cited_ids)}
    by_id = {p.lit_id: p for p in papers}
    def label(p: Paper) -> str:
        authors = [a.strip() for a in (p.authors or []) if a and a.strip()]
        if not authors:
            name = "相关研究"
        elif p.source in (Source.CNKI, Source.USER_IMPORTED):
            name = authors[0] + ("等" if len(authors) > 1 else "")
        else:
            name = authors[0].split()[-1].rstrip(".") + (" et al." if len(authors) > 1 else "")
        return f"{name}（{p.year}）" if p.year else name
    for s in sections:
        def repl(match: _re.Match) -> str:
            lid = match.group(1)
            n = number_map.get(lid, lid)
            p = by_id.get(lid)
            if p is None:
                return f"[{n}]"

            # 锚点通常由“作者（年份）”夹注自动注入，正文已经包含作者和年份；
            # 只有占位章节/旧数据没有可见夹注时，才补一个元数据标签。
            # 旧逻辑只看锚点前 40 个字符，遇到“参见：...；作者（年份）”
            # 或较长句子就会误判，产生“作者（年份）...作者（年份）[N]”。
            sentence_start = 0
            for boundary in _re.finditer(r"[。！？!?；;\n]", s.content[:match.start()]):
                sentence_start = boundary.end()
            local = s.content[sentence_start:match.start()]
            # 上一轮编号写入的标签就是 label(p) 原文,先直接查它,避免
            # "作者1"里的数字被作者名清洗剥掉后误判不可见而重复插标签。
            display_label = label(p)
            visible = bool(display_label) and display_label in local
            year = str(p.year) if p.year else ""
            if not visible and year:
                if p.source in (Source.CNKI, Source.USER_IMPORTED):
                    author_tokens = []
                    for author in p.authors or []:
                        raw_author = str(author or "").strip().rstrip(".")
                        compact = _re.sub(r"[^\u4e00-\u9fff]", "", str(author or ""))
                        # 紧凑形与原始形都参与识别:只查紧凑形会把含数字/
                        # 外文的作者名(作者1、Jean-标注)误判为不可见,
                        # 重复注入「作者1(2023)作者1（2023）[N]」式粘连
                        if compact:
                            author_tokens.append(compact)
                        if raw_author and raw_author != compact:
                            author_tokens.append(raw_author)
                else:
                    author_tokens = []
                    for author in p.authors or []:
                        raw_author = str(author or "").strip().rstrip(".")
                        if not raw_author:
                            continue
                        if "," in raw_author:
                            raw_author = raw_author.split(",", 1)[0].strip()
                        parts = raw_author.split()
                        author_tokens.append((parts[-1] if parts else raw_author).rstrip("."))
                visible = any(
                    token and _re.search(
                        _re.escape(token) + r"(?:等|\s+et\s+al\.)?\s*[（(]\s*"
                        + _re.escape(year) + r"\s*[）)]",
                        local,
                        flags=_re.IGNORECASE,
                    )
                    for token in author_tokens
                )
            return f"[{n}]" if visible else f"{display_label}[{n}]"
        # 模型可能残留 [54] 之类未经映射的数字伪锚点，统一剥离后再替换
        # 真正的 lit_id 锚点，避免 QA 报告悬空编号。
        cleaned = _re.sub(r"\[\d{1,3}\]", "", s.content)
        s.content = _CITE_ANCHOR_RE.sub(repl, cleaned)
    # 同步每个章节的 citations:必须按本章节锚点的**正文出现顺序**回填。
    # 此前沿用旧 citations 的顺序过滤 —— 锚点集合发生变化(修复重写后
    # 重新编号)时,顺序与 number_map 的编号顺序脱节,下一轮 restore 按
    # citations 顺序反解 [N] 会把 lit_id 张冠李戴,产生跨文献的引用错配。
    section_cited_ids = collect_cited_ids(sections)
    for s in sections:
        present: list[str] = []
        seen_in_s: set[str] = set()
        for m in _SECTION_LIT_IDS.get(id(s), []):
            lid = m.group(1)
            if lid in seen_in_s or lid not in by_id:
                continue
            present.append(lid)
            seen_in_s.add(lid)
        s.citations = present

    # 参考文献必须与引用顺序一致,不能沿用 papers 原始顺序,否则编号错位
    cited_papers = [by_id[cid] for cid in cited_ids if cid in by_id]
    return render_reference_list(cited_papers), number_map


def render_reference_list(papers: list[Paper]) -> str:
    """生成 GB/T 7714-2025 参考文献列表,按传入顺序统一编号 [N]。

    - papers 顺序即引用顺序(由 collect_cited_ids 保证去重与保序);
    - CNKI 使用 raw_citation,OpenAlex/PubMed/Crossref 使用 citeproc 渲染条目;
    - 编号由本函数统一分配,不再依赖 citeproc 单条渲染(否则每条都是 [1])。
    """
    lines: list[str] = []
    for idx, p in enumerate(papers, start=1):
        lines.append(f"[{idx}] {_format_one_gbt(p)}")
    return "\n".join(lines)





