"""核查4:引用格式规范性 (GB/T 7714-2025)。

执行节点:render_reference_list 之后。

目的:逐一校验每篇文献的著录要素(作者、题名、出处、年份、卷期页码等)
的排序、标点、缩写格式是否完全符合选定规范的要求。
英文文献引用只能是英文,不能出现中文(语种一致性硬约束)。

实现策略:
- 中文条目(raw_citation): 基于已有的 _clean_dirty + 标准著录要素序列做检查;
  由于原文来自用户粘贴,只做"是否存在明显遗漏"的弱校验,不擅自拼接。
- 英文条目(OpenAlex / PubMed): 重用 writing.citeproc_renderer 的 citeproc 规则
  引擎拿到渲染结果,再做语种一致性 + 关键要素完备度校验。
"""
from __future__ import annotations

import logging
import re
from collections import Counter

from qa.models import QACheckResult, QAIssue
from qa.rules import QACheckStatus
from retrieval.types import Paper, Source


log = logging.getLogger(__name__)


_CHINESE_SOURCES: frozenset[Source] = frozenset({Source.CNKI, Source.USER_IMPORTED})
_ENGLISH_SOURCES: frozenset[Source] = frozenset({
    Source.OPENALEX,
    Source.PUBMED,
    Source.CROSSREF,
})


_CN_CHAR_RE = re.compile(r"[\u4e00-\u9fff]")
_LATIN_LETTER_RE = re.compile(r"[A-Za-z]")
_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
_DOI_RE = re.compile(r"\b10\.\d{3,9}/[^\s\"<>]+", re.IGNORECASE)


def _has_chinese(text: str) -> bool:
    return bool(_CN_CHAR_RE.search(text or ""))


def _has_latin(text: str) -> bool:
    return bool(_LATIN_LETTER_RE.search(text or ""))


def _check_lang_consistency(papers: list[Paper], rendered: dict[str, str]) -> list[QAIssue]:
    """语种一致性:中文条目不能出现英文渲染;英文条目渲染里不允许中文字符。"""
    issues: list[QAIssue] = []
    for paper in papers:
        path = f"paper/{paper.lit_id}"
        text = rendered.get(paper.lit_id) or ""

        if paper.source in _CHINESE_SOURCES:
            raw = paper.raw_citation or text
            if not _has_chinese(raw):
                issues.append(QAIssue(
                    "CITATION_FORMAT_LANG_LOSS", QACheckStatus.FAIL,
                    "raw_citation", path,
                    "中文条目引文不含中文,疑似被英文库覆盖或粘贴异常",
                    lit_id=paper.lit_id,
                    snippet=raw[:80],
                ))
            # 反向不应全是英文:中文条目也不该是英文渲染
            if _has_latin(raw) and not _has_chinese(raw):
                issues.append(QAIssue(
                    "CITATION_FORMAT_LANG_MIX", QACheckStatus.FAIL,
                    "raw_citation", path,
                    "中文条目渲染为纯英文,疑似错配其他英文数据库",
                    lit_id=paper.lit_id,
                ))

        if paper.source in _ENGLISH_SOURCES:
            if _has_chinese(text):
                issues.append(QAIssue(
                    "CITATION_FORMAT_LANG_MIX", QACheckStatus.FAIL,
                    "rendered", path,
                    "英文条目渲染结果包含中文字符,违反语种一致性",
                    lit_id=paper.lit_id,
                    snippet=text[:120],
                ))
    return issues


def _check_required_elements(papers: list[Paper], rendered: dict[str, str]) -> list[QAIssue]:
    """著录必备要素检查(作者/题名/年份/出处)。

    - 中文条目: 用 raw_citation + 论文字段共同判断(用户已粘贴的优先)
    - 英文条目: 用 citeproc 渲染结果 + 论文字段共同判断
    """
    issues: list[QAIssue] = []
    for paper in papers:
        path = f"paper/{paper.lit_id}"
        text = rendered.get(paper.lit_id, "")

        # 1) 作者
        authors = [a for a in (paper.authors or []) if (a or "").strip()]
        if not authors:
            issues.append(QAIssue(
                "CITATION_FORMAT_MISSING_AUTHORS", QACheckStatus.FAIL,
                "authors", path, "作者缺失, 著录要素不完整",
                lit_id=paper.lit_id,
            ))

        # 2) 年份
        if not paper.year or paper.year <= 0:
            issues.append(QAIssue(
                "CITATION_FORMAT_MISSING_YEAR", QACheckStatus.FAIL,
                "year", path, "年份缺失, 著录要素不完整",
                lit_id=paper.lit_id,
            ))
        elif text and not _YEAR_RE.search(text):
            issues.append(QAIssue(
                "CITATION_FORMAT_YEAR_NOT_EMBEDDED", QACheckStatus.WARN,
                "year", path, "渲染结果中找不到年份, 可能有遗漏",
                lit_id=paper.lit_id,
            ))

        # 3) 题名
        if not (paper.title or "").strip():
            issues.append(QAIssue(
                "CITATION_FORMAT_MISSING_TITLE", QACheckStatus.FAIL,
                "title", path, "题名缺失, 著录要素不完整",
                lit_id=paper.lit_id,
            ))

        # 4) 出处(中文 = 期刊名/学校/出版社; 英文 = 期刊名)
        if paper.source in _ENGLISH_SOURCES and not (paper.journal or "").strip():
            issues.append(QAIssue(
                "CITATION_FORMAT_MISSING_JOURNAL", QACheckStatus.FAIL,
                "journal", path, "英文条目期刊名为空, 无法著录",
                lit_id=paper.lit_id,
            ))
        if paper.source in _CHINESE_SOURCES:
            if not (paper.journal or "").strip() and not (paper.raw_citation or "").strip():
                issues.append(QAIssue(
                    "CITATION_FORMAT_MISSING_SOURCE", QACheckStatus.FAIL,
                    "journal", path,
                    "中文条目同时缺少期刊/学校/出版社, 无法著录",
                    lit_id=paper.lit_id,
                ))

    return issues


def _check_punctuation_and_order(papers: list[Paper], rendered: dict[str, str]) -> list[QAIssue]:
    """标点与字段顺序启发式校验。

    GB/T 7714-2025 通用约定:
    - 中文条目以句点 '.' 结束(国际惯例是 '.').
    - 英文条目以 '.' 结束.
    - 年份后跟 . 或 , (取决于字段类别), 不应紧贴作者.

    这部分只做最低限度的"明显错误"标记,不做严格语法校验(citeproc 已覆盖).
    """
    issues: list[QAIssue] = []
    for paper in papers:
        path = f"paper/{paper.lit_id}"
        text = (rendered.get(paper.lit_id) or "").strip()
        if not text:
            issues.append(QAIssue(
                "CITATION_FORMAT_EMPTY", QACheckStatus.FAIL,
                "rendered", path, "渲染结果为空",
                lit_id=paper.lit_id,
            ))
            continue
        # 末尾标点: 不允许以逗号、分号、空格结尾
        if text[-1] in ",; ":
            issues.append(QAIssue(
                "CITATION_FORMAT_TRAILING_PUNCT", QACheckStatus.WARN,
                "rendered", path, f"末尾可疑标点: {text[-1]!r}",
                lit_id=paper.lit_id,
            ))
        # 不允许中间出现连续三个 '.' (省略号除外)
        if "...." in text:
            issues.append(QAIssue(
                "CITATION_FORMAT_DOTS", QACheckStatus.WARN,
                "rendered", path, "渲染结果出现连续省略号,可能字段缺失",
                lit_id=paper.lit_id,
            ))
    return issues


def _check_render_quality(papers: list[Paper], rendered: dict[str, str]) -> list[QAIssue]:
    """渲染质量:不允许出现明显模板占位符 / 未替换变量。"""
    issues: list[QAIssue] = []
    placeholders = ("{title}", "{author}", "{year}", "{journal}", "[未知]", "N/A")
    for paper in papers:
        path = f"paper/{paper.lit_id}"
        text = rendered.get(paper.lit_id, "") or ""
        for token in placeholders:
            if token.lower() in text.lower():
                issues.append(QAIssue(
                    "CITATION_FORMAT_PLACEHOLDER", QACheckStatus.FAIL,
                    "rendered", path, f"渲染残留占位符: {token}",
                    lit_id=paper.lit_id,
                    snippet=text[:120],
                ))
                break
    return issues


def _render_papers(papers: list[Paper], style_id: str) -> dict[str, str]:
    """调用 citeproc 规则引擎批量渲染英文条目;中文条目直接返回 raw_citation。"""
    out: dict[str, str] = {}
    try:
        from writing.citeproc_renderer import format_citation_via_citeproc
    except Exception:
        format_citation_via_citeproc = None  # type: ignore[assignment]

    for paper in papers:
        if paper.source in _CHINESE_SOURCES:
            out[paper.lit_id] = (paper.raw_citation or "").strip()
        elif paper.source in _ENGLISH_SOURCES and format_citation_via_citeproc is not None:
            try:
                rendered = format_citation_via_citeproc(paper, style_id=style_id)
            except Exception as exc:  # noqa: BLE001
                log.warning("citeproc 渲染失败: %s / %s", paper.lit_id, exc)
                rendered = ""
            out[paper.lit_id] = (rendered or "").strip()
        else:
            out[paper.lit_id] = ""
    return out


def check_citation_format(
    papers: list[Paper],
    *,
    style_id: str = "china-national-standard-gb-t-7714-2025-numeric",
) -> QACheckResult:
    """统一入口。

    - papers  : 全部入选文献
    - style_id: 选定的 CSL 样式,默认 GB/T 7714-2025 顺序编码制
    """
    result = QACheckResult(
        check_id="citation_format_001",
        name="引用格式规范性核查",
        status=QACheckStatus.PASS,
        metrics={
            "inspected": len(papers),
            "style": style_id,
            "by_source": dict(Counter(p.source.value for p in papers)),
        },
    )

    rendered = _render_papers(papers, style_id)

    issues: list[QAIssue] = []
    issues.extend(_check_lang_consistency(papers, rendered))
    issues.extend(_check_required_elements(papers, rendered))
    issues.extend(_check_punctuation_and_order(papers, rendered))
    issues.extend(_check_render_quality(papers, rendered))

    result.metrics["rendered_chars_total"] = sum(len(t) for t in rendered.values())
    result.metrics["rendered_chars_by_lang"] = {
        "chinese": sum(len(t) for lid, t in rendered.items()
                        if next((p for p in papers if p.lit_id == lid), None)
                        and (next((p for p in papers if p.lit_id == lid), None).source
                              in _CHINESE_SOURCES)),
        "english": sum(len(t) for lid, t in rendered.items()
                       if next((p for p in papers if p.lit_id == lid), None)
                       and (next((p for p in papers if p.lit_id == lid), None).source
                             in _ENGLISH_SOURCES)),
    }
    result.metrics["issues_count"] = len(issues)
    result.metrics["fail_count"] = sum(1 for i in issues if i.severity == QACheckStatus.FAIL)
    result.metrics["warn_count"] = sum(1 for i in issues if i.severity == QACheckStatus.WARN)
    result.issues = issues

    fail_n = result.metrics["fail_count"]
    warn_n = result.metrics["warn_count"]
    if fail_n > 0:
        result.status = QACheckStatus.FAIL
        result.notes.append("存在格式硬错(语种混用/必备要素缺失等),阻断最终输出")
    elif warn_n > 0:
        result.status = QACheckStatus.WARN
        result.notes.append("存在轻微格式告警,需要复核")
    else:
        result.notes.append(f"全部 {len(papers)} 条文献符合 {style_id}")

    log.info(
        "citation_format check: inspected=%d style=%s issues=%d fail=%d warn=%d status=%s",
        result.metrics["inspected"], style_id, result.metrics["issues_count"],
        fail_n, warn_n, result.status.value,
    )
    return result
