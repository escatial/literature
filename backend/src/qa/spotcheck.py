"""成稿后抽查回源核验(v9.10,对应学术规范"至少3次核查且有记录"的第3次)。

入池时已做过真实性核查(VERIFY_1/2,canonical_work + verification_run),
成稿后再按固定种子抽取 30% 样本回源复核元数据:
- 有 DOI 的文献回 Crossref 官方 API,题名逐字比对(归一化后);
- 无 DOI 的中文文献无公开核验接口,记为 skipped 并留痕(不冒充通过)。

固定种子 = 环境变量 QA_SPOTCHECK_SEED,缺省用池内 lit_id 列表的稳定哈希
——同一文献池每次抽到同一批样本,过程可复现、可写进核查记录。

网络失败按"不可达"留痕,不冒充通过也不阻断交付(整体 WARN);
本检查需要外网,测试套件默认通过 QA_SPOTCHECK=0 关闭。
"""
from __future__ import annotations

import difflib
import hashlib
import json
import logging
import os
import random
import re
import urllib.request
from typing import TYPE_CHECKING

from qa.models import QACheckResult, QAIssue
from qa.rules import QACheckStatus
from retrieval.types import Paper

if TYPE_CHECKING:
    from writing.orchestrator import SectionResult  # pragma: no cover

log = logging.getLogger(__name__)

_ENV_TOGGLE = "QA_SPOTCHECK"
_ENV_SEED = "QA_SPOTCHECK_SEED"
_CROSSREF_API = "https://api.crossref.org/works/{doi}"
_TIMEOUT_SECONDS = 10.0
_SAMPLE_RATIO = 0.3
_SAMPLE_MIN = 5
_SAMPLE_MAX = 30
_TITLE_SIMILAR_THRESHOLD = 0.90


def _normalize_title(text: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", (text or "").lower())


def spotcheck_enabled() -> bool:
    return os.environ.get(_ENV_TOGGLE, "1") not in {"0", "false", "no", "off"}


def _stable_seed(papers: list[Paper]) -> int:
    explicit = os.environ.get(_ENV_SEED, "").strip()
    if explicit:
        return int(explicit) & 0xFFFFFFFF
    digest = hashlib.sha256(
        "|".join(sorted(p.lit_id for p in papers)).encode("utf-8")
    ).hexdigest()
    return int(digest[:8], 16)


def _fetch_crossref_title(doi: str) -> str | None:
    """回源 Crossref 取官方题名;网络失败返回 None(不可达,与不匹配区分)。

    SSRF 收窄:Crossref 为固定公网 API base,DOI 经 quote 编码后拼入
    路径,不含可操控的 host 成分;请求前经 validate_external_url 校验
    (仅放行公网 http/https,紧邻 urlopen 静态可见)。校验不过按
    "不可达"留痕返回 None,与网络失败同语义。
    """
    from urllib.parse import quote

    from security.auth import validate_external_url

    url = _CROSSREF_API.format(doi=quote(doi.strip(), safe=""))
    try:
        req = urllib.request.Request(
            validate_external_url(url),
            headers={"User-Agent": "lit-review-agent/9.10 (mailto:check@example.org)"},
        )
    except ValueError as exc:
        log.warning("Crossref 回源地址被拒(%s): %s", url, exc)
        return None
    with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    titles = (payload.get("message", {}) or {}).get("title") or []
    return str(titles[0]).strip() if titles else None


def check_source_spotcheck(papers: list[Paper]) -> QACheckResult:
    """固定种子抽查 30% 回源核验。网络失败与无 DOI 都留痕,不冒充通过。"""
    result = QACheckResult(
        check_id="spotcheck_001",
        name="成稿抽查回源核验",
        status=QACheckStatus.PASS,
        metrics={},
    )
    with_doi = sorted(
        (p for p in papers if (p.doi or "").strip()),
        key=lambda p: p.lit_id,
    )
    if not with_doi:
        result.status = QACheckStatus.SKIPPED
        result.notes.append("文献池中没有带 DOI 的文献,无公开回源接口,抽查跳过")
        return result

    sample_size = max(_SAMPLE_MIN, min(_SAMPLE_MAX, round(len(with_doi) * _SAMPLE_RATIO)))
    sample_size = min(sample_size, len(with_doi))
    sample = random.Random(_stable_seed(papers)).sample(with_doi, sample_size)

    records: list[dict] = []
    matched = mismatched = unreachable = skipped = 0
    for paper in sample:
        record: dict = {
            "lit_id": paper.lit_id,
            "doi": paper.doi,
            "title": (paper.title or "")[:60],
        }
        try:
            remote_title = _fetch_crossref_title(paper.doi)
        except Exception as exc:  # noqa: BLE001
            unreachable += 1
            record.update({"status": "unreachable", "detail": str(exc)[:120]})
            records.append(record)
            continue
        if not remote_title:
            skipped += 1
            record.update({"status": "skipped", "detail": "Crossref 无题名记录"})
            records.append(record)
            continue
        similarity = difflib.SequenceMatcher(
            None,
            _normalize_title(paper.title or ""),
            _normalize_title(remote_title),
        ).ratio()
        if similarity >= _TITLE_SIMILAR_THRESHOLD:
            matched += 1
            record.update({"status": "matched", "similarity": round(similarity, 4)})
        else:
            mismatched += 1
            record.update({
                "status": "mismatched",
                "similarity": round(similarity, 4),
                "remote_title": remote_title[:80],
            })
            result.issues.append(QAIssue(
                "SPOTCHECK_TITLE_MISMATCH", QACheckStatus.WARN,
                "title", f"paper/{paper.lit_id}",
                f"回源题名不一致(相似度 {similarity:.2f},doi={paper.doi}): "
                f"本地「{(paper.title or '')[:40]}」 vs Crossref「{remote_title[:40]}」,"
                "请人工核对 DOI 与元数据",
                lit_id=paper.lit_id,
            ))
        records.append(record)

    no_doi = len(papers) - len(with_doi)
    result.metrics.update({
        "pool_size": len(papers),
        "with_doi": len(with_doi),
        "sampled": len(sample),
        "sample_ratio": round(len(sample) / len(papers), 3) if papers else 0,
        "seed_source": os.environ.get(_ENV_SEED, "").strip() or "pool-hash",
        "matched": matched,
        "mismatched": mismatched,
        "unreachable": unreachable,
        "skipped_no_title": skipped,
        "no_doi_skipped": no_doi,
        "records": records[:60],
    })
    if mismatched:
        result.status = QACheckStatus.WARN
        result.notes.append(f"抽查 {len(sample)} 篇,{mismatched} 篇回源题名不一致,需人工核对")
    elif unreachable == len(sample):
        result.status = QACheckStatus.WARN
        result.notes.append(f"抽查 {len(sample)} 篇均无法访问回源接口,本次核查未完成(留痕)")
    else:
        result.notes.append(
            f"抽查 {len(sample)} 篇(固定种子可复现),回源一致 {matched} 篇,"
            f"不可达 {unreachable} 篇,无 DOI 未抽查 {no_doi} 篇"
        )
    log.info(
        "spot check: sampled=%d matched=%d mismatch=%d unreachable=%d",
        len(sample), matched, mismatched, unreachable,
    )
    return result
