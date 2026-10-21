"""写作池配额精选:核心期刊分层 × 相关性分级 × 语言引导线。

筛选层(relevant + abstract_ok)回答"能不能进池";本层回答"池子里选谁"。
设计立场(2026-10-21 用户明确:不要写得太死,配额是引导线不是卡尺):
- 总量目标 90 篇(WRITING_REF_TARGET);软上限 = 目标 +10%(90 → 99),
  池子不超过软上限就全量保留,91 篇不值得裁;
- 明显超配额才裁。英文进池线 = 目标 × 40%(即 36 篇):35 篇高质量英文
  不会被"中文之外的剩余名额"卡掉;中文按核心期刊优先(北大核心/CSCD/CSSCI,
  classify_journal 判定)、同层按相关性等级+总分降序补齐到目标总量;
- 中文占比 60% 是英文 30%-40% 引导带的补集,不再强制 2/3;
- 总量跌破下限(WRITING_REF_MIN=70)时才允许英文越线回填到下限,
  保证 QA 引用区间有足够候选;
- method_rigor < 40 的文献仅在同等级内垫底,不跨级淘汰(与 v9.9 立场一致:
  严谨性是审计维度,不参与相关性分级);
- 被裁文献不丢:返回 PoolCut 明细(标题/期刊/层级/原因),随 relevance_report
  的 quota_cut 键透出给前端确认面板展示与追溯(与 excluded_low_relevance 同构)。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from retrieval.types import Paper, Source
from writing.relevance import GRADE_ORDER, RelevanceScore

_CHINESE_SOURCES = frozenset({Source.CNKI, Source.USER_IMPORTED})

# method_rigor 垫底线:低于该值的文献在同等级内排到最后,不单独淘汰。
_RIGOR_FLOOR = 40.0

# 英文进池线:目标总量 × 40%(英文 30%-40% 引导带的上沿)。
# 是引导线不是硬帽——只有总量跌破 WRITING_REF_MIN 下限时英文才越线回填。
_EN_SHARE_MAX = 0.40


@dataclass(frozen=True)
class PoolCut:
    """一篇被配额裁剪的文献及其裁剪原因。"""

    lit_id: str
    title: str
    journal: str
    year: int
    language: str   # "zh" | "en"
    core_tier: str  # "core" | "non_core" | "foreign"
    grade: str      # high | medium | low | ""(无评分数据时)
    total: float
    reason: str

    def to_row(self) -> dict:
        return {
            "lit_id": self.lit_id,
            "title": self.title,
            "journal": self.journal,
            "year": self.year,
            "language": self.language,
            "core_tier": self.core_tier,
            "grade": self.grade,
            "total": self.total,
            "reason": self.reason,
        }


def _is_chinese(paper: Paper) -> bool:
    return paper.source in _CHINESE_SOURCES


def _journal_in_core(journal: str) -> bool:
    """核心期刊判定。classify_journal 自带内存缓存;清单不可用时按非核心处理,
    只影响分层顺序,不影响入选总量。"""
    if not (journal or "").strip():
        return False
    try:
        from core_journals import classify_journal
        return bool(classify_journal(journal)["in_core"])
    except Exception:
        return False


def _score_of(
    scores: dict[str, RelevanceScore] | None, lit_id: str,
) -> RelevanceScore | None:
    return scores.get(lit_id) if scores else None


def _chinese_sort_key(
    paper: Paper, scores: dict[str, RelevanceScore] | None,
) -> tuple:
    """中文排序:核心层优先 → 相关性等级 → 严谨性垫底 → 总分 → 标题(稳定)。"""
    score = _score_of(scores, paper.lit_id)
    grade = GRADE_ORDER[score.grade] if score else GRADE_ORDER["medium"]
    rigor_penalty = (
        1 if score is not None and score.method_rigor is not None
        and score.method_rigor < _RIGOR_FLOOR else 0
    )
    return (
        0 if _journal_in_core(paper.journal) else 1,
        grade,
        rigor_penalty,
        -score.total if score else 0.0,
        paper.title,
    )


def _english_sort_key(
    paper: Paper, scores: dict[str, RelevanceScore] | None,
) -> tuple:
    """英文排序:相关性等级 → 严谨性垫底 → 总分 → 被引数 → 标题(稳定)。"""
    score = _score_of(scores, paper.lit_id)
    grade = GRADE_ORDER[score.grade] if score else GRADE_ORDER["medium"]
    rigor_penalty = (
        1 if score is not None and score.method_rigor is not None
        and score.method_rigor < _RIGOR_FLOOR else 0
    )
    return (
        grade,
        rigor_penalty,
        -score.total if score else 0.0,
        -paper.cited_by_count,
        paper.title,
    )


def _cut_reason(core_tier: str) -> str:
    if core_tier == "core":
        return "核心期刊文献超出写作池配额(同层按相关性排序取优)"
    if core_tier == "non_core":
        return "非核心期刊文献,配额优先核心期刊与更高相关文献"
    return "英文文献超出写作池配额(按相关性与被引排序)"


def _make_cut(
    paper: Paper, scores: dict[str, RelevanceScore] | None,
) -> PoolCut:
    score = _score_of(scores, paper.lit_id)
    chinese = _is_chinese(paper)
    core_tier = (
        ("core" if _journal_in_core(paper.journal) else "non_core")
        if chinese else "foreign"
    )
    return PoolCut(
        lit_id=paper.lit_id,
        title=paper.title,
        journal=paper.journal,
        year=paper.year or 0,
        language="zh" if chinese else "en",
        core_tier=core_tier,
        grade=score.grade if score else "",
        total=score.total if score else 0.0,
        reason=_cut_reason(core_tier),
    )


def select_writing_pool(
    papers: list[Paper],
    scores: dict[str, RelevanceScore] | None = None,
    *,
    target_total: int | None = None,
    min_total: int | None = None,
    max_total: int | None = None,
) -> tuple[list[Paper], list[PoolCut]]:
    """从筛选后全量文献中精选写作池,返回 (入选文献, 被裁明细)。

    软引导而非硬配额:
    - 池子 ≤ 软上限(目标 +10%,90 → 99)时全量保留,一篇不裁;
    - 明显超配额才裁到目标总量:英文先在 40% 进池线内取满,中文(核心
      期刊优先)补齐;中文池不足导致总量短于目标是正常形态;
    - 只有总量跌破下限(70)时,英文才越线回填到下限(纯英文池可到 90)。
    """
    from writing.orchestrator import (
        _REFERENCE_LIMIT_DEFAULT,
        _REFERENCE_LIMIT_MAX,
        _REFERENCE_LIMIT_MIN,
    )

    target = target_total if target_total is not None else _REFERENCE_LIMIT_DEFAULT
    floor = min_total if min_total is not None else _REFERENCE_LIMIT_MIN
    ceiling = max_total if max_total is not None else _REFERENCE_LIMIT_MAX

    chinese = sorted(
        (p for p in papers if _is_chinese(p)),
        key=lambda p: _chinese_sort_key(p, scores),
    )
    english = sorted(
        (p for p in papers if not _is_chinese(p)),
        key=lambda p: _english_sort_key(p, scores),
    )
    total_available = len(chinese) + len(english)

    # 软上限:目标 +10%(90 → 99)。91 篇的池子不值得动刀,配额不凑整。
    soft_ceiling = ceiling + max(5, round(ceiling * 0.1))
    if total_available <= soft_ceiling:
        return list(papers), []

    en_keep = min(len(english), math.ceil(target * _EN_SHARE_MAX))
    zh_keep = min(len(chinese), target - en_keep)
    # 中文池不足且总量跌破下限时,英文才越占比线回填(纯英文池可到 90)。
    shortfall = max(0, floor - zh_keep - en_keep)
    en_keep += min(len(english) - en_keep, shortfall)

    selected = chinese[:zh_keep] + english[:en_keep]
    selected_ids = {p.lit_id for p in selected}
    cuts = [
        _make_cut(p, scores)
        for p in papers
        if p.lit_id not in selected_ids
    ]
    return selected, cuts
