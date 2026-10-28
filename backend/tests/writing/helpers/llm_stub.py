# -*- coding: utf-8 -*-
"""通读笔记批(T02)共享 LLM stub helper,供 T03 存量适配复用。

patch 点说明(单实例纪律,见 src/writing/readthrough.py 模块注释):
- R1/R1' 全部经 src.writing.readthrough(单实例),patch 它的
  messages_create 一处即覆盖逐块通读与节级通读;
- R2 定轴/归组在 src.writing.classifier,patch 它的 messages_create;
- 写作正文流在 src.writing.section_writer 的 messages_stream_ex。

动态笔记模式从 user 清单行(``#N | 标题 | 作者(年份) | 年份 | 摘要:…``)
解析真实作者年份回填进笔记行 —— 与 papers 元数据一致,正文夹注回填
[lit_xxx] 锚点才能命中。
"""
from __future__ import annotations

import json
import re

import src.writing.classifier as _classifier
import src.writing.readthrough as _readthrough
import src.writing.section_writer as _section_writer

# user 清单行 → (编号, 标题, 作者(年份), 年份)
_CATALOG_ROW_RE = re.compile(
    r"^#(\d+)\s*\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*摘要:", re.M
)


def stub_llm_queue(monkeypatch, target, responses: list) -> list[dict]:
    """按调用序弹出响应;队列耗尽后的调用视为测试漏洞,直接报错。

    responses 元素: str(固定响应) | Exception(抛出) |
    Callable[[kwargs, calls], str](动态生成)。
    返回 calls(每次调用的 kwargs 列表)供断言。
    """
    queue = list(responses)
    calls: list[dict] = []

    def fake(**kwargs):
        calls.append(kwargs)
        if not queue:
            raise AssertionError(
                f"意外的第 {len(calls)} 次 LLM 调用(队列已空): "
                f"system={str(kwargs.get('system'))[:120]!r} "
                f"user={str(kwargs.get('user'))[:160]!r}"
            )
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        if callable(item):
            return item(kwargs, calls)
        return item

    monkeypatch.setattr(target, "messages_create", fake)
    return calls


def _dynamic_notes(user: str) -> str:
    """按清单行逐行生成合法行协议笔记(作者年份取自清单本身)。"""
    rows = []
    for m in _CATALOG_ROW_RE.finditer(user or ""):
        no, title, author_year = m.group(1), m.group(2), m.group(3)
        rows.append(
            f"#{no}|{author_year}|研究问题{no}:该文如何解释{title[:10]}的核心机制"
            f"|问卷实证分析|结论要点{no}:关键变量显著影响绩效|主题线索{int(no) % 3}"
        )
    if not rows:
        raise AssertionError(f"通读 stub 未在 user 里找到 #N 清单行: {user[:200]!r}")
    return "\n".join(rows)


def stub_readthrough(
    monkeypatch,
    responses: list | None = None,
    fail: bool = False,
) -> list[dict]:
    """R1 逐块通读 / R1' 节级通读的统一 stub。

    - 默认(无 responses):动态按 user 清单生成合法行协议笔记;
    - responses: 按调用序弹出(同 stub_llm_queue 语义);
    - fail=True: 每次调用都抛 RuntimeError(降级路径专用)。
    返回 calls。
    """
    calls: list[dict] = []

    def fake(**kwargs):
        calls.append(kwargs)
        if fail:
            raise RuntimeError("模拟通读笔记 LLM 不可用")
        if responses:
            item = responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item(kwargs, calls) if callable(item) else item
        return _dynamic_notes(str(kwargs.get("user") or ""))

    monkeypatch.setattr(_readthrough, "messages_create", fake)
    return calls


def axis_plan_payload(names: list[str]) -> str:
    """R2 定轴 JSON(theme 轴,带 research_question)。"""
    return json.dumps({
        "themes": [
            {"name": name, "research_question": f"{name}的研究问题"}
            for name in names
        ],
    }, ensure_ascii=False)


def assign_line_payload(names: list[str], n_papers: int) -> str:
    """归组行协议(每篇一行「编号. 轴名」,轴名循环取用)。"""
    return "\n".join(
        f"{i}. {names[(i - 1) % len(names)]}" for i in range(1, n_papers + 1)
    )


_SECTION_BODY = (
    "本节梳理渠道策略研究。王小明（2022）认为渠道选择显著影响企业绩效,"
    "李航海（2022）认为线上渠道扩张是增长的关键机制,张远航（2022）认为"
    "渠道协同决定转化效率,陈观澜（2022）认为数字化程度调节上述关系强度。"
)


def stub_section_notes(
    monkeypatch,
    body: str | None = None,
    fail: bool = False,
) -> list[dict]:
    """R1' 节级通读 stub(笔记)+ 写作正文 stub,组合注入。

    同时 patch 流式(messages_stream_ex)与同步(_invoke_section_chain 底层
    messages_create)两条正文链路 —— T03 起同步链也接通读笔记。
    body 为正文(经作者(年份)夹注回填 [lit_xxx] 锚点);
    返回 readthrough.messages_create 的 calls 供断言。
    """
    rt_calls = stub_readthrough(monkeypatch, fail=fail)

    def fake_stream(system, user, max_tokens=4000, model=None, **kw):
        yield ("reasoning", "思考中")
        yield ("content", body if body is not None else _SECTION_BODY)

    monkeypatch.setattr(_section_writer, "messages_stream_ex", fake_stream)
    monkeypatch.setattr(
        _section_writer, "messages_create",
        lambda *args, **kwargs: body if body is not None else _SECTION_BODY,
    )
    return rt_calls
