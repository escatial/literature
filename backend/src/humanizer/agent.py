"""humanizer-zh 三轮润色 Agent(方案 §6 "学术润色循环")。

输入: 已冻结 cite_id 的草稿
输出: 三轮润色后的版本(每一轮对应一个 prompt 模板与差异校验)

设计:
- 每一轮 prompt 关注点不同(方案 §6):
  - humanize_1: 结构套话去除(去除「综上所述」「随着……的发展」等模板化开场)
  - humanize_2: 句式节奏调整(长短句交替,避免连续 3 句以上同结构)
  - humanize_3: 学术精确性收紧(精确术语、避免口语化、量化表述)
- 引用冻结:每轮 LLM 的 prompt 显式告知「[lit_xxx] 与 [N] 不可改变」,
  并对 LLM 输出做差异检查,若 cite_id / 编号变化则回滚到上一版。
- 失败兜底:任何一轮 LLM 异常或差异校验失败,直接返回上一版草稿,
  不阻断主流程。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Callable

log = logging.getLogger(__name__)

_CITE_TOKEN_RE = re.compile(r"\[(?:lit_[a-zA-Z0-9_]+|hash:[a-zA-Z0-9_]+|\d{1,3})\]")


HUMANIZE_SYSTEM_TEMPLATES: list[str] = [
    # Round 1: 结构套话去除
    (
        "你是中文学术论文润色助手。本轮任务:去除结构套话与模板化开场。\n"
        "硬约束:\n"
        "  1. 不得改变任何 [lit_xxx]、[hash:xxx]、[N] 等引用标记;\n"
        "  2. 不得删除或新增文献;\n"
        "  3. 不得修改数据、年份、卷期;\n"
        "  4. 仅去除『综上所述』『随着…的发展』『本文认为』等套话;\n"
        "  5. 输出纯正文,不要解释、不要 markdown 标记。"
    ),
    # Round 2: 句式节奏
    (
        "你是中文学术论文润色助手。本轮任务:调整句式节奏,使长短句交替,\n"
        "避免连续 3 句以上同结构(连续以『。』结尾的短句或连续长定语句)。\n"
        "硬约束(同上轮):引用标记、文献、数据、年份一律不可改。\n"
        "输出纯正文。"
    ),
    # Round 3: 学术精确性
    (
        "你是中文学术论文润色助手。本轮任务:收紧学术精确性。\n"
        "  - 用精确学术术语替换口语化表述(如『很多』改为『多项』,『很大』改为『显著』);\n"
        "  - 量化表述优先(给出数量级、百分比、年份区间);\n"
        "  - 不引入原文中没有的新事实。\n"
        "硬约束(同上轮):引用标记、文献、数据、年份一律不可改。\n"
        "输出纯正文。"
    ),
]


@dataclass
class HumanizeDiff:
    """一轮润色的差异报告。"""

    round_no: int
    before_chars: int
    after_chars: int
    citations_before: set[str]
    citations_after: set[str]
    citations_preserved: bool
    accepted: bool
    detail: str = ""


def _extract_citations(text: str) -> set[str]:
    return set(_CITE_TOKEN_RE.findall(text or ""))


def _strip_preamble(text: str) -> str:
    """剥掉 LLM 偶发的开场白(如『好的,以下是润色后的内容:』)与代码块包装。"""
    t = (text or "").strip()
    # 去掉 ``` 围栏
    t = re.sub(r"^```(?:markdown|md|text)?\s*\n?", "", t)
    t = re.sub(r"\n?```\s*$", "", t)
    # 去掉「润色后:」等前缀
    t = re.sub(r"^(?:润色后[::]|改写后[::]|好的[,。].{0,30}[:：])\s*", "", t)
    return t.strip()


def _diff_citations(before: str, after: str) -> tuple[set[str], set[str], bool]:
    """返回 (before 引用集合, after 引用集合, 是否完全一致)。"""
    b = _extract_citations(before)
    a = _extract_citations(after)
    return b, a, b == a


def humanize_one_round(
    *,
    round_no: int,
    text: str,
    caller: Callable[..., str],
) -> tuple[str, HumanizeDiff]:
    """跑一轮 humanize。

    Args:
        round_no: 1 / 2 / 3
        text:     待润色文本
        caller:   LLM 调用 callable(system, user, max_tokens=..., ...) -> str
                  签名与 llm.client.messages_create 对齐。

    Returns:
        (new_text, HumanizeDiff)
        - 若差异校验失败,new_text == text(回滚),accepted=False;
        - 若 LLM 异常,new_text == text,accepted=False。
    """
    if round_no < 1 or round_no > 3:
        raise ValueError(f"round_no 必须在 1..3 之间,得到 {round_no}")

    system = HUMANIZE_SYSTEM_TEMPLATES[round_no - 1]
    before_chars = len(text or "")
    citations_before = _extract_citations(text or "")

    try:
        raw = caller(system=system, user=text or "", max_tokens=8000)
        after = _strip_preamble(raw)
    except Exception as exc:
        log.warning("humanize round %d 调用 LLM 失败: %s", round_no, exc)
        return text, HumanizeDiff(
            round_no=round_no,
            before_chars=before_chars,
            after_chars=before_chars,
            citations_before=citations_before,
            citations_after=citations_before,
            citations_preserved=True,
            accepted=False,
            detail=f"LLM 调用失败:{exc}",
        )

    citations_before, citations_after, preserved = _diff_citations(text or "", after)
    if not preserved:
        missing = citations_before - citations_after
        added = citations_after - citations_before
        log.warning(
            "humanize round %d 引用变化,回滚;missing=%s added=%s",
            round_no, missing, added,
        )
        return text, HumanizeDiff(
            round_no=round_no,
            before_chars=before_chars,
            after_chars=before_chars,
            citations_before=citations_before,
            citations_after=citations_before,
            citations_preserved=True,
            accepted=False,
            detail=f"引用变化:missing={sorted(missing)},added={sorted(added)}",
        )

    return after, HumanizeDiff(
        round_no=round_no,
        before_chars=before_chars,
        after_chars=len(after),
        citations_before=citations_before,
        citations_after=citations_after,
        citations_preserved=True,
        accepted=True,
        detail="ok",
    )


def humanize_three_rounds(
    *,
    text: str,
    caller: Callable[..., str],
) -> tuple[str, list[HumanizeDiff]]:
    """三轮连续 humanize,任何一轮失败则保留上一版。"""
    cur = text or ""
    diffs: list[HumanizeDiff] = []
    for r in (1, 2, 3):
        cur, diff = humanize_one_round(round_no=r, text=cur, caller=caller)
        diffs.append(diff)
    return cur, diffs


__all__ = [
    "humanize_one_round",
    "humanize_three_rounds",
    "HumanizeDiff",
    "HUMANIZE_SYSTEM_TEMPLATES",
]