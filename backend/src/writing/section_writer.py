"""章节 LLM 写作:带引用强校验,拒绝幻觉。

核心约束:
- LLM 只能用 [lit_xxx] 形式引用输入清单里存在的 lit_id
- 生成的文本中出现的任何 [lit_xxx] 都必须在允许集合内
- 幻觉引用会被从正文中剥离并记录警告
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from src.llm.client import messages_create
from src.retrieval.types import Paper
from src.writing.classifier import Group
from src.writing.templates import SectionSpec

# 正文中 [lit_xxx] 的匹配
CITE_RE = re.compile(r"\[(lit_[0-9a-f]{16})\]")


@dataclass
class SectionResult:
    """单章写作结果。"""

    key: str
    title: str
    content: str
    citations: list[str] = field(default_factory=list)  # 正文实际引用的 lit_id(保序去重)
    dropped_citations: list[str] = field(default_factory=list)  # 幻觉引用被剥离的


def _build_bibliography(papers: list[Paper]) -> str:
    """生成给 LLM 的文献清单(不含引文字段,只有元数据 + lit_id)。"""
    lines = []
    for p in papers:
        lines.append(
            f"- {p.lit_id} | {p.title} | {', '.join(p.authors)} | "
            f"{p.journal or 'N/A'} | {p.year or 'N/A'}"
        )
    return "\n".join(lines)


def _build_group_desc(groups: list[Group]) -> str:
    return "\n".join(f"- {g.name}: {len(g.lit_ids)} 篇" for g in groups)


def write_section(
    section: SectionSpec,
    topic: str,
    groups: list[Group],
    papers: list[Paper],
    model: str | None = None,
) -> SectionResult:
    """写一章。papers 是本章允许引用的全集。"""
    allowed = {p.lit_id for p in papers}

    system = (
        "你是学术写作助手，正在为学位论文撰写文献综述章节。\n"
        "硬性规则:\n"
        "1. 引用文献时，只能使用 [lit_xxxxxxxxxxxxxxxx] 这种形式；\n"
        "2. lit_id 必须从用户提供的文献清单中精确复制，禁止编造；\n"
        "3. 禁止生成 GB/T 7714 格式引文条目，正文中只允许出现 [lit_xxx] 锚点；\n"
        "4. 不要输出参考文献列表；\n"
        "5. 学术中文书面语，客观、严谨。\n"
        f"本章节写作要求:{section.instruction}"
    )
    user = (
        f"研究主题:{topic}\n\n"
        f"章节:{section.title}\n\n"
        f"文献分组:\n{_build_group_desc(groups)}\n\n"
        f"允许引用的文献清单:\n{_build_bibliography(papers)}\n\n"
        f"请输出本章节正文。"
    )

    content = messages_create(
        system=system,
        user=user,
        max_tokens=4000,
        model=model,
    )

    # 校验引用
    citations: list[str] = []
    dropped: list[str] = []

    def _strip(m: re.Match[str]) -> str:
        lit_id = m.group(1)
        if lit_id in allowed:
            if lit_id not in citations:
                citations.append(lit_id)
            return m.group(0)
        dropped.append(lit_id)
        return ""

    cleaned = CITE_RE.sub(_strip, content)

    return SectionResult(
        key=section.key,
        title=section.title,
        content=cleaned,
        citations=citations,
        dropped_citations=dropped,
    )
