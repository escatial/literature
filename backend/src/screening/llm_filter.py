"""综述写作前的主题与文献质量筛选。"""
from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Generator

from llm.client import messages_create
from retrieval.types import Paper

log = logging.getLogger(__name__)


class ScreeningError(RuntimeError):
    """筛选无法得到可信结果时终止后续写作。"""


SYSTEM = """你是学术论文写作前的文献筛选助手。
任务:对每篇候选论文同时判断主题相关性和摘要是否足以支撑后续综述写作。

判定规则:
- relevant=true:论文与用户研究主题在研究对象、问题、方法、理论或应用上有实质关联。
  语义同义即算相关,不要因用字差异误杀:用户主题用词可能不是学术规范表述
  (如「权利寻租」的规范用语是「权力寻租」、「村官」即「村干部」),
  文献标题/摘要中出现规范表述或常见变体时,应视为同一概念。
- relevant=false:完全无关、仅有表面关键词重合、或研究对象差异过大。
- abstract_ok=true:摘要不是空白、占位符、明显截断文本,并且至少能识别研究对象/问题和主要发现、方法或结论中的核心信息。
- abstract_ok=false:摘要缺失、只有极短描述、明显被截断,或信息不足以支撑学术综述。
- 不要根据常识补全摘要中没有的信息。

同时给出三个 0-100 的整数相关性维度分(用于后续分层筛选):
- field_match 研究领域匹配度:100 表示研究领域与用户主题完全贴合(同一研究对象与问题域);
  60-89 表示邻近领域;0-59 表示领域明显偏离。
- method_applicability 研究方法适用性:100 表示其研究方法可被本主题直接参考或借鉴;
  70-99 表示可部分借鉴;0-69 表示方法不适用或摘要未交代方法。
- conclusion_value 结论参考价值:100 表示结论可直接作为本主题的核心论据;
  分数越低表示结论越只能作为背景信息。
维度分不确定时给保守分,不要凭空拔高;但这不影响 relevant 判定——
语义相关就如实给 relevant=true。

严格输出 JSON 对象(不要其他文字,不要 markdown 代码块):
{"results": [
  {"lit_id": "<原文 lit_id>", "relevant": true|false, "abstract_ok": true|false,
   "field_match": 0-100, "method_applicability": 0-100, "conclusion_value": 0-100,
   "reason": "<一句话中文原因>"}
]}
必须为输入中的每一个 lit_id 输出且只能输出一次。"""

# LLM 相关性维度字段。缺失时由 writing.relevance 用保守默认值兜底,
# 不在筛选层伪造分数。
_DIMENSION_FIELDS = ("field_match", "method_applicability", "conclusion_value")


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

_SCREENING_BATCH_SIZE = 24
_SCREENING_ABSTRACT_CHARS = 2000
_SCREENING_TIMEOUT_SECONDS = 90.0
_SCREENING_MAX_RETRIES = 1


def _screen_chunk(
    papers: list[Paper],
    topic: str,
    abstract_chars: int,
) -> list[dict]:
    """筛选一个有界批次,避免单次 JSON 输出被模型截断。"""
    payload = [
        {
            "lit_id": p.lit_id,
            "title": p.title,
            "abstract": (p.abstract or "")[:abstract_chars],
        }
        for p in papers
    ]
    user = (
        f"研究主题:{topic}\n"
        f"候选论文:\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n"
        "请输出严格 JSON 对象,格式为 {\"results\":[...]},覆盖全部候选论文。"
        "每个 reason 最多 80 个字。"
    )
    raw = messages_create(
        SYSTEM,
        user,
        # 每条多输出 3 个维度分,预算相应上调,避免 JSON 被截断触发拆半重试
        max_tokens=6500,
        max_retries=_SCREENING_MAX_RETRIES,
        timeout=_SCREENING_TIMEOUT_SECONDS,
        response_format={"type": "json_object"},
    )
    return _parse_decisions(raw)


def _screen_chunk_with_fallback(
    papers: list[Paper],
    topic: str,
    abstract_chars: int,
) -> list[dict]:
    """解析或覆盖不完整时缩小请求范围,绝不默认补齐结果。"""
    try:
        llm_decisions = _screen_chunk(papers, topic, abstract_chars)
        return _normalize_chunk_decisions(llm_decisions, papers)
    except ScreeningError as exc:
        retryable = (
            "不是合法 JSON" in str(exc)
            or "未覆盖 lit_id" in str(exc)
        )
        # 模型可能返回合法但不完整的 JSON,与截断 JSON 一样拆半重试。
        # 非法 lit_id 或字段类型错误不靠拆分掩盖,直接终止;
        # 重复 lit_id 已在 _normalize_chunk_decisions 内去重容忍,不会到这里。
        if not retryable or len(papers) <= 1:
            raise
        midpoint = len(papers) // 2
        return (
            _screen_chunk_with_fallback(papers[:midpoint], topic, abstract_chars)
            + _screen_chunk_with_fallback(papers[midpoint:], topic, abstract_chars)
        )


def _screening_batches(papers: list[Paper]) -> list[list[Paper]]:
    """按固定上限分批,让输入和输出规模都可预测。"""
    return [
        papers[start : start + _SCREENING_BATCH_SIZE]
        for start in range(0, len(papers), _SCREENING_BATCH_SIZE)
    ]


def _normalize_chunk_decisions(
    llm_decisions: list[dict],
    batch: list[Paper],
) -> list[dict]:
    """校验单批筛选结果,确保每个输入 lit_id 恰好出现一次。

    设计原则:
      - 上游已经按 lit_id 去重,故 batch 内必无重复 paper 实例。
      - LLM 必须为 batch 中每个 lit_id 返回决策,字段类型必须正确。
      - 缺失 / 未知 lit_id / 字段类型错误 raise ScreeningError,
        由上游 _screen_chunk_with_fallback 拆分重试或终止流程。
      - LLM 同批重复输出同一 lit_id 是常见抖动:保留第一条即可,不终止。
    """
    expected_ids = {p.lit_id for p in batch}
    seen_ids: set[str] = set()
    normalized: list[dict] = []

    for decision in llm_decisions:
        if not isinstance(decision, dict):
            raise ScreeningError("筛选结果包含非对象项")
        lit_id = str(decision.get("lit_id", ""))
        if not lit_id:
            raise ScreeningError("筛选结果 lit_id 为空")
        if lit_id not in expected_ids:
            # batch 已去重,LLM 不应输出 batch 之外的 lit_id
            raise ScreeningError(
                f"筛选结果包含未知 lit_id: {lit_id}(不在本批中)"
            )
        if lit_id in seen_ids:
            # 重复输出属于模型抖动,保留第一条决策,避免整批终止
            log.warning("筛选结果重复 lit_id: %s(保留第一条决策)", lit_id)
            continue
        if not isinstance(decision.get("relevant"), bool):
            raise ScreeningError(f"筛选结果 relevant 非布尔值: {lit_id}")
        if not isinstance(decision.get("abstract_ok"), bool):
            raise ScreeningError(f"筛选结果 abstract_ok 非布尔值: {lit_id}")
        seen_ids.add(lit_id)
        item = {
            "lit_id": lit_id,
            "relevant": decision["relevant"],
            "abstract_ok": decision["abstract_ok"],
            "reason": str(decision.get("reason", ""))[:200],
        }
        # 维度分只在 LLM 给出合法数值时透传,缺失交由下游用保守默认值兜底
        for key in _DIMENSION_FIELDS:
            value = decision.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            item[key] = round(min(100.0, max(0.0, float(value))), 2)
        normalized.append(item)

    missing_ids = expected_ids - seen_ids
    if missing_ids:
        raise ScreeningError(
            "筛选结果未覆盖 lit_id: " + ", ".join(sorted(missing_ids))
        )
    return normalized


def _screen_batch_events(
    eligible: list[Paper],
    topic: str,
    abstract_chars: int,
) -> Generator[dict, None, None]:
    """并行执行有界筛选批次并产生进度事件。"""
    batches = _screening_batches(eligible)
    total_batches = len(batches)
    if not batches:
        return

    max_workers = min(4, total_batches)
    for batch_index, batch in enumerate(batches[:max_workers], start=1):
        yield {
            "status": "started",
            "batch": batch_index,
            "total_batches": total_batches,
            "processed": 0,
            "total": len(eligible),
            "batch_size": len(batch),
            "parallel_workers": max_workers,
        }

    completed_count = 0
    with ThreadPoolExecutor(
        max_workers=max_workers,
        thread_name_prefix="screening",
    ) as executor:
        futures = {
            executor.submit(
                _screen_chunk_with_fallback,
                batch,
                topic,
                abstract_chars,
            ): (batch_index, batch)
            for batch_index, batch in enumerate(batches, start=1)
        }
        try:
            for future in as_completed(futures):
                batch_index, batch = futures[future]
                normalized = future.result()
                completed_count += len(batch)
                yield {
                    "status": "completed",
                    "batch": batch_index,
                    "total_batches": total_batches,
                    "processed": completed_count,
                    "total": len(eligible),
                    "batch_size": len(batch),
                    "results": normalized,
                }
        except Exception as exc:
            for future in futures:
                future.cancel()
            log.exception("文献筛选批次失败")
            if isinstance(exc, ScreeningError):
                raise
            raise ScreeningError(f"文献筛选调用失败: {exc}") from exc


def screen_batch_stream(
    papers: list[Paper],
    topic: str,
    max_chars: int = _SCREENING_ABSTRACT_CHARS,
) -> Generator[dict, None, None]:
    """分批筛选并发出 started/completed/finished 事件。

    入口去重:paper 与 lit_id 是一对一关系,任何重复都按"前一条更完整"语义保留一条,
    直接丢弃多余副本。一篇 paper 不可能在 screening 阶段被评估两次。
    """
    if not papers:
        yield {"status": "finished", "results": []}
        return

    deduped: list[Paper] = []
    seen_ids: set[str] = set()
    for paper in papers:
        if paper.lit_id in seen_ids:
            continue
        seen_ids.add(paper.lit_id)
        deduped.append(paper)

    decisions: list[dict] = []
    eligible: list[Paper] = []
    for paper in deduped:
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
        for event in _screen_batch_events(
            eligible,
            topic,
            max_chars if max_chars > 0 else _SCREENING_ABSTRACT_CHARS,
        ):
            if event["status"] == "completed":
                decisions.extend(event["results"])
            yield event

    expected_ids = {p.lit_id for p in eligible}
    result_ids = {decision["lit_id"] for decision in decisions}
    if len(result_ids) != len(decisions):
        raise ScreeningError("筛选结果包含重复 lit_id")
    if result_ids & expected_ids != expected_ids:
        raise ScreeningError("筛选结果未覆盖全部可筛选文献")
    yield {"status": "finished", "results": decisions}


def screen_batch(
    papers: list[Paper],
    topic: str,
    max_chars: int = _SCREENING_ABSTRACT_CHARS,
) -> list[dict]:
    """先剔除硬性缺失文献,再由 LLM 分批判断主题和摘要质量。

    筛选失败直接抛错,绝不把未判定文献默认为合格并继续写作。
    """
    decisions: list[dict] | None = None
    for event in screen_batch_stream(papers, topic, max_chars):
        if event["status"] == "finished":
            decisions = event["results"]
    if decisions is None:
        raise ScreeningError("筛选未产生完整结果")

    ordered = {decision["lit_id"]: decision for decision in decisions}
    return [ordered[paper.lit_id] for paper in papers]
