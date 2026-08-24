"""综述写作前的主题与文献质量筛选。"""
from __future__ import annotations

import json
import logging
import re

from llm.client import messages_create
from retrieval.types import Paper

log = logging.getLogger(__name__)


class ScreeningError(RuntimeError):
    """筛选无法得到可信结果时终止后续写作。"""


SYSTEM = """你是学术论文写作前的文献筛选助手。
任务:对每篇候选论文同时判断主题相关性和摘要是否足以支撑后续综述写作。

判定规则:
- relevant=true:论文与用户研究主题在研究对象、问题、方法、理论或应用上有实质关联。
- relevant=false:完全无关、仅有表面关键词重合、或研究对象差异过大。
- abstract_ok=true:摘要不是空白、占位符、明显截断文本,并且至少能识别研究对象/问题和主要发现、方法或结论中的核心信息。
- abstract_ok=false:摘要缺失、只有极短描述、明显被截断,或信息不足以支撑学术综述。
- 不要根据常识补全摘要中没有的信息。

严格输出 JSON 数组(不要其他文字,不要 markdown 代码块):
[
  {"lit_id": "<原文 lit_id>", "relevant": true|false, "abstract_ok": true|false, "reason": "<一句话中文原因>"}
]
必须为输入中的每一个 lit_id 输出且只能输出一次。"""


def _quality_rejection(paper: Paper) -> str | None:
    """先做无需 LLM 的硬性完整性检查。"""
    if not paper.title.strip():
        return "标题缺失"
    abstract = (paper.abstract or "").strip()
    if not abstract:
        return "摘要缺失"
    return None


def _parse_decisions(raw: str) -> list[dict]:
    """解析筛选结构,兼容 JSON object 模式下的明确数组包装。"""
    cleaned = re.sub(r"```(?:json)?|```", "", raw).strip()
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ScreeningError(f"筛选结果不是合法 JSON: {exc}") from exc

    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        for key in ("results", "decisions", "items"):
            value = parsed.get(key)
            if isinstance(value, list):
                return value
    raise ScreeningError("筛选结果必须是 JSON 数组或包含数组的 JSON 对象")


def screen_batch(papers: list[Paper], topic: str, max_chars: int = 4000) -> list[dict]:
    """先剔除硬性缺失文献,再由 LLM 判断主题和摘要质量。

    筛选失败直接抛错,绝不把未判定文献默认为合格并继续写作。
    """
    if not papers:
        return []

    decisions: list[dict] = []
    eligible: list[Paper] = []
    for paper in papers:
        reason = _quality_rejection(paper)
        if reason:
            decisions.append({
                "lit_id": paper.lit_id,
                "relevant": False,
                "abstract_ok": False,
                "reason": reason,
            })
        else:
            eligible.append(paper)

    if eligible:
        payload = [
            {
                "lit_id": p.lit_id,
                "title": p.title,
                "abstract": (p.abstract or "")[:max_chars],
            }
            for p in eligible
        ]
        user = (
            f"研究主题:{topic}\n"
            f"候选论文:\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n"
            "请输出严格 JSON 数组,覆盖全部候选论文。"
        )
        try:
            raw = messages_create(
                SYSTEM,
                user,
                max_tokens=6000,
                response_format={"type": "json_object"},
            )
            llm_decisions = _parse_decisions(raw)
        except Exception as exc:
            log.exception("文献筛选失败")
            if isinstance(exc, ScreeningError):
                raise
            raise ScreeningError(f"文献筛选调用失败: {exc}") from exc

        expected_ids = {p.lit_id for p in eligible}
        seen_ids: set[str] = set()
        for decision in llm_decisions:
            if not isinstance(decision, dict):
                raise ScreeningError("筛选结果包含非对象项")
            lit_id = str(decision.get("lit_id", ""))
            if lit_id not in expected_ids or lit_id in seen_ids:
                raise ScreeningError(f"筛选结果包含未知或重复 lit_id: {lit_id or '<empty>'}")
            if not isinstance(decision.get("relevant"), bool):
                raise ScreeningError(f"筛选结果 relevant 非布尔值: {lit_id}")
            if not isinstance(decision.get("abstract_ok"), bool):
                raise ScreeningError(f"筛选结果 abstract_ok 非布尔值: {lit_id}")
            seen_ids.add(lit_id)
            decisions.append({
                "lit_id": lit_id,
                "relevant": decision["relevant"],
                "abstract_ok": decision["abstract_ok"],
                "reason": str(decision.get("reason", ""))[:200],
            })

        missing_ids = expected_ids - seen_ids
        if missing_ids:
            raise ScreeningError(
                "筛选结果未覆盖 lit_id: " + ", ".join(sorted(missing_ids))
            )

    ordered = {decision["lit_id"]: decision for decision in decisions}
    return [ordered[paper.lit_id] for paper in papers]