"""三重核验 Agent(方案 §3 "三重核验 Agent")。

输入: 标准 canonical_work 记录 + 来源快照
输出: verification_run 记录 + verification_status 字段更新

核验项(方案 §3 "未完成核验不得放行"):
  1. is_journal      —— 类型是否为期刊论文(非预印本/书章/会议)
  2. year_ok         —— 年份在合理范围(<= 当前年)
  3. metadata_complete —— 标题/作者/期刊/年份至少齐全
  4. source_reachable  —— 详情页 URL 或 DOI 可达(简化实现:URL/DOI 非空即通过)

设计要点:
- 每条 canonical_work 每次核验都产生一条 VerificationRun(可追溯);
- 核验失败时,canonical_work.verification_status=FAIL,后续入库门禁拒绝;
- 支持 dry_run 模式(只计算,不落库),用于批量评估;
- 不依赖 LLM,纯规则(LLM 时代会被幻觉污染,门禁必须可解释)。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from db.models import CanonicalWorkModel, SourceSnapshotModel, VerificationRunModel
from db.session import SessionLocal
from retrieval.types import Paper

log = logging.getLogger(__name__)


# 视为期刊论文的合法 work_type / 来源
JOURNAL_TYPES = {"journal", "journal-article", "article", "review"}
# 这些来源(枚举值)的论文均视为期刊来源,即使 work_type 字段缺失
JOURNAL_SOURCES = {
    "cnki", "openalex", "crossref", "pubmed",
}

# 视为「非期刊」的来源/类型(预印本、书章、博文等)
NON_JOURNAL_TYPE_HINTS = ("preprint", "chapter", "blog", "thesis", "report", "monograph")

# 核验规则版本(对应 quality_gate.rule_version)
RULE_VERSION = "v1"


@dataclass
class FieldCheck:
    """单个核验字段的结果。"""
    name: str
    status: str   # pass / fail
    detail: str = ""


@dataclass
class VerificationResult:
    """一条核验的完整结果(纯函数输出,不落库)。"""
    canonical_id: str
    field_checks: list[FieldCheck] = field(default_factory=list)
    overall_status: str = "PENDING"  # PENDING / PASS / FAIL
    reason: str = ""

    @property
    def is_pass(self) -> bool:
        return self.overall_status == "PASS"


def verify_paper(paper: Paper | dict) -> VerificationResult:
    """对单条 Paper / dict 做三重核验(纯函数,不落库)。

    输入兼容 Paper dataclass 与 dict(paper.dict())。
    """
    if isinstance(paper, Paper):
        cid = paper.lit_id
        work_type = (paper.source or "unknown").lower()
        title = paper.title or ""
        authors = paper.authors or []
        journal = paper.journal or ""
        year = paper.year or 0
        doi = paper.doi
        source_url = paper.source_url or ""
    else:
        cid = str(paper.get("canonical_id") or paper.get("lit_id") or "")
        work_type = (paper.get("work_type") or paper.get("source") or "unknown").lower()
        title = paper.get("title") or ""
        authors = paper.get("authors") or []
        journal = paper.get("journal") or ""
        year = int(paper.get("year") or 0)
        doi = paper.get("doi")
        source_url = paper.get("source_url") or ""

    checks: list[FieldCheck] = []

    # 1. is_journal
    # 优先 work_type;若没有,fallback 到 source(兼容 Paper.source 枚举 + dict['source'])
    type_ok = (
        (work_type in JOURNAL_TYPES
         and not any(hint in work_type for hint in NON_JOURNAL_TYPE_HINTS))
        or (work_type in JOURNAL_SOURCES)
    )
    is_journal_ok = type_ok

    # 兼容 Paper dataclass:source 可能是 Source 枚举
    if not is_journal_ok and hasattr(paper, "source"):
        src_value = getattr(paper.source, "value", str(paper.source)).lower()
        if src_value in JOURNAL_SOURCES:
            is_journal_ok = True

    checks.append(FieldCheck(
        name="is_journal",
        status="pass" if is_journal_ok else "fail",
        detail=f"work_type={work_type}",
    ))

    # 2. year_ok
    current_year = datetime.now(timezone.utc).year
    year_ok = 1900 <= year <= current_year
    checks.append(FieldCheck(
        name="year_ok",
        status="pass" if year_ok else "fail",
        detail=f"year={year} (current={current_year})",
    ))

    # 3. metadata_complete
    meta_ok = bool(title.strip()) and bool(authors) and bool(journal.strip()) and bool(year)
    checks.append(FieldCheck(
        name="metadata_complete",
        status="pass" if meta_ok else "fail",
        detail=f"title={'Y' if title.strip() else 'N'} "
               f"authors={len(authors)} "
               f"journal={'Y' if journal.strip() else 'N'} "
               f"year={year}",
    ))

    # 4. source_reachable
    has_doi = bool(doi and re.match(r"^10\.\d{4,9}/.+", doi.strip(), flags=re.I))
    has_url = bool(source_url and source_url.startswith(("http://", "https://")))
    source_ok = has_doi or has_url
    checks.append(FieldCheck(
        name="source_reachable",
        status="pass" if source_ok else "fail",
        detail=f"doi={'Y' if has_doi else 'N'} url={'Y' if has_url else 'N'}",
    ))

    overall = "PASS" if all(c.status == "pass" for c in checks) else "FAIL"
    fail_reasons = [f"{c.name}({c.detail})" for c in checks if c.status == "fail"]
    reason = "; ".join(fail_reasons) if fail_reasons else "all checks passed"

    return VerificationResult(
        canonical_id=cid,
        field_checks=checks,
        overall_status=overall,
        reason=reason,
    )


def upsert_canonical_work(
    *,
    paper: Paper,
    snapshot: SourceSnapshotModel | None = None,
) -> CanonicalWorkModel:
    """把 Paper 写进 canonical_works(幂等 upsert by identity_key)。

    注意:本函数不写 verification_runs,核验由调用方在写入后触发 verify_and_update()。
    """
    from retrieval.paper_identity import cross_source_key

    identity = cross_source_key(
        title=paper.title or "",
        authors=paper.authors or [],
        year=paper.year or 0,
        doi=paper.doi,
    )
    with SessionLocal() as db:
        row = db.query(CanonicalWorkModel).filter_by(identity_key=identity).one_or_none()
        if row is None:
            # canonical_id 取 identity 的稳定散列(基于 identity 的 SHA256 前 12 位)
            import hashlib as _hl
            digest = _hl.sha256(identity.encode("utf-8")).hexdigest()[:12]
            row = CanonicalWorkModel(
                canonical_id=f"can_{digest}",
                identity_key=identity,
                title=paper.title or "",
                authors=list(paper.authors or []),
                journal=paper.journal or "",
                year=paper.year or 0,
                volume=paper.volume,
                issue=paper.issue,
                pages=paper.pages,
                doi=paper.doi,
                work_type="journal",
            )
            db.add(row)
        else:
            # 更新字段(若新值更全)
            if not row.title and paper.title:
                row.title = paper.title
            if not row.authors and paper.authors:
                row.authors = list(paper.authors)
            if not row.journal and paper.journal:
                row.journal = paper.journal
            if not row.year and paper.year:
                row.year = paper.year
            if not row.volume and paper.volume:
                row.volume = paper.volume
            if not row.issue and paper.issue:
                row.issue = paper.issue
            if not row.pages and paper.pages:
                row.pages = paper.pages
            if not row.doi and paper.doi:
                row.doi = paper.doi
        if snapshot is not None:
            snapshot.canonical_id = row.canonical_id
            db.add(snapshot)
        db.commit()
        db.refresh(row)
        return row


def verify_and_update(canonical_id: str) -> VerificationResult:
    """对已入库的 canonical_work 跑核验并落库 verification_run,更新 verification_status。"""
    with SessionLocal() as db:
        row = db.query(CanonicalWorkModel).filter_by(canonical_id=canonical_id).one_or_none()
        if row is None:
            raise ValueError(f"canonical_work 不存在:{canonical_id}")

        result = verify_paper({
            "canonical_id": row.canonical_id,
            "title": row.title,
            "authors": row.authors,
            "journal": row.journal,
            "year": row.year,
            "doi": row.doi,
            "work_type": row.work_type,
        })

        run = VerificationRunModel(
            run_id=f"ver_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}",
            canonical_id=row.canonical_id,
            rule_version=RULE_VERSION,
            field_results={
                c.name: {"status": c.status, "detail": c.detail}
                for c in result.field_checks
            },
            status=result.overall_status,
            reason=result.reason,
        )
        db.add(run)
        row.verification_status = result.overall_status
        row.last_verified_at = datetime.now(timezone.utc)
        db.commit()
        return result


def enforce_gate(canonical_id: str) -> bool:
    """引用门禁(方案 §4):只有 PASS 的 canonical_work 才允许被 claim/citation_link 引用。"""
    with SessionLocal() as db:
        row = db.query(CanonicalWorkModel).filter_by(canonical_id=canonical_id).one_or_none()
        if row is None:
            log.warning("引用门禁拦截:canonical_work 不存在 (%s)", canonical_id)
            return False
        if row.verification_status != "PASS":
            log.warning(
                "引用门禁拦截:canonical_work=%s status=%s",
                canonical_id, row.verification_status,
            )
            return False
        return True


__all__ = [
    "verify_paper",
    "upsert_canonical_work",
    "verify_and_update",
    "enforce_gate",
    "VerificationResult",
    "FieldCheck",
    "RULE_VERSION",
]