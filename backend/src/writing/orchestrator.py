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
from retrieval.types import Paper, Source
from screening.llm_filter import screen_batch, screen_batch_stream
from writing.classifier import Group, classify, _title_similarity
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
        if not _group_name_acceptable(clean_name) or clean_name in used_names:
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
                "study", "studies", "research", "analysis", "method", "methods", "approach",
                "this", "that", "these", "those", "with", "without", "based", "and", "the",
                "are", "for", "from", "into", "is", "it", "its", "of", "on", "or", "to", "using",
                "used", "use", "can", "could", "may", "might", "will", "would", "also", "than",
                "results", "result", "paper", "papers", "work", "works", "shown", "show", "find", "findings",
                "such", "data", "future", "challenge", "challenges", "learning", "new", "two", "one",
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
    return {
        "research_question": topic.strip(),
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
    from writing.section_writer import SectionResult, _paper_display_author_year

    if getattr(spec, "key", "") == "comment":
        return _placeholder_section_result(spec, topic, papers)
    evidence = []
    citations: list[str] = []
    for paper in papers:
        if not paper.lit_id:
            continue
        author_year = _paper_display_author_year(paper)
        title = (paper.title or "未命名文献").strip().rstrip("。！？!?")
        evidence.append(
            f"{author_year}围绕《{title}》讨论了与本主题相关的研究对象、决策变量或分析方法，"
            f"其题名与摘要证据可用于界定该研究路径的适用边界[{paper.lit_id}]。"
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
            retry = write_section(spec, topic, groups, papers, grades=grades)
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
        # v9.7:QA FAIL 不再报废整篇综述。此时正文/参考文献已全部生成,
        # 抛错只会丢掉全部 LLM 成本,且 raise 路径连核查报告都不返回,
        # 用户只能看到一句笼统的"未通过"。改为:照常返回 payload,
        # overall/issues 经 qa_done 事件与 complete.qa_report 交付前端,
        # 由用户决定是否采纳;真正的失败信息一处不丢。
        log.warning("QA 核查未通过(软门禁,不阻断输出): %s",
                    ", ".join(r.name for r in summary.results
                              if r.status == "fail"))

    return run_post_write_qa(
        runner=QARunner(ruleset),
        papers=papers,
        sections=sections,
        reference_list=reference_list,
        grades=grade_map,
        on_fail=_soft_on_fail,
    )


def plan_review_stream(
    topic: str,
    papers: list[Paper],
    classify_mode: str,
    stop_event=None,
    progress=None,
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
            "message": "正在进行主题相关性与摘要质量筛选,每批 24 篇,最多并行处理 2 批...",
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
        })
        section_specs = build_review_sections(classify_mode, groups, topic)
        yield _sse_event("plan_complete", {
            "groups": [{"name": g.name, "lit_ids": g.lit_ids} for g in groups],
            "section_titles": [spec.title for spec in section_specs],
            "papers": [_paper_payload(p) for p in papers],
            "screened_out_ids": screened_out,
            # LLM 分组失败时 classify 会降级单组兜底:告知前端醒目提示重新划分
            # ≥9 篇的池单组都不符合 _min_groups 下限,一律提示
            "classify_fallback": classify_mode == "theme" and len(papers) >= 9 and len(groups) <= 1,
            "blueprint": build_review_blueprint(topic, papers, classify_mode, groups, screened_out),
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
) -> Generator[str, None, None]:
    """流式生成综述,按事件 yield SSE 字符串。

    两阶段模式:confirmed_groups 非 None 时为阶段2(主题已由用户确认,直接写正文)。
    此时 papers 须传主题规划阶段(plan_review_stream)筛选后的文献,
    relevance_report 传规划阶段返回的分级清单(缺失时写作不带分级提示)。
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

        if confirmed_groups is not None:
            # 两阶段·阶段2:主题已确认,跳过筛选与分类(筛选不可重入,重复跑
            # 既浪费 LLM 调用,也可能二次剔文献打乱用户确认过的分组)。
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
                "total_sections": len(build_review_sections(classify_mode, groups, topic)),
            })
        else:
            yield _sse_event("screening_started", {
                "total": len(papers),
                "message": "正在进行主题相关性与摘要质量筛选,每批 24 篇,最多并行处理 2 批...",
            })
            screening_iterator = iter(_screen_papers_stream(topic, papers, do_screening))
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

        # v9.6:补传 topic——漏传时组名不合格的标题退化成通用名「主题研究进展」,
        # 与阶段 1 预览(L741)/同步路径(L980)不一致
        section_specs = build_review_sections(classify_mode, groups, topic)
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
                        )
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
                yield _sse_event("qa_done", {
                    "overall": overall,
                    "pass_rate": qa_payload["summary"]["pass_rate"],
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
                    # v9.7 软门禁:FAIL 交付报告与正文,不再以 error 终止流。
                    # 详情见 qa_done.issues 与 complete.qa_report,前端汇总展示。
                    yield _sse_event("qa_failed", {
                        "message": "全流程质量核查未通过,报告如下;综述仍已生成,请结合报告人工复核。",
                    })

        yield _sse_event("complete", {
            "screened_out_ids": screened_out,
            "dropped_citations": all_dropped,
            "qa_report": qa_payload,
            "qa_error": qa_failure,
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
) -> ReviewResult:
    """一次性生成完整综述(测试/向后兼容用)。

    confirmed_groups 非 None 时为两阶段模式阶段2:跳过筛选与分类直接写作。
    """
    if classify_mode not in ("locale", "theme"):
        raise ValueError(f"unknown classify_mode: {classify_mode}")

    if confirmed_groups is not None:
        groups = _sanitize_confirmed_groups(confirmed_groups, papers)
        # 与流式阶段二保持一致：用户确认后的分组不再被隐式缩减。
        screened_out: list[str] = []
        relevance_report = relevance_report or build_relevance_report([], {})
    else:
        papers, screened_out, relevance_report = _screen_papers(topic, papers, do_screening)
        groups = classify(papers, topic, classify_mode)

    section_specs = build_review_sections(classify_mode, groups, topic)
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
            )
        except Exception as exc:
            log.warning(
                "章节 %s 写作失败,生成占位章节: %s",
                getattr(spec, "key", "?"), exc,
            )
            res = _placeholder_section_result(spec, topic, section_papers)
        sections.append(res)
        all_dropped.extend(res.dropped_citations)

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
            return _strip_doi(rendered)
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

    - 以正文实际锚点为单一事实来源,按出现顺序分配全局编号 N(1, 2, 3, ...);
    - 把各章节正文中的 [lit_xxx] 原地替换为 [N];
    - 按同一顺序渲染参考文献列表(编号 [N] 连续);
    - 同步回写每个章节的 citations,使之和正文真实锚点一致;
      避免 s.citations 与正文实际 [N] 不一致触发 BINDING_LISTED_NOT_CITED。

    返回 (reference_list, lit_id -> N 映射)。
    """
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
            visible = False
            year = str(p.year) if p.year else ""
            if year:
                if p.source in (Source.CNKI, Source.USER_IMPORTED):
                    author_tokens = []
                    for author in p.authors or []:
                        compact = _re.sub(r"[^\u4e00-\u9fff]", "", str(author or ""))
                        if compact:
                            author_tokens.append(compact)
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
            return f"[{n}]" if visible else f"{label(p)}[{n}]"
        # 模型可能残留 [54] 之类未经映射的数字伪锚点，统一剥离后再替换
        # 真正的 lit_id 锚点，避免 QA 报告悬空编号。
        cleaned = _re.sub(r"\[\d{1,3}\]", "", s.content)
        s.content = _CITE_ANCHOR_RE.sub(repl, cleaned)
    # 同步每个章节的 citations:必须从替换前的 lit_id 集合回填,
    # 因为 s.content 此时已是 [N] 形式,_CITE_ANCHOR_RE 不再能匹配。
    section_cited_ids = collect_cited_ids(sections)
    for s in sections:
        present: list[str] = []
        seen_in_s: set[str] = set()
        for lid in section_cited_ids:
            if lid in seen_in_s:
                continue
            if lid not in by_id:
                continue
            # 只保留本章节实际写入了 [lit_xxx] 的部分(从替换前的快照推导)
            if any(m.group(1) == lid for m in _SECTION_LIT_IDS.get(id(s), [])):
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





