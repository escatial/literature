"""综述分类器:按国内外 / 按主题 对文献分组。

使用 prompts/literature-review.md 模板中的 `classify` 段作为 system prompt。
"""
from __future__ import annotations

import re as _re
from dataclasses import dataclass, field

from prompts.service import parse_llm_json, render
from src.llm.client import messages_create
from src.retrieval.types import Paper, Source
from src.writing.settings import LOCALE_GROUP_DOMESTIC, LOCALE_GROUP_FOREIGN


@dataclass
class Group:
    name: str
    lit_ids: list[str] = field(default_factory=list)


# 通用兜底组名,不作为学术章节标题使用
_GENERIC_GROUP_NAMES = frozenset({
    "其他", "其它", "其他相关研究", "其它相关研究", "其余相关研究",
    "其他研究", "杂项", "others", "other", "misc", "miscellaneous",
    "general", "general research",
})
# 纯英文/数字/符号组成的组名(如 food / study / this / are),
# 说明 LLM 把主题词碎片当成了章节标题,属于未正确分组。
_LATIN_ONLY_GROUP_RE = _re.compile(r"^[A-Za-z0-9\s&+.,()/_-]+$")
# 形如 "food 相关研究" / "study相关研究":英文单词打头、无其他中文描述,
# 属于把主题词碎片拼上"相关研究"当标题。
_EN_FRAGMENT_GROUP_RE = _re.compile(r"^[A-Za-z][A-Za-z0-9&+./-]*\s*相关研究$")
# 有实际学术含义的英文缩写,允许单独作为组名主体(统一大写存储)
_EN_ACRONYM_ALLOWLIST = frozenset({
    "AI", "IOT", "MSC", "ERP", "B2B", "O2O", "SNS", "GDPR",
})


def _group_name_acceptable(name: str) -> bool:
    """组名是否可作为学术综述章节标题。"""
    name = (name or "").strip()
    if not name:
        return False
    if name in _GENERIC_GROUP_NAMES:
        return False
    if _LATIN_ONLY_GROUP_RE.fullmatch(name):
        return False
    m = _EN_FRAGMENT_GROUP_RE.match(name)
    if m:
        head = m.group(0).split()[0]
        if head.upper() not in _EN_ACRONYM_ALLOWLIST:
            return False
    return True


def _groups_acceptable(groups: list[Group]) -> bool:
    """LLM 分组结果是否全部可接受(数量达标 + 组名合格)。"""
    return bool(groups) and all(_group_name_acceptable(g.name) for g in groups)


def classify_by_locale(papers: list[Paper]) -> list[Group]:
    """国内外分类:中文导入(USER_IMPORTED)为国内,其余为国外。"""
    domestic = [p.lit_id for p in papers if p.source == Source.USER_IMPORTED]
    foreign = [p.lit_id for p in papers if p.source != Source.USER_IMPORTED]
    groups: list[Group] = []
    if domestic:
        groups.append(Group(name=LOCALE_GROUP_DOMESTIC, lit_ids=domestic))
    if foreign:
        groups.append(Group(name=LOCALE_GROUP_FOREIGN, lit_ids=foreign))
    return groups


def _parse_group_response(raw: str) -> list[dict]:
    data = parse_llm_json(raw)
    if isinstance(data, dict) and "groups" in data:
        data = data["groups"]
    return [item for item in data if isinstance(item, dict)]


def _build_groups(items: list[dict], valid_ids: set[str]) -> list[Group]:
    covered: set[str] = set()
    groups: list[Group] = []
    for item in items:
        name = item.get("name") or item.get("theme")
        ids = [i for i in item.get("lit_ids", []) if i in valid_ids]
        if not name or not ids:
            continue
        covered.update(ids)
        groups.append(Group(name=name, lit_ids=ids))
    return groups, covered


def _keyword_fallback_groups(papers: list[Paper], topic: str, k: int = 4) -> list[Group]:
    """LLM 只输出 1 组时的兜底:用标题关键词把文献粗分成 k 组。

    策略:对每篇 paper 抽标题 + 摘要前 100 字的关键词,按关键词聚到 k 个 cluster。
    不依赖 sklearn;用词频-共现 + 主题前置法:
    1) 抽高频特征词(去停用、去主题词「无人机/卡车/应急/配送」避免全归一组);
    2) 给每篇 paper 算它在前 k 个特征词上的命中数,贪心归到命中最多的特征;
    3) 命中的特征作为组名,没命中的走「其他相关研究」组。
    """
    import re as _re
    from collections import Counter

    if not papers:
        return [Group(name=topic, lit_ids=[])]

    stop = {
        "无人机", "卡车", "应急", "配送", "物资", "协同", "联合", "问题",
        "研究", "基于", "面向", "一种", "模型", "优化", "设计", "物流",
        "下的", "分析", "构建", "方法", "提出", "针对", "应用", "场景",
        "the", "and", "of", "for", "a", "in", "with", "to", "on",
    }
    # 主题相关停用词(防止聚成一组)+ 英文停用词
    word_re = _re.compile(r"[\u4e00-\u9fff]{2,6}|[A-Za-z]{3,12}")

    doc_tokens: list[list[str]] = []
    for p in papers:
        text = (p.title or "") + " " + (p.abstract or "")[:200]
        # 中文 token 上限 6 字(避免贪婪匹配把整句当作一个 token);
        # 英文 token 上限 12 字符。
        toks = [w for w in word_re.findall(text) if w.lower() not in stop and (len(w) <= 6 if any('\u4e00' <= c <= '\u9fff' for c in w) else len(w) <= 12)]
        doc_tokens.append(toks)

    # 全局词频
    freq: Counter = Counter()
    for toks in doc_tokens:
        freq.update(toks)
    # 取 top 词频且至少出现在 2 篇里的词(避免噪声)
    n_papers = max(1, len(papers))
    # 候选词必须:1) ≥2 篇出现;2) 不超过 80% paper(过滤通用词);
    candidates = [w for w, c in freq.most_common(80) if c >= 2 and c <= 0.8 * n_papers][:20]
    if not candidates:
        return [Group(name=topic, lit_ids=[p.lit_id for p in papers])]
    # 取 top k 词作为组锚词
    anchor_words = candidates[:k]

    groups: list[Group] = []
    bucket: dict[str, list[str]] = {w: [] for w in anchor_words}
    others: list[str] = []
    for p, toks in zip(papers, doc_tokens):
        best = None
        best_n = 0
        for w in anchor_words:
            n = sum(1 for t in toks if t == w)
            if n > best_n:
                best_n = n
                best = w
        if best is not None and best_n > 0:
            bucket[best].append(p.lit_id)
        else:
            others.append(p.lit_id)

    # 给每桶起个能读的主题名:直接用锚词,不再依赖主题硬编码词表
    # (原 label_map 是"无人机协同配送"专用映射,对水产等其他主题会错位)
    for w in anchor_words:
        if not bucket[w]:
            continue
        groups.append(Group(name=f"{w} 相关研究", lit_ids=bucket[w]))
    if others:
        # 兜底组名也简洁自然(用『其余相关研究』而不是 topic 前缀),避免冗长
        groups.append(Group(name="其余相关研究", lit_ids=others))
    return groups or [Group(name=topic, lit_ids=[p.lit_id for p in papers])]


def classify_by_theme(papers: list[Paper], topic: str) -> list[Group]:
    """主题分类:LLM 将文献归入若干主题。

    强约束(代码层兜底):
      - LLM 至少输出 3 组(papers 数量 ≥ 30 时);不满足自动 retry 一次;
      - 仍违规时,改用关键词兜底分组成 k=4 组。
    """
    if not papers:
        return []

    catalog = "\n".join(
        f"- {p.lit_id} | {p.title} | {p.journal or 'N/A'} | {p.year or 'N/A'} | source={p.source.value}"
        for p in papers
    )
    valid_ids = {p.lit_id for p in papers}

    min_groups = 3 if len(papers) >= 30 else 1

    def _ask(force_split: bool = False) -> list[Group]:
        # 防御性注入:组名要像学术综述章节标题(4-10 字,反映研究角度/方法),
        # 严禁通用名(如 'emergency 相关研究' 与主题脱节)
        topic_prefix_reminder = (
            f"\n\n## 重要:组名要像学术综述章节标题 — 简洁(4-10 字)、有信息量。"
            "示例(主题={topic}):「路径建模方法」「算法设计与求解」「不确定性与鲁棒优化」「应急情境应用」。"
            f"**严禁**起通用名(如「emergency 相关研究」「logistics 相关研究」) — 与『{topic}』脱节。\n"
        )
        extra = (
            "\n\n## 强约束提醒\n你之前只输出了 1 个组,这违反了硬性要求。"
            "请按 (a) 研究问题/方法 (b) 应用情境/ (d) 不确定性处理 等维度拆 3-5 个并列子主题。"
            "严禁只输出 1 个总主题。\n"
        ) + topic_prefix_reminder
        system = render(
            "literature-review:classify",
            topic=topic,
            classify_mode="theme",
            papers_catalog=catalog,
        )
        user = f"研究主题:{topic}\n\n文献清单:\n{catalog}{extra}"
        try:
            raw = messages_create(
                system=system, user=user, max_tokens=2000,
                response_format={"type": "json_object"},
            )
            items = _parse_group_response(raw)
        except Exception:
            return []
        groups, _ = _build_groups(items, valid_ids)
        return groups

    groups = _ask(force_split=False)
    if len(groups) < min_groups and len(papers) >= 30:
        # 违规:retry 一次,显式要求拆分
        groups = _ask(force_split=True)
    if len(groups) < min_groups or not _groups_acceptable(groups):
        # 数量不达标或组名不合格(如 food/study/this/are 这类英文碎片、
        # "xxx 相关研究" 无信息量标题、纯通用名):关键词兜底重分组
        fallback = _keyword_fallback_groups(papers, topic, k=4)
        if _groups_acceptable(fallback) or len(fallback) >= min_groups:
            groups = fallback
        else:
            # 兜底也失败:就返回 LLM 原样(或 1 组兜底)
            groups = groups or _ask(force_split=False)
            if not groups:
                return [Group(name=topic, lit_ids=[p.lit_id for p in papers])]

    # 把未覆盖的文献追加到最后一组(避免漏 paper)
    covered = {lid for g in groups for lid in g.lit_ids}
    rest = [p.lit_id for p in papers if p.lit_id not in covered]
    if rest and groups:
        groups[-1].lit_ids.extend(rest)
    return groups


def classify(papers: list[Paper], topic: str, mode: str) -> list[Group]:
    if mode == "locale":
        return classify_by_locale(papers)
    if mode == "theme":
        return classify_by_theme(papers, topic)
    raise ValueError(f"unknown classify mode: {mode}")
