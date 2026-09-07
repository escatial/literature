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
from writing.classifier import Group, classify
from writing.relevance import (
    RelevanceScore,
    build_concept_groups,
    build_relevance_report,
    grade_papers,
    sort_by_relevance,
)
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
_REFERENCE_LIMIT_MAX = _int_env("WRITING_REF_MAX", 100)       # 综述最高引用数
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
    不合格时回退到主题派生名(主题前 4 字 +「研究进展」),多个组同时回退时
    追加中文序号(二)(三) 去重,避免出现重复的「一、主题研究」「二、主题研究」。
    """
    instruction_template = (
        SECTION_THEME_INSTRUCTION_TEMPLATE
        if classify_mode == "theme"
        else SECTION_LOCALE_INSTRUCTION_TEMPLATE
    )
    sections: list[SectionSpec] = []
    # 内部导入避免循环依赖
    from writing.classifier import _clean_group_name, _group_name_acceptable

    # 回退组名:从研究主题派生,保证非空、通过组名校验。
    # 主题只取前 4 字:给重复时的序号后缀「(二)」预留长度
    # (上限 12 字,topic[:6] 拼序号会超长导致去重循环找不到合格名)。
    topic_stripped = (topic or "").strip()
    fallback_base = f"{topic_stripped[:4]}研究进展" if topic_stripped else "主题研究进展"
    if not _group_name_acceptable(fallback_base):
        fallback_base = "主题研究进展"
    used_names: set[str] = set()

    def _unique_fallback() -> str:
        """回退名去重:重复时追加中文序号(二)(三)...。"""
        if fallback_base not in used_names:
            return fallback_base
        num = 2
        while num <= 99:
            candidate = f"{fallback_base}({_format_chinese_index(num)})"
            if candidate not in used_names and _group_name_acceptable(candidate):
                return candidate
            num += 1
        # 极端兜底:全部序号都超长/重复时允许重名,绝不死循环
        return fallback_base

    for idx, group in enumerate(groups, start=1):
        clean_name = _clean_group_name(group.name)
        if not _group_name_acceptable(clean_name):
            clean_name = _unique_fallback()
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
    missing_chinese = _required_chinese_count(kept_papers)
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
    missing_chinese = _required_chinese_count(kept_papers)
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
    for grp in confirmed_groups:
        ids = [i for i in grp.lit_ids if i in known]
        if grp.name and ids:
            groups.append(Group(name=grp.name, lit_ids=ids))
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
        yield _sse_event("start", {
            "topic": topic,
            "total_papers": len(papers),
            "classify_mode": classify_mode,
        })
        yield _sse_event("screening_started", {
            "total": len(papers),
            "message": "正在进行主题相关性与摘要质量筛选,每批 24 篇,最多并行处理 4 批...",
        })
        screening_iterator = iter(_screen_papers_stream(topic, papers, True))
        while True:
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
        groups = classify(papers, topic, classify_mode)
        # 主题模式且文献量足够时,嵌入 AI 质检 agent:LLM 自主查组感知 → 反思 → 提交终版
        agent_meta: dict | None = None
        if classify_mode == "theme" and len(papers) >= 15:
            agent_iterator = iter(classify_agent_stream(topic, papers, groups))
            while True:
                try:
                    agent_progress = next(agent_iterator)
                except StopIteration as finished:
                    groups, agent_meta = finished.value
                    break
                evt_name, evt_data = agent_progress
                yield _sse_event("classify_progress", {"kind": evt_name, **evt_data})
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
        })
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
) -> Generator[str, None, None]:
    """流式生成综述,按事件 yield SSE 字符串。

    两阶段模式:confirmed_groups 非 None 时为阶段2(主题已由用户确认,直接写正文)。
    此时 papers 须传主题规划阶段(plan_review_stream)筛选后的文献,
    relevance_report 传规划阶段返回的分级清单(缺失时写作不带分级提示)。
    """
    try:
        yield _sse_event("start", {
            "topic": topic,
            "total_papers": len(papers),
            "classify_mode": classify_mode,
        })

        if confirmed_groups is not None:
            # 两阶段·阶段2:主题已确认,跳过筛选与分类(筛选不可重入,重复跑
            # 既浪费 LLM 调用,也可能二次剔文献打乱用户确认过的分组)。
            groups = _sanitize_confirmed_groups(confirmed_groups, papers)
            screened_out: list[str] = []
            if relevance_report is None:
                relevance_report = build_relevance_report([], {})
            yield _sse_event("classify_done", {
                "groups": [{"name": g.name, "lit_ids": g.lit_ids} for g in groups],
            })
        else:
            yield _sse_event("screening_started", {
                "total": len(papers),
                "message": "正在进行主题相关性与摘要质量筛选,每批 24 篇,最多并行处理 4 批...",
            })
            screening_iterator = iter(_screen_papers_stream(topic, papers, do_screening))
            while True:
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





