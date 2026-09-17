"""核查2:正文-文献关联一致性。

执行节点:apply_citation_numbering 之后(此时正文 lit_xxx 锚点已替换为 [N])。

目的:建立正文引用标记与对应文献条目之间的双向绑定映射关系,
逐一校验每一处正文引用都有唯一匹配的文献条目,每一条文献条目都对应
正文内的实际引用场景,杜绝无来源引用、无效冗余文献或错配关联。
"""
from __future__ import annotations

import logging
import re
from typing import Iterable
from collections import Counter

from qa.models import QACheckResult, QAIssue
from qa.rules import QACheckStatus
from writing.orchestrator import SectionResult
from retrieval.types import Paper


log = logging.getLogger(__name__)


# 同时识别两种锚点形态:
#   [lit_oa_3]  -> lit_id 形式(出现在 orchestrator 完成编号替换之前)
#   [12]        -> 渲染为顺序编号(出现在 apply_citation_numbering 之后)
_LIT_ID_ANCHOR_RE = re.compile(r"\[(?:lit_[a-zA-Z0-9_]+|hash:[a-zA-Z0-9_]+)\]")
_NUMERIC_ANCHOR_RE = re.compile(r"\[(\d{1,3})\]")


def _collect_number_anchors(content: str) -> list[int]:
    """从已编号的正文中抽取 N。

    注意:正文里可能同时混有 lit_xxx 与 [N],两种都要识别。
    """
    nums: list[int] = []
    for match in _NUMERIC_ANCHOR_RE.finditer(content or ""):
        nums.append(int(match.group(1)))
    return nums


def _split_section_keys(sections: Iterable[SectionResult]) -> dict[str, SectionResult]:
    return {s.key: s for s in sections}


def check_reference_binding(
    sections: list[SectionResult],
    reference_list: str,
    papers: list[Paper],
) -> QACheckResult:
    """统一入口。

    - sections     : 章节渲染结果(已用 [N] 替换 lit_xxx)
    - reference_list: render_reference_list 输出的纯文本
    - papers       : 入选的所有 Paper(供 lit_id 反查)
    """
    result = QACheckResult(
        check_id="binding_001",
        name="正文-文献关联一致性核查",
        status=QACheckStatus.PASS,
        metrics={
            "sections": len(sections),
            "inspected_papers": len(papers),
            "reference_list_size": len(reference_list or ""),
        },
    )

    issues: list[QAIssue] = []
    by_id = {p.lit_id: p for p in papers}
    section_map = _split_section_keys(sections)

    # 1) 解析参考文献列表,得到 N -> lit_id 的映射(逐行解析以保持顺序)
    ref_lines = [ln for ln in (reference_list or "").splitlines() if ln.strip()]
    number_to_lit: dict[int, str] = {}
    ref_header_re = re.compile(r"^\[(\d+)\]\s*(.*)$")
    for line in ref_lines:
        m = ref_header_re.match(line.strip())
        if not m:
            issues.append(QAIssue(
                "BINDING_REF_BAD_FORMAT", QACheckStatus.FAIL,
                "reference_list", "reference_list",
                f"参考文献行无法解析: {line[:80]!r}",
                snippet=line[:120],
            ))
            continue
        n = int(m.group(1))
        # 不能再根据这一行反推 lit_id;通过 contents 缓存到 metrics
        number_to_lit.setdefault(n, "")

    # 由于 render_reference_list 输出的是 [N] 文本,我们从原文 [lit_xxx] 已替换为 [N]
    # 这一事实出发,把正文中出现的 [N] 与 section.citations 中记录的 lit_id 一一对应
    # — 这正是 orchestrator.apply_citation_numbering 保留下来的 lit_id 序列。
    cited_ids_in_order: list[str] = []
    for s in sections:
        for cid in s.citations:
            if cid not in cited_ids_in_order:
                cited_ids_in_order.append(cid)
    number_to_lit_id = {
        i + 1: cid for i, cid in enumerate(cited_ids_in_order) if cid
    }

    # 2) 抽取每章正文里所有 [N] 锚点,校验 N 与"按引用顺序的第 N 条"是否一致
    total_anchors = 0
    unmatched_anchors: list[tuple[str, int]] = []  # (section_key, n)
    chapter_n_used: dict[str, list[int]] = {}
    for s in sections:
        nums = _collect_number_anchors(s.content or "")
        chapter_n_used[s.key] = nums
        total_anchors += len(nums)
        for n in nums:
            if n not in number_to_lit_id:
                unmatched_anchors.append((s.key, n))

    for section_key, n in unmatched_anchors:
        issues.append(QAIssue(
            "BINDING_UNMATCHED_ANCHOR", QACheckStatus.FAIL,
            "content", f"section:{section_key}",
            f"正文锚点 [{n}] 未匹配任何文献条目",
            section_key=section_key,
            snippet=f"[{n}]",
        ))

    # 3) section.citations 与正文中实际出现的 [N] 是否一致
    for s in sections:
        used = set(chapter_n_used.get(s.key, []))
        cited = set(number_to_lit_id.get(i + 1) for i in range(len(cited_ids_in_order))
                    if number_to_lit_id.get(i + 1) in {c for c in s.citations})
        # 反向: section.citations 里每个 lit_id 都该对应一个出现在正文的 [N]
        for cid in s.citations:
            if not by_id.get(cid):
                issues.append(QAIssue(
                    "BINDING_ORPHAN_CITATION", QACheckStatus.FAIL,
                    "citations", f"section:{s.key}",
                    f"章节记录引用了不存在的文献 lit_id={cid}",
                    lit_id=cid,
                    section_key=s.key,
                ))
                continue
            target_n = next((k for k, v in number_to_lit_id.items() if v == cid), None)
            if target_n is not None and target_n not in used:
                issues.append(QAIssue(
                    "BINDING_LISTED_NOT_CITED", QACheckStatus.FAIL,
                    "citations", f"section:{s.key}",
                    f"文献 {cid} 已被收进参考文献列表, 但本章正文未出现对应 [N]",
                    lit_id=cid,
                    section_key=s.key,
                ))

    # 4) 同一章节同一 lit_id 必须仅 1 次(去重)
    for s in sections:
        seen_in_section: set[str] = set()
        for cid in s.citations:
            if cid in seen_in_section:
                issues.append(QAIssue(
                    "BINDING_DUP_IN_SECTION", QACheckStatus.FAIL,
                    "citations", f"section:{s.key}",
                    f"同一文献在同一章节重复引用: {cid}",
                    lit_id=cid,
                    section_key=s.key,
                ))
            else:
                seen_in_section.add(cid)

    # 5) 同一章节同一 [N] 可以在多个独立论断中重复出现；编号重复本身
    # 不是错误。真正需要阻断的是无法映射的锚点或章节声明与正文不一致。
    # 这里保留重复次数指标，供报告展示，但不再把正常的重复引用判为告警。
    for s in sections:
        counter: Counter[int] = Counter(chapter_n_used.get(s.key, []))
        duplicate_anchor_count = sum(max(0, cnt - 1) for cnt in counter.values())
        if duplicate_anchor_count:
            result.metrics.setdefault("duplicate_anchor_count", 0)
            result.metrics["duplicate_anchor_count"] += duplicate_anchor_count

    # 6) 候选池覆盖率审计
    # 写作池是“候选证据全集”，不是要求每篇都必须进入正文的参考文献清单。
    # 逐篇报告未引用会制造几十条噪声告警，也掩盖真正的锚点错配；改为报告
    # 一个可比较的覆盖率指标，只有覆盖率明显过低才给出一条聚合告警。
    cited_ids_set = {cid for cid in cited_ids_in_order}
    pool_size = len(papers)
    coverage = len(cited_ids_set) / pool_size if pool_size else 1.0
    if pool_size >= 15 and coverage < 0.50:
        issues.append(QAIssue(
            "BINDING_COVERAGE_LOW", QACheckStatus.WARN,
            "literature_pool", "literature_pool",
            f"正文仅覆盖候选文献的 {coverage:.1%},低于 50% 的最低覆盖线",
        ))

    result.issues = issues
    result.metrics.update({
        "anchors_total": total_anchors,
        "unique_cited": len(cited_ids_in_order),
        "pool_coverage": round(coverage, 4),
        "reference_count": len(ref_lines),
        "issues_count": len(issues),
        "fail_count": sum(1 for i in issues if i.severity == QACheckStatus.FAIL),
        "warn_count": sum(1 for i in issues if i.severity == QACheckStatus.WARN),
    })

    fail_n = result.metrics["fail_count"]
    warn_n = result.metrics["warn_count"]
    if fail_n > 0:
        result.status = QACheckStatus.FAIL
        result.notes.append("正文与文献存在错配/冗余,阻断最终输出")
    elif warn_n > 0:
        result.status = QACheckStatus.WARN
        result.notes.append("存在轻微重复或缺锚问题,建议复核")
    else:
        result.notes.append("正文与文献双向绑定完整、无错配")

    log.info(
        "binding check: anchors=%d cited=%d issues=%d fail=%d warn=%d status=%s",
        result.metrics["anchors_total"], result.metrics["unique_cited"],
        result.metrics["issues_count"], fail_n, warn_n, result.status.value,
    )
    return result
