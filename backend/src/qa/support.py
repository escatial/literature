"""引用支持性裁判(support judge,v9.9 新增,方案 M3)。

binding 核查只验证"引用存在且与本池匹配",查不出"引错对象"——
把 A 文献的结论挂到 B 文献头上。本裁判对每个带引用的正文论断判定:
  2 = 所引文献的摘要/证据完全支持该论断
  1 = 部分支持(论断强于证据)
  0 = 不支持(疑似张冠李戴/过度推断)
判 0 的引用产 WARN 并进入审计报告。

设计约定(借鉴 arxiv-research-agent 的支撑性裁判):
- 按章批量判定一次,不逐引用调用,控制成本;
- 默认关闭:QA 门禁此前是纯规则、可解释、零成本,裁判属于 LLM 判定,
  通过环境变量 QA_SUPPORT_JUDGE=1 显式开启,灰度期间只产 WARN;
- 上线前应先建评测集(人工标注含构造错误的样例,量出漏判/误杀率),
  评测数据进 eval_sets 表。
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import TYPE_CHECKING

from qa.models import QACheckResult, QAIssue
from qa.rules import QACheckStatus
from retrieval.types import Paper

if TYPE_CHECKING:
    from writing.orchestrator import SectionResult  # pragma: no cover

log = logging.getLogger(__name__)

_ENV_TOGGLE = "QA_SUPPORT_JUDGE"
_NUMERIC_ANCHOR_RE = re.compile(r"\[(\d{1,3})\]")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？!?])|\n")
_ABSTRACT_CHARS = 700
_MAX_SENTENCES_PER_SECTION = 40
_MAX_TOKENS = 5000
_TIMEOUT = 120.0

_SYSTEM = """你是学术引用支持性裁判。任务:判断每条正文论断是否被其所引文献的真实内容支持。

对每条待判定论断输出:
- index: 论断序号(与输入一致)
- verdict: 2(完全支持)/ 1(部分支持,论断略强于证据)/ 0(不支持)
- reason: 一句话中文理由(仅 verdict≤1 时需要)

判定标准:
- 论断内容(结论、方向、对象)必须能在所引文献的标题/摘要/证据中找到依据;
- 文献只研究了 X 对 Y 的影响,论断却说 X 导致 Z → 0;
- 文献结论是相关关系,论断写成因果决定 → 0;
- 文献结论方向相反或对象不同 → 0;
- 论断是文献结论的合理概括但表述略强 → 1;
- 摘要信息不足以判定时给 1 并说明证据不足,不要猜 0。

严格输出 JSON 对象(不要 markdown 代码块):
{"judgements": [{"index": 1, "verdict": 2, "reason": "..."}]}
必须覆盖全部待判定论断。"""


def support_judge_enabled() -> bool:
    return os.environ.get(_ENV_TOGGLE) == "1"


def _cited_ids_in_order(sections) -> dict[int, str]:
    cited: list[str] = []
    for section in sections:
        for cid in section.citations or []:
            if cid and cid not in cited:
                cited.append(cid)
    return {i + 1: cid for i, cid in enumerate(cited)}


def _section_items(section, number_to_lit: dict[int, str]) -> list[dict]:
    """抽取一章中带引用的论断: {index, sentence, lit_ids}。"""
    text = section.content or ""
    items: list[dict] = []
    start = 0
    for m in _SENTENCE_SPLIT_RE.finditer(text):
        end = m.end()
        sentence = text[start:end].strip()
        start = end
        if not sentence:
            continue
        numbers = [int(n) for n in _NUMERIC_ANCHOR_RE.findall(sentence)]
        lit_ids = list(dict.fromkeys(
            number_to_lit[n] for n in numbers if n in number_to_lit
        ))
        if lit_ids:
            items.append({
                "index": len(items),
                "sentence": sentence[:400],
                "lit_ids": lit_ids,
            })
        if len(items) >= _MAX_SENTENCES_PER_SECTION:
            break
    return items


def _papers_payload(lit_ids: list[str], papers: list[Paper], evidence_map: dict | None) -> list[dict]:
    by_id = {p.lit_id: p for p in papers}
    out: list[dict] = []
    seen: set[str] = set()
    for lit_id in lit_ids:
        if lit_id in seen:
            continue
        seen.add(lit_id)
        paper = by_id.get(lit_id)
        if paper is None:
            continue
        entry = {
            "lit_id": lit_id,
            "title": paper.title,
            "abstract": (paper.abstract or "")[:_ABSTRACT_CHARS],
        }
        ev = (evidence_map or {}).get(lit_id) or {}
        if ev.get("extracted"):
            if ev.get("findings"):
                entry["findings"] = ev["findings"][:3]
            if ev.get("polarity") and ev["polarity"] != "unclear":
                entry["polarity"] = ev["polarity"]
        out.append(entry)
    return out


def _parse_judgements(raw: str, expected: int) -> list[dict]:
    cleaned = re.sub(r"```(?:json)?|```", "", raw or "").strip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError(f"支持性裁判结果不是合法 JSON: {exc}") from exc
    items = data.get("judgements") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ValueError("支持性裁判结果缺少 judgements 数组")
    out: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            index = int(item.get("index"))
        except (TypeError, ValueError):
            continue
        verdict = item.get("verdict")
        if isinstance(verdict, bool) or not isinstance(verdict, (int, float)):
            continue
        verdict = int(verdict)
        if verdict not in (0, 1, 2):
            continue
        out.append({
            "index": index,
            "verdict": verdict,
            "reason": str(item.get("reason", ""))[:200],
        })
    return out


def check_citation_support(
    sections: list["SectionResult"],
    papers: list[Paper],
    *,
    evidence_map: dict | None = None,
) -> QACheckResult:
    """支持性裁判入口。判 0 的论断产 WARN;裁判不可用/失败由 runner 记 SKIPPED。"""
    result = QACheckResult(
        check_id="support_001",
        name="引用支持性裁判",
        status=QACheckStatus.PASS,
        metrics={"sections": len(sections)},
    )
    number_to_lit = _cited_ids_in_order(sections)
    total = 0
    judged = 0
    unsupported: list[QAIssue] = []
    partial = 0
    from llm.client import messages_create

    for section in sections:
        if section.key == "comment":
            continue
        items = _section_items(section, number_to_lit)
        if not items:
            continue
        lit_ids = sorted({lid for item in items for lid in item["lit_ids"]})
        payload = {
            "claims": items,
            "papers": _papers_payload(lit_ids, papers, evidence_map),
        }
        raw = messages_create(
            _SYSTEM,
            json.dumps(payload, ensure_ascii=False),
            max_tokens=_MAX_TOKENS,
            max_retries=0,
            timeout=_TIMEOUT,
            response_format={"type": "json_object"},
        )
        judgements = _parse_judgements(raw, len(items))
        by_index = {j["index"]: j for j in judgements}
        for item in items:
            total += 1
            judgement = by_index.get(item["index"])
            if judgement is None:
                continue
            judged += 1
            if judgement["verdict"] == 0:
                unsupported.append(QAIssue(
                    "SUPPORT_UNVERIFIED", QACheckStatus.WARN,
                    "content", f"section:{section.key}",
                    "所引文献不支持该论断" + (
                        f": {judgement['reason']}" if judgement["reason"] else ""
                    ),
                    section_key=section.key,
                    lit_id=item["lit_ids"][0],
                    snippet=item["sentence"][:160],
                ))
            elif judgement["verdict"] == 1:
                partial += 1

    result.issues = unsupported
    result.metrics.update({
        "cited_claims": total,
        "judged_claims": judged,
        "judge_coverage": round(judged / total, 4) if total else 1.0,
        "unsupported_claims": len(unsupported),
        "partial_support_claims": partial,
    })
    result.status = QACheckStatus.WARN if unsupported else QACheckStatus.PASS
    result.notes.append(
        f"共判定 {total} 条带引用论断;"
        + (
            f"不支持 {len(unsupported)} 条、部分支持 {partial} 条"
            if (unsupported or partial)
            else "全部论断获得所引文献支持"
        )
    )
    return result


__all__ = ["support_judge_enabled", "check_citation_support"]
