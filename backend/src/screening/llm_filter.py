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
- relevant=true:论文与用户研究主题的研究对象或核心问题相同、属于直接上下位概念，
  或者摘要明确说明其结论可直接迁移到该研究对象。
  语义同义即算相关,不要因用字差异误杀:用户主题用词可能不是学术规范表述
  (如「权利寻租」的规范用语是「权力寻租」、「村官」即「村干部」),
  文献标题/摘要中出现规范表述或常见变体时,应视为同一概念。
- relevant=false:完全无关、仅有表面关键词重合、研究对象差异过大，或者仅仅使用了
  相同算法/模型/研究方法但没有研究同一对象或问题。方法相似只能影响
  method_applicability 分数，不能单独使 relevant=true。
- abstract_ok=true:摘要不是空白、占位符、明显截断文本,并且至少能识别研究对象/问题和主要发现、方法或结论中的核心信息。
- abstract_ok=false:摘要缺失、只有极短描述、明显被截断,或信息不足以支撑学术综述。
- 不要根据常识补全摘要中没有的信息。

同时给出四个 0-100 的整数评分维度(前三个用于后续分层筛选,method_rigor 用于引用审计):
- field_match 研究领域匹配度:100 表示研究领域与用户主题完全贴合(同一研究对象与问题域);
  60-89 表示邻近领域;0-59 表示领域明显偏离。
- method_applicability 研究方法适用性:100 表示其研究方法可被本主题直接参考或借鉴;
  70-99 表示可部分借鉴;0-69 表示方法不适用或摘要未交代方法。
- conclusion_value 结论参考价值:100 表示结论可直接作为本主题的核心论据;
  分数越低表示结论越只能作为背景信息。
- method_rigor 方法严谨性:评价该研究自身的证据质量——设计能否支撑其结论
  (实验/纵向 > 横截面问卷;样本规模与代表性;是否报告效应量/稳健性;
  测量工具是否可靠)。100 表示设计严谨、结论可信;0-59 表示设计有明显缺陷
  或摘要完全未交代方法。该分数只影响引用审计(低严谨性文献不得单独支撑
  关键判断),不影响 relevant 判定。
维度分不确定时给保守分,不要凭空拔高;但这不影响 relevant 判定——
语义相关就如实给 relevant=true。

严格输出 JSON 对象(不要其他文字,不要 markdown 代码块):
{"results": [
  {"no": <输入中的编号数字>, "relevant": true|false, "abstract_ok": true|false,
   "field_match": 0-100, "method_applicability": 0-100, "conclusion_value": 0-100,
   "method_rigor": 0-100, "reason": "<一句话中文原因>"}
]}
每条的 no 必须逐字填输入里的编号数字;严禁编造不存在的编号,严禁遗漏。
必须为输入中的每一个编号输出且只能输出一次。"""

# LLM 相关性维度字段。缺失时由 writing.relevance 用保守默认值兜底,
# 不在筛选层伪造分数。method_rigor 仅透传给相关性报告与审计层,
# 不参与相关性分级阈值(v1 保持既有分级行为不变)。
_DIMENSION_FIELDS = ("field_match", "method_applicability", "conclusion_value", "method_rigor")


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
    if not cleaned:
        # MiniMax M3 偶发 200 OK 但正文为空(思考内容吃掉输出预算):
        # 必须与"非 JSON"区分开,空正文走整批重采样,拆半没有意义。
        raise ScreeningError("筛选结果为空")
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

# 批大小与并发度(v9.10 提速):36 篇/批 × 4 并发,把 500 篇筛选从
# 11-15 分钟压到 4-6 分钟。并发可经 SCREENING_WORKERS 覆盖;单 provider
# 限流时由 transport 重试 + 多 provider 降级兜底(deepseek/gpt)。
_SCREENING_BATCH_SIZE = 36


def _env_workers() -> int:
    import os
    try:
        return max(2, min(8, int(os.environ.get("SCREENING_WORKERS", "4"))))
    except (TypeError, ValueError):
        return 4
_SCREENING_ABSTRACT_CHARS = 2000
_SCREENING_TIMEOUT_SECONDS = 75.0
_SCREENING_MAX_RETRIES = 2


def _screen_chunk(
    papers: list[Paper],
    topic: str,
    abstract_chars: int,
) -> list[dict]:
    """筛选一个有界批次,避免单次 JSON 输出被模型截断。

    输入载荷用批内数字编号(no),模型只回显编号,由本模块按位置回填
    真实 lit_id —— 25 位十六进制串的逐字抄写是 LLM 的高频错误源
    (线上 423 篇池实测:b775 抄成 b375),编号方案根治这一类抖动。
    """
    payload = [
        {
            "no": idx,
            "title": p.title,
            "abstract": (p.abstract or "")[:abstract_chars],
        }
        for idx, p in enumerate(papers, start=1)
    ]
    user = (
        f"研究主题:{topic}\n"
        f"候选论文(no 是每篇的唯一编号):\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n"
        "请输出严格 JSON 对象,格式为 {\"results\":[...]},覆盖全部候选论文。"
        "每条的 no 必须逐字填输入里的编号数字,严禁编造不存在的编号。"
        "每个 reason 最多 80 个字。"
    )
    raw = messages_create(
        SYSTEM,
        user,
        # 每条输出 4 个维度分 + reason,预算相应上调,避免 JSON 被截断触发拆半重试
        max_tokens=8000,
        max_retries=_SCREENING_MAX_RETRIES,
        timeout=_SCREENING_TIMEOUT_SECONDS,
        response_format={"type": "json_object"},
    )
    return _parse_decisions(raw)


def _screen_chunk_with_fallback(
    papers: list[Paper],
    topic: str,
    abstract_chars: int,
    _retried: bool = False,
) -> list[dict]:
    """解析或覆盖不完整时缩小请求范围,绝不默认补齐结果。"""
    try:
        llm_decisions = _screen_chunk(papers, topic, abstract_chars)
        return _normalize_chunk_decisions(llm_decisions, papers)
    except ScreeningError as exc:
        retryable = (
            "不是合法 JSON" in str(exc)
            or "筛选结果为空" in str(exc)
            or "未覆盖 lit_id" in str(exc)
        )
        # 模型可能返回合法但不完整的 JSON,与截断 JSON 一样拆半重试。
        # 非法 lit_id 或字段类型错误不靠拆分掩盖,直接终止;
        # 重复 lit_id 已在 _normalize_chunk_decisions 内去重容忍,不会到这里。
        if not retryable or len(papers) <= 1:
            raise
        if not _retried:
            # 空正文/整体退化的输出与批大小无关(MiniMax 偶发,新采样即愈),
            # 整批重采样一次比立刻拆半更有效:拆半会把请求数翻倍且修不了
            # 系统性抖动(线上 423 篇池实测:空正文 + 子批 lit_id 全空)。
            log.warning(
                "筛选批次输出异常(size=%d),整批重采样重试: %s",
                len(papers), str(exc)[:80],
            )
            return _screen_chunk_with_fallback(
                papers, topic, abstract_chars, _retried=True
            )
        midpoint = len(papers) // 2
        return (
            _screen_chunk_with_fallback(papers[:midpoint], topic, abstract_chars)
            + _screen_chunk_with_fallback(papers[midpoint:], topic, abstract_chars)
        )
    except Exception as exc:
        # 连接/超时/网关错误也按“当前批次失败”处理：先二分降低上下文和输出，
        # 不让一个批次的异常取消其他已完成批次。小批次仍失败才上抛。
        if len(papers) <= 6:
            raise ScreeningError(f"筛选批次调用失败(size={len(papers)}): {exc}") from exc
        midpoint = len(papers) // 2
        log.warning("筛选批次调用失败(size=%d)，自动二分重试: %s", len(papers), exc)
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
    """校验单批筛选结果:编号回填 lit_id,确保每个输入编号恰好出现一次。

    设计原则:
      - 上游已经按 lit_id 去重,故 batch 内必无重复 paper 实例。
      - 模型只回显批内数字编号(no),本函数按位置映射回真实 lit_id。
      - 缺失编号 / 非法 JSON raise ScreeningError,
        由上游 _screen_chunk_with_fallback 整批重采样或拆分重试。
      - 退化/抄错条目(无 no、非数字、批外编号)一律丢弃+告警,不终止;
        真正漏掉的编号由下方 missing 校验兜底。
    """
    number_map = {idx: p.lit_id for idx, p in enumerate(batch, start=1)}
    seen_nos: set[int] = set()
    normalized: list[dict] = []

    for decision in llm_decisions:
        if not isinstance(decision, dict):
            raise ScreeningError("筛选结果包含非对象项")
        raw_no = decision.get("no")
        if isinstance(raw_no, bool) or raw_no is None:
            # 退化条目(低预算输出会出现缺 no 的对象):丢弃+告警,
            # 漏掉的编号交给 missing 校验触发整批重采样/拆半重试。
            log.warning("筛选结果某条缺少编号 no,已丢弃该条决策")
            continue
        try:
            no = int(str(raw_no).strip())
        except (TypeError, ValueError):
            log.warning("筛选结果 no 非数字(%r),已丢弃该条决策", raw_no)
            continue
        if no not in number_map:
            # 编号越界/抄错:丢弃该条决策;若因此漏掉真实编号,missing 分支兜底
            log.warning("筛选结果包含未知编号 %s(不在本批中),已丢弃", no)
            continue
        if no in seen_nos:
            # 重复输出属于模型抖动,保留第一条决策,避免整批终止
            log.warning("筛选结果重复编号 %s(保留第一条决策)", no)
            continue
        if not isinstance(decision.get("relevant"), bool):
            raise ScreeningError(f"筛选结果 relevant 非布尔值: no={no}")
        if not isinstance(decision.get("abstract_ok"), bool):
            raise ScreeningError(f"筛选结果 abstract_ok 非布尔值: no={no}")
        seen_nos.add(no)
        item = {
            "lit_id": number_map[no],
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

    missing = [p.lit_id for idx, p in enumerate(batch, start=1) if idx not in seen_nos]
    if missing:
        raise ScreeningError(
            "筛选结果未覆盖 lit_id: " + ", ".join(sorted(missing))
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

    # 筛选是 LLM 重请求；并发 4 容易把单一 provider 的连接池和限额打满。
    # 降到 2，配合单批二分重试，整体吞吐略降但失败率显著降低。
    max_workers = min(_env_workers(), total_batches)
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
