"""pipeline 内嵌 agent:主题划分质检循环。

设计模式(agent 感知-反思-行动闭环嵌入 pipeline 分类环节):
    感知  inspect_group          LLM 自主查看任意分组的文献标题样本
    反思  LLM 自行判断组数/规模/主题互斥性是否合理,决定是否修正
    行动  submit_final_groups    提交终版分组(认可原样提交,或修正后提交)
    兜底  质检循环任何异常 → 原样返回初版分组,不劣化现有 pipeline 行为

对外仍是 pipeline:进度以 (event, data) 元组 yield 给 orchestrator,
由 orchestrator 转成 SSE 事件推给前端,界面交互不变。
"""
from __future__ import annotations

import json
import logging
from typing import Generator

from llm.client import messages_create_with_tools
from retrieval.types import Paper
from writing.classifier import Group, _group_name_acceptable, _min_groups

log = logging.getLogger(__name__)

MAX_AGENT_ROUNDS = 4
# 提交终版时允许的最大文献丢失比例(防 LLM 大量丢文献)
MAX_DROPPED_RATIO = 0.10
# 单组规模上限占比:超过视为划分失衡(与 SYSTEM_PROMPT 的均衡要求对应)
MAX_GROUP_SHARE = 0.60

SYSTEM_PROMPT = """你是「文献主题划分质检员」。初版分组由另一个模型产出,你负责检查并给出终版。

检查要点:
1. 组数适中:3~6 个主题为宜;只有 1 个组且文献较多属于划分失败,必须重新拆分。
2. 规模均衡:单组文献量不宜超过总量的一半;过大应拆分,过小(<5 篇)可并入相近主题。
3. 主题互斥且贴合研究主题,组名是规范的学术主题短语。

工作方式:
- 先用 inspect_group 抽查你觉得可疑的分组(最多查 3 组),基于真实文献判断。
- 然后必须调用 submit_final_groups 提交终版:认可初版就原样提交;不合理就修正(改组名/移动文献/拆分合并)。
- 约束:只能使用池内 lit_id;修正后文献总量不得低于初版的 90%;组数满足下限(≥30 篇至少 4 组,9~29 篇至少 3 组);每组组名必须像章节标题,不允许「其他/杂项/近年来」这类无信息量名称。
"""


def _paper_titles(papers: list[Paper], lit_ids: list[str], limit: int = 20) -> list[str]:
    id_set = set(lit_ids)
    titles = [p.title for p in papers if p.lit_id in id_set]
    return titles[:limit]


def _validate_submission(
    raw_groups: list[dict],
    papers: list[Paper],
    initial_groups: list[Group],
) -> tuple[list[Group] | None, str]:
    """校验 LLM 提交的终版分组。合法返回 (groups, ""),非法返回 (None, 原因)。"""
    if not isinstance(raw_groups, list) or not raw_groups:
        return None, "groups 不能为空"

    known = {p.lit_id for p in papers}
    initial_cover = {lid for g in initial_groups for lid in g.lit_ids}

    final: list[Group] = []
    seen: set[str] = set()
    for item in raw_groups:
        if not isinstance(item, dict):
            return None, "groups 元素必须是对象"
        name = str(item.get("name", "")).strip()
        lit_ids = item.get("lit_ids", [])
        if not name:
            return None, "存在空组名"
        if not isinstance(lit_ids, list):
            return None, f"分组「{name}」的 lit_ids 必须是数组"
        clean_ids: list[str] = []
        for lid in lit_ids:
            lid = str(lid)
            if lid not in known or lid in seen:
                continue  # 池外/重复 lit_id 静默丢弃
            seen.add(lid)
            clean_ids.append(lid)
        if clean_ids:
            final.append(Group(name=name, lit_ids=clean_ids))

    if not final:
        return None, "提交的分组没有任何有效文献"
    # 硬校验 1:组数下限(与 classify 同源,防止 QC 原样放行单巨组)
    min_groups = _min_groups(len(papers))
    if len(final) < min_groups:
        return (
            None,
            f"只分了 {len(final)} 组,低于下限 {min_groups} 组;请按研究方法/情境等维度继续拆分后重新提交",
        )
    # 硬校验 2:组名必须像章节标题(QC 改名也不能引入废品名)
    bad_names = [g.name for g in final if not _group_name_acceptable(g.name)]
    if bad_names:
        return (
            None,
            f"组名不合格: {bad_names};请改成 4~12 字的中文学术名词短语后重新提交",
        )
    # 硬校验 3:单组规模失衡(文献较多时,单组不得超过总量 60%)
    if len(papers) >= 15 and max(len(g.lit_ids) for g in final) > len(papers) * MAX_GROUP_SHARE:
        largest = max(final, key=lambda g: len(g.lit_ids))
        return (
            None,
            f"组「{largest.name}」有 {len(largest.lit_ids)} 篇,超过总量的 60%;请把该组按更细的维度拆分后重新提交",
        )
    dropped = initial_cover - seen
    if initial_cover and len(dropped) > len(initial_cover) * MAX_DROPPED_RATIO:
        return (
            None,
            f"提交版本丢失 {len(dropped)} 篇文献(超过允许的 10%),请把遗漏文献并入相近主题后重新提交",
        )
    return final, ""


def _groups_equal(a: list[Group], b: list[Group]) -> bool:
    if len(a) != len(b):
        return False
    pa = sorted((g.name, tuple(sorted(g.lit_ids))) for g in a)
    pb = sorted((g.name, tuple(sorted(g.lit_ids))) for g in b)
    return pa == pb


TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "inspect_group",
            "description": "查看指定分组的文献数量与标题样本,用于判断分组是否合理",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "分组名称,必须是初版分组之一"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit_final_groups",
            "description": "提交终版分组(质检终点):认可初版就原样提交,不合理就修正后提交",
            "parameters": {
                "type": "object",
                "properties": {
                    "groups": {
                        "type": "array",
                        "description": "终版分组列表",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string", "description": "主题组名"},
                                "lit_ids": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "该组包含的文献 lit_id 列表",
                                },
                            },
                            "required": ["name", "lit_ids"],
                        },
                    },
                    "note": {"type": "string", "description": "一句话说明质检结论或修正思路"},
                },
                "required": ["groups"],
            },
        },
    },
]


def classify_agent_stream(
    topic: str,
    papers: list[Paper],
    initial_groups: list[Group],
) -> Generator[tuple[str, dict], None, tuple[list[Group], dict]]:
    """主题划分 AI 质检 agent。

    yield (event, data) 进度元组;return (终版分组, 质检元信息)。
    任何异常兜底:原样返回初版分组。
    """
    meta: dict = {"checked": True, "rounds": 0, "changed": False, "note": ""}
    final_groups = initial_groups
    try:
        by_name = {g.name: g for g in initial_groups}
        yield ("phase", {"message": f"AI 质检:检查 {len(initial_groups)} 个主题分组的合理性..."})

        msgs: list[dict] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"研究主题:{topic}\n文献总量:{len(papers)} 篇\n"
                    f"初版分组({len(initial_groups)} 组):\n"
                    + "\n".join(
                        f"- {g.name}: {len(g.lit_ids)} 篇" for g in initial_groups
                    )
                ),
            },
        ]

        for round_no in range(1, MAX_AGENT_ROUNDS + 1):
            meta["rounds"] = round_no
            msg = messages_create_with_tools(
                msgs, TOOLS_SCHEMA, max_tokens=2500, temperature=0.2,
            )
            tool_calls = getattr(msg, "tool_calls", None) or []
            if not tool_calls:
                # 模型只回了文本:把文本回喂,要求必须调用 submit_final_groups
                msgs.append({"role": "assistant", "content": getattr(msg, "content", "") or ""})
                msgs.append({
                    "role": "user",
                    "content": "请直接调用 submit_final_groups 提交终版分组。",
                })
                continue

            msgs.append({
                "role": "assistant",
                "content": getattr(msg, "content", None) or "",
                "tool_calls": [
                    {
                        "id": getattr(tc, "id", f"call_{i}"),
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for i, tc in enumerate(tool_calls)
                ],
            })

            submitted: list[Group] | None = None
            for i, tc in enumerate(tool_calls):
                name = getattr(tc.function, "name", "") or ""
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                if not isinstance(args, dict):
                    args = {}

                if name == "inspect_group":
                    gname = str(args.get("name", ""))
                    g = by_name.get(gname)
                    if g is None:
                        result = {
                            "error": f"分组不存在,可用分组: {list(by_name.keys())}",
                        }
                    else:
                        result = {
                            "count": len(g.lit_ids),
                            "titles": _paper_titles(papers, g.lit_ids),
                        }
                        yield ("inspect", {"group": gname, "count": len(g.lit_ids)})
                elif name == "submit_final_groups":
                    groups, reason = _validate_submission(
                        args.get("groups", []), papers, initial_groups,
                    )
                    if groups is None:
                        result = {"error": reason, "hint": "请修正后重新调用 submit_final_groups"}
                        yield ("submit_rejected", {"reason": reason})
                    else:
                        submitted = groups
                        meta["note"] = str(args.get("note", "") or "")
                        result = {"ok": True}
                else:
                    result = {"error": f"未知工具: {name}"}
                msgs.append({
                    "role": "tool",
                    "tool_call_id": getattr(tc, "id", f"call_{i}"),
                    "content": json.dumps(result, ensure_ascii=False),
                })

            if submitted is not None:
                changed = not _groups_equal(submitted, initial_groups)
                meta["changed"] = changed
                final_groups = submitted
                yield ("submit_ok", {
                    "groups": len(submitted),
                    "changed": changed,
                    "note": meta["note"],
                })
                return final_groups, meta

        # 轮次耗尽仍未提交:保留初版
        meta["note"] = "质检未在限定轮次内提交终版,保留初版分组"
        yield ("timeout", {"message": meta["note"]})
        return final_groups, meta
    except Exception as exc:  # noqa: BLE001
        log.warning("主题划分 AI 质检异常,保留初版分组: %s", exc)
        meta["checked"] = False
        meta["note"] = f"质检异常,保留初版: {exc}"
        return initial_groups, meta
