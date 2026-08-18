"""激进的极简版综述 pipeline: 2 次 LLM + 1 次检索。

- 不分章节、不筛不分类、不流式、不组 session
- topic in → review out
- 2 次 LLM 调用(检索式拆解 + 一次性写综述)
- 检索只用 OpenAlex(CNKI 验证码绕道,够用)
"""
from __future__ import annotations

import json
import logging
from typing import Any

from src.llm.client import get_default_provider, messages_create
from src.retrieval.openalex_adapter import OpenAlexAdapter
from src.retrieval.types import Paper

log = logging.getLogger(__name__)


# ---------- 1) LLM 拆检索式 ----------
_QUERY_SYSTEM = """你是学术检索专家。把研究主题拆成结构化搜索词。

输出严格 JSON:
{
  "topic_summary": "一句英文研究问题",
  "keywords_en": ["英文关键词1", "英文关键词2", "英文关键词3"],
  "year_start": 2020,
  "year_end": 2026
}

- keywords 3-5 个, 学术/常用缩写优先
- year_range 按主题合理填, 默认 2020-2026
- 不要任何额外文字
"""


def build_query(topic: str, provider: str | None = None, model: str | None = None) -> dict:
    raw = messages_create(
        system=_QUERY_SYSTEM,
        user=f"研究主题:{topic}",
        max_tokens=400,
        response_format={"type": "json_object"},
        provider=provider,
        model=model,
    )
    from prompts.service import parse_llm_json
    parsed = parse_llm_json(raw)
    return {
        "topic_summary": parsed.get("topic_summary", topic),
        "keywords_en": parsed.get("keywords_en", [topic]),
        "year_start": int(parsed.get("year_start", 2020) or 2020),
        "year_end": int(parsed.get("year_end", 2026) or 2026),
    }


# ---------- 2) 检索 ----------
def search_papers(query: dict, limit: int = 20) -> list[Paper]:
    """OpenAlex 检索, 返回去重后的 Paper 列表。"""
    adapter = OpenAlexAdapter()
    keywords = " ".join(query.get("keywords_en", []))
    if not keywords.strip():
        keywords = query.get("topic_summary", "")
    year_range = (query.get("year_start", 2020), query.get("year_end", 2026))
    papers = adapter.search(
        query=keywords,
        year_range=year_range,
        per_page=limit,
    )
    return papers[:limit]


# ---------- 3) LLM 一次性写综述 ----------
_WRITE_SYSTEM = """你是严谨的学术写作助手。根据提供的文献清单, 撰写"${topic}"主题的文献综述。

## 强制要求

- 第三人称客观表述, 严禁"我/我们/笔者"
- 字数 3000-6000 字
- 引用格式:作者(年份)[lit_id],lit_id 严格从下方"可用 lit_id 列表"中选,不要编造
- 每篇文献只引用一次
- 严禁 AI 套话:"此外""深入探讨""重要组成部分""为 X 提供新视角""具有理论意义和实践价值"等
- 用学者动作词轮换:考察/检视/梳理/辨析/剖析/管窥/审视/回顾/探究
- 句长变异:目标方差 ≥8, 避免每句 18-25 字的均匀长度
- 适度模糊:可"可能/或许/有待验证"
- 不要跑题、题外话、抽象主张
- 当文献覆盖不足时直接说"研究较少",禁止编造

## 输出格式

严格 JSON, 不要任何额外文字:
{
  "review": "综述正文(纯文本,带 [lit_xxx] 内联引用)",
  "references": ["参考文献 1(GB/T 7714 格式:作者.题名[J].期刊,年,卷(期):页码.)", "参考文献 2", ...]
}

review 字段只能包含综述正文 + 内联引用, 不要 markdown 标题或开场白。
references 字段是 GB/T 7714 格式的纯文本条目,顺序与正文引用顺序一致。
"""


def write_review(topic: str, papers: list[Paper], provider: str | None = None, model: str | None = None) -> dict:
    """1 次 LLM 调用, 直接产出全文 + 参考文献。"""
    catalog_lines = []
    for p in papers:
        abstract_short = (p.abstract or "")[:400] if p.abstract else "(无摘要)"
        authors = ", ".join(p.authors[:3]) if p.authors else "佚名"
        catalog_lines.append(
            f"- {p.lit_id} | {p.title} | {authors} | "
            f"{p.journal or 'N/A'} | {p.year or 'N/A'}\n"
            f"  摘要:{abstract_short}"
        )
    papers_catalog = "\n".join(catalog_lines) if catalog_lines else "(无文献)"
    available_lit_ids = ", ".join(p.lit_id for p in papers)
    
    user = (
        f"研究主题:{topic}\n\n"
        f"文献清单(每条带 lit_id 和摘要):\n{papers_catalog}\n\n"
        f"可用 lit_id 列表(必须严格从这里选, 不要编造):\n{available_lit_ids}\n\n"
        f"请输出综述(3000-6000 字)和参考文献列表。"
    )
    
    raw = messages_create(
        system=_WRITE_SYSTEM.replace("${topic}", topic),
        user=user,
        max_tokens=6000,
        response_format={"type": "json_object"},
        provider=provider,
        model=model,
    )
    from prompts.service import parse_llm_json
    return parse_llm_json(raw)


# ---------- 一站式入口 ----------
def run_simple_review(topic: str, max_papers: int = 20, provider: str | None = None, model: str | None = None) -> dict:
    """Topic → 综述(2 LLM + 1 检索)。"""
    log.info("simple_review: build_query for topic=%r", topic)
    query = build_query(topic, provider=provider, model=model)
    log.info("simple_review: query=%s", json.dumps(query, ensure_ascii=False))
    
    log.info("simple_review: searching (limit=%d)", max_papers)
    papers = search_papers(query, limit=max_papers)
    log.info("simple_review: found %d papers", len(papers))
    
    if not papers:
        return {
            "review": "未检索到与该主题相关的文献, 无法生成综述。",
            "references": [],
            "papers_found": 0,
            "query": query,
            "provider": provider or get_default_provider(),
        }
    
    log.info("simple_review: writing review via LLM")
    result = write_review(topic, papers, provider=provider, model=model)
    
    return {
        "review": result.get("review", ""),
        "references": result.get("references", []),
        "papers_found": len(papers),
        "query": query,
        "provider": provider or get_default_provider(),
    }
