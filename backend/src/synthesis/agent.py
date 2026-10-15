"""主题综合 Agent:共识 / 争议 / 方法差异 / 研究空白(方案 M3)。

每主题一次 LLM 调用,输入为该主题文献的题录+摘要+结构化证据(evidence 模块
产出),输出严格 JSON。文献以行首数字编号指代(与 classifier 同一套防截断
策略),返回后回填真实 lit_id 并逐条校验归属。
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from writing.classifier import Group
from retrieval.types import Paper

log = logging.getLogger(__name__)

# 环境开关:默认启用;单测通过 conftest 统一置 0,专项测试自行 monkeypatch。
_ENV_TOGGLE = "WRITING_SYNTHESIS"

# 单主题条目上限(防止 LLM 灌水拖垮写作 prompt)
_MAX_CONSENSUS = 6
_MAX_CONTROVERSIES = 4
_MAX_METHOD_DIFF = 4
_MAX_GAPS = 5
_ABSTRACT_CHARS = 600


class SynthesisError(RuntimeError):
    """主题综合失败(调用方按主题降级)。"""


def synthesis_enabled() -> bool:
    raw = os.environ.get(_ENV_TOGGLE)
    return raw != "0"


def _paper_lines(papers: list[Paper]) -> tuple[str, dict[int, str]]:
    """渲染数字编号文献清单,返回 (清单文本, 编号->lit_id 映射)。"""
    lines: list[str] = []
    number_map: dict[int, str] = {}
    for idx, paper in enumerate(papers, start=1):
        number_map[idx] = paper.lit_id
        abstract = (paper.abstract or "").strip()
        if len(abstract) > _ABSTRACT_CHARS:
            abstract = abstract[:_ABSTRACT_CHARS] + "…"
        authors = ", ".join((paper.authors or [])[:3])
        lines.append(
            f"#{idx} | {paper.title} | {authors} | {paper.year or 'N/A'}\n"
            f"  摘要: {abstract or '(无摘要)'}"
        )
    return "\n".join(lines), number_map


def _evidence_lines(papers: list[Paper], evidence_map: dict[str, dict] | None) -> str:
    if not evidence_map:
        return "(无结构化证据)"
    blocks: list[str] = []
    for idx, paper in enumerate(papers, start=1):
        ev = evidence_map.get(paper.lit_id) or {}
        if not ev.get("extracted"):
            continue
        parts = [f"#{idx}"]
        if ev.get("method_design"):
            parts.append(f"设计={ev['method_design']}")
        if ev.get("sample"):
            parts.append(f"样本={ev['sample']}")
        if ev.get("polarity") and ev["polarity"] != "unclear":
            parts.append(f"结论极性={ev['polarity']}")
        if ev.get("key_variables"):
            parts.append("变量=" + "、".join(ev["key_variables"][:4]))
        blocks.append(" | ".join(parts))
    return "\n".join(blocks) if blocks else "(无结构化证据)"


def _rq_block(research_questions: list[str] | None) -> str:
    if not research_questions:
        return ""
    lines = "\n".join(f"- {rq}" for rq in research_questions if str(rq).strip())
    return f"\n用户的研究问题(综合时须服务于回答这些问题):\n{lines}\n" if lines else ""


def _valid_ids(value: object, number_map: dict[int, str]) -> list[str]:
    """把模型输出的数字编号(或 lit_id 原文)回填为合法 lit_id 列表。"""
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, bool):
            continue
        if isinstance(item, int):
            lit_id = number_map.get(item)
            if lit_id and lit_id not in out:
                out.append(lit_id)
            continue
        text = str(item or "").strip()
        if not text:
            continue
        if text.isdigit():
            lit_id = number_map.get(int(text))
            if lit_id and lit_id not in out:
                out.append(lit_id)
            continue
        # 模型直接抄了 lit_id 原文:只要是池内合法值就接受
        if text in number_map.values() and text not in out:
            out.append(text)
    return out


def _clean_str(value: object, limit: int = 300) -> str:
    return str(value or "").strip()[:limit]


def _sanitize_synthesis(
    data: dict, number_map: dict[int, str], theme_size: int
) -> dict:
    """校验并清洗综合结果:条目必须绑定池内真实文献,不合法即丢弃。"""
    consensus: list[dict] = []
    for item in (data.get("consensus") or [])[:_MAX_CONSENSUS * 2]:
        if not isinstance(item, dict):
            continue
        claim = _clean_str(item.get("claim"), 400)
        lit_ids = _valid_ids(item.get("lit_ids"), number_map)
        if claim and lit_ids:
            consensus.append({"claim": claim, "lit_ids": lit_ids})
        if len(consensus) >= _MAX_CONSENSUS:
            break

    controversies: list[dict] = []
    for item in (data.get("controversies") or [])[:_MAX_CONTROVERSIES * 2]:
        if not isinstance(item, dict):
            continue
        claim = _clean_str(item.get("claim"), 400)
        side_a_ids = _valid_ids((item.get("side_a") or {}).get("lit_ids"), number_map)
        side_b_ids = _valid_ids((item.get("side_b") or {}).get("lit_ids"), number_map)
        side_a_pos = _clean_str((item.get("side_a") or {}).get("position"), 200)
        side_b_pos = _clean_str((item.get("side_b") or {}).get("position"), 200)
        causes = [
            _clean_str(c, 120)
            for c in (item.get("possible_causes") or [])
            if _clean_str(c, 120)
        ][:_MAX_CONTROVERSIES]
        # 双方都必须有真实文献,且合计 ≥2 篇,否则视为伪争议丢弃
        if claim and side_a_ids and side_b_ids and len(set(side_a_ids + side_b_ids)) >= 2:
            controversies.append({
                "claim": claim,
                "side_a": {"lit_ids": side_a_ids, "position": side_a_pos},
                "side_b": {"lit_ids": side_b_ids, "position": side_b_pos},
                "possible_causes": [
                    {"cause": c, "type": "hypothesis"} for c in causes
                ],
            })
        if len(controversies) >= _MAX_CONTROVERSIES:
            break

    method_differences = [
        _clean_str(m, 300)
        for m in (data.get("method_differences") or [])
        if _clean_str(m, 300)
    ][:_MAX_METHOD_DIFF]

    gaps: list[dict] = []
    for item in (data.get("gaps") or [])[:_MAX_GAPS * 2]:
        if not isinstance(item, dict):
            continue
        gap = _clean_str(item.get("gap"), 300)
        evidence = _clean_str(item.get("evidence"), 300)
        kind = str(item.get("kind") or "").strip().lower()
        if kind not in {"confirmed", "not_retrieved"}:
            kind = "not_retrieved"
        if gap:
            gaps.append({"gap": gap, "evidence": evidence, "kind": kind})
        if len(gaps) >= _MAX_GAPS:
            break

    return {
        "consensus": consensus,
        "controversies": controversies,
        "method_differences": method_differences,
        "gaps": gaps,
    }


def _parse_synthesis_json(raw: str) -> dict:
    cleaned = re.sub(r"```(?:json)?|```", "", raw or "").strip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise SynthesisError(f"主题综合结果不是合法 JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise SynthesisError("主题综合结果必须是 JSON 对象")
    return data


def build_theme_synthesis(
    theme_name: str,
    theme_question: str | None,
    papers: list[Paper],
    evidence_map: dict[str, dict] | None,
    topic: str,
    research_questions: list[str] | None = None,
) -> dict:
    """单个主题的综合推理。失败抛 SynthesisError,由调用方按主题降级。"""
    if len(papers) < 2:
        raise SynthesisError(f"主题「{theme_name}」文献不足 2 篇,跳过综合")

    catalog, number_map = _paper_lines(papers)
    rq_block = _rq_block(research_questions)
    system = _render_prompt(
        theme_name, theme_question, topic, catalog,
        _evidence_lines(papers, evidence_map), rq_block or "(未提供研究问题)",
    )
    from llm.client import messages_create
    raw = messages_create(
        system,
        f"研究主题:{topic}\n主题:{theme_name}\n"
        "请输出严格 JSON 对象(不要 markdown 代码块,不要解释)。",
        max_tokens=5000,
        max_retries=0,
        timeout=120.0,
        response_format={"type": "json_object"},
    )
    data = _parse_synthesis_json(raw)
    sanitized = _sanitize_synthesis(data, number_map, len(papers))
    if not (sanitized["consensus"] or sanitized["controversies"]):
        raise SynthesisError(f"主题「{theme_name}」综合结果无有效条目")
    return sanitized


def _render_prompt(
    theme_name: str,
    theme_question: str | None,
    topic: str,
    catalog: str,
    evidence_block: str,
    rq_block: str,
) -> str:
    from prompts.service import render
    return render(
        "literature-review:synthesis",
        topic=topic,
        theme_name=theme_name,
        theme_question=theme_question or "(未提供)",
        papers_catalog=catalog,
        evidence_block=evidence_block,
        rq_block=rq_block,
    )


def build_all_syntheses(
    groups: list[Group],
    papers: list[Paper],
    evidence_map: dict[str, dict] | None,
    topic: str,
    research_questions: list[str] | None = None,
    *,
    progress: Any = None,
) -> tuple[dict[str, dict], dict]:
    """全部主题的综合推理入口。

    返回 (theme_key -> synthesis dict, meta)。
    - theme_key 与章节 key 一致(theme_1 / theme_2 …),写作时按 spec.key 取用;
    - 单主题失败跳过并记入 meta.failures,不阻塞整体;
    - meta 含 succeeded / failures / counts,供 SSE 事件与前端展示。
    """
    by_id = {p.lit_id for p in papers}
    syntheses: dict[str, dict] = {}
    failures: list[dict] = []
    counts = {"consensus": 0, "controversies": 0, "gaps": 0}

    for idx, group in enumerate(groups, start=1):
        theme_papers = [p for p in papers if p.lit_id in set(group.lit_ids) & by_id]
        try:
            result = build_theme_synthesis(
                group.name, group.research_question, theme_papers,
                evidence_map, topic, research_questions,
            )
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, SynthesisError):
                log.info("主题综合跳过: %s", exc)
            else:
                log.warning("主题综合失败(%s): %s", group.name, exc)
            failures.append({"theme_index": idx, "theme": group.name, "reason": str(exc)})
            continue
        syntheses[f"theme_{idx}"] = result
        counts["consensus"] += len(result["consensus"])
        counts["controversies"] += len(result["controversies"])
        counts["gaps"] += len(result["gaps"])
        if progress:
            progress({
                "kind": "theme_synthesis_done",
                "theme_index": idx,
                "theme": group.name,
                "consensus": len(result["consensus"]),
                "controversies": len(result["controversies"]),
                "gaps": len(result["gaps"]),
            })

    meta = {
        "succeeded": len(syntheses),
        "failed": len(failures),
        "failures": failures,
        "counts": counts,
    }
    return syntheses, meta


def comparison_table(syntheses: dict[str, dict], groups: list[Group]) -> list[dict]:
    """《研究结论比较表》:主题 / 支持研究 / 相反研究 / 可能原因。

    供交付物渲染;争议原因是假设(synthesis 层已标注 type=hypothesis)。
    """
    rows: list[dict] = []
    for idx, group in enumerate(groups, start=1):
        result = syntheses.get(f"theme_{idx}")
        if not result:
            continue
        for controversy in result["controversies"]:
            rows.append({
                "theme": group.name,
                "claim": controversy["claim"],
                "side_a": controversy["side_a"],
                "side_b": controversy["side_b"],
                "possible_causes": controversy["possible_causes"],
            })
    return rows


__all__ = [
    "SynthesisError",
    "synthesis_enabled",
    "build_theme_synthesis",
    "build_all_syntheses",
    "comparison_table",
]
