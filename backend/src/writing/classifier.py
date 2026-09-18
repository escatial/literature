"""综述分类器:按国内外 / 按主题 对文献分组。

使用 prompts/literature-review.md 模板中的 `classify` 段作为 system prompt。
"""
from __future__ import annotations

import json as _json
import logging
import math
import re as _re
from dataclasses import dataclass, field

from prompts.service import parse_llm_json, render
from src.llm.client import messages_create
from src.retrieval.types import Paper, Source
from src.writing.settings import LOCALE_GROUP_DOMESTIC, LOCALE_GROUP_FOREIGN

logger = logging.getLogger(__name__)


@dataclass
class Group:
    name: str
    lit_ids: list[str] = field(default_factory=list)


# 通用兜底组名,不作为学术章节标题使用
# 注意:同时收录清洗后形态 —— "其余相关研究" 会被 _clean_group_name 剥成"其余"
_GENERIC_GROUP_NAMES = frozenset({
    "其他", "其它", "其他相关研究", "其它相关研究", "其余相关研究",
    "其余", "其他研究", "其他文献", "其它文献", "其他主题",
    "其他方面", "其他内容", "其他类别",
    "研究主题", "主题研究", "主题分类", "研究方向",
    "杂项", "others", "other", "misc", "miscellaneous",
    "general", "general research",
})
# 纯英文/数字/符号组成的组名,
# 说明 LLM 把主题词碎片当成了章节标题,属于未正确分组。
_LATIN_ONLY_GROUP_RE = _re.compile(r"^[A-Za-z0-9\s&+.,()/_-]+$")
# 形如 "英文单词+相关研究/综述":英文单词打头、无其他中文描述,
# 属于把主题词碎片拼上"相关研究/综述"当标题。
_EN_FRAGMENT_GROUP_RE = _re.compile(
    r"^([A-Za-z][A-Za-z0-9&+./-]*)\s*(相关研究|相关综述|研究综述|研究述评|综述|述评)$"
)
_PLACEHOLDER_GROUP_RE = _re.compile(
    r"^(?:(?:主题(?:方向)?|文献子主题|研究议题|研究主题|研究方向)|.*子主题)\s*[一二三四五六七八九十0-9]+$"
)
_CHINESE_SOURCES = frozenset({Source.CNKI, Source.USER_IMPORTED})
_KEYWORD_CHAIN_RE = _re.compile(r"^(?:[\u4e00-\u9fff]{1,3}与){2,}[\u4e00-\u9fff]{1,4}$")
_SLASH_CHAIN_RE = _re.compile(r"\s*[/|]+\s*")
# 有实际学术含义的英文缩写,允许单独作为组名主体(统一大写存储)
_EN_ACRONYM_ALLOWLIST = frozenset({
    "AI", "IOT", "MSC", "ERP", "B2B", "O2O", "SNS", "GDPR",
})
# 英文功能词在中英文混合标题中出现频率极高，却不能表达研究主题。
# 兜底聚类命名绝不能把它们拼成“of与in与to”一类伪章节标题。
_ENGLISH_STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from",
    "in", "into", "is", "it", "its", "of", "on", "or", "the", "to",
    "with", "without", "via", "using", "use", "based", "study", "studies",
    "research", "review", "analysis", "approach", "method", "methods",
    "this", "that", "these", "those", "their", "there", "here", "can", "could",
    "may", "might", "will", "would", "should", "also", "than", "then", "such",
    "new", "two", "one", "using", "used", "use", "based", "results", "result",
    "paper", "papers", "work", "works", "shown", "show", "find", "findings",
    "ch", "learnin", "learning",
})
_TITLE_LEAD_WORDS = ("基于", "面向", "考虑", "关于", "针对", "采用", "通过")
_TITLE_CLAUSE_MARKERS = ("的", "背景下", "情境下", "条件下", "环境下", "中", "下")
# 叙述性短语/连接词,严禁作为章节标题(LLM 偶发返回"近年来"、"然而"等)
# 这类词没有信息量,只能用作正文过渡
_NARRATIVE_GROUP_NAMES = frozenset({
    "近年来", "然而", "综上所述", "综上", "总而言之", "首先", "其次",
    "再次", "最后", "一方面", "另一方面", "此外", "同时", "因此",
    "故", "是故", "进而", "更进一步", "更进一步地", "首先来看", "总的来说",
    "如前所述", "上文所述", "下文将", "下文", "下文将展开", "以下将",
    "以下", "下面", "本节", "本章节", "本章", "本章将",
    "具体来说", "具体而言", "具体地", "事实上", "实际中", "实践中",
    "近年来", "近些年", "近几年来", "近段时间", "近一时期",
    "回顾", "回顾历史", "回顾性", "现状", "发展趋势", "未来展望",
    "目前", "当前", "当下", "现阶段", "新时期", "新形势下",
})
# 主题名不能停在未完成的词尾。除常见的“与/和/及/的”等半截连接词外，
# 还拦截“配方向”这类把“配送方向”截断后的残片；这里按语言形态判断，
# 不绑定任何具体研究领域。
_TRUNCATED_GROUP_SUFFIX_RE = _re.compile(r"(?:与|和|及|的|等|之|其)$")
_TITLE_LEAD_RE = _re.compile(r"^(?:基于|面向|考虑|关于|针对|采用|通过)")
# 组名长度上限:超出 12 字的 LLM 描述倾向堆砌,不再像标题
_GROUP_NAME_MAX_LEN = 20


def _group_name_acceptable(name: str) -> bool:
    """组名是否可作为学术综述章节标题。

    硬约束:
      - 中文标题不超过 20 字，英文标题不超过 48 字
      - 不是通用兜底名("其他"/"杂项"/"misc")
      - 不是叙述性短语("近年来"/"然而"/"综上所述" 等)
      - 不是英文碎片(纯英文单词或"英文单词+相关研究"式拼接)
    """
    name = (name or "").strip()
    if not name:
        return False
    if _TITLE_LEAD_RE.match(name):
        return False
    # 斜杠、竖线连接的对象词是关键词清单，不是研究问题导向主题。
    if _SLASH_CHAIN_RE.search(name):
        return False
    if _PLACEHOLDER_GROUP_RE.fullmatch(name):
        return False
    if _KEYWORD_CHAIN_RE.fullmatch(name):
        return False
    if name in _GENERIC_GROUP_NAMES or name in _NARRATIVE_GROUP_NAMES:
        return False
    if len(name) > 2 and _TRUNCATED_GROUP_SUFFIX_RE.search(name):
        return False
    chinese_chars = _re.findall(r"[\u4e00-\u9fff]", name)
    latin_words = [w.lower() for w in _re.findall(r"[A-Za-z][A-Za-z0-9-]*", name)]
    if _LATIN_ONLY_GROUP_RE.fullmatch(name):
        # 英文文献簇允许使用包含至少两个实质词的英文主题名；
        # 纯英文功能词/单词碎片仍然拒绝，避免出现 such/data/future 一类伪标题。
        meaningful = [w for w in latin_words if w not in _ENGLISH_STOPWORDS]
        if len(name) > 80 or not meaningful:
            return False
        return True
    if len(name) > _GROUP_NAME_MAX_LEN:
        return False
    # 主题章节默认必须是中文学术短语。英文片段（如
    # ``this与learnin``、``this与that与ch``）即使夹了连接字也不是主题。
    if len(chinese_chars) < 2 and not latin_words:
        return False
    if latin_words and chinese_chars and any(w.upper() not in _EN_ACRONYM_ALLOWLIST for w in latin_words):
        return False
    m = _EN_FRAGMENT_GROUP_RE.match(name)
    if m:
        head = m.group(1)
        if head.upper() not in _EN_ACRONYM_ALLOWLIST:
            return False
    return True


def _clean_group_name(name: str) -> str:
    """轻度清洗:剥掉前缀编号("1." / "一、"/ "(1)")和尾部"...相关研究"。

    不改变语义,只把 LLM 偶发的修饰性前缀/后缀去掉,得到学术名词短语。
    """
    name = (name or "").strip()
    # 剥前导编号: "1. xxx" / "一、xxx" / "(1) xxx"
    name = _re.sub(r"^[\d一二三四五六七八九十]+[\.\u3001\s\)\(]+", "", name)
    # 剥尾部"...相关研究"/"...综述"
    name = _re.sub(r"(相关研究|相关综述|研究综述|研究述评|综述|述评)$", "", name)
    # “方向/议题/问题”是展示层泛化尾缀，不应把同一研究对象拆成
    # “对象”和“对象方向”两个高度重叠的章节名。
    name = _re.sub(r"(?:研究)?(?:方向|议题|问题)$", "", name)
    # 论文标题常以“基于/面向/考虑”等方法性引导语开头；章节名保留
    # 研究对象或问题即可，去掉这些标题句式不会引入领域假设。
    name = _TITLE_LEAD_RE.sub("", name).strip()
    # 通用清洗只处理格式噪声，不替换任何领域词，避免把某个样例领域
    # 的术语写死到通用文献综述工具中。语义不可靠的名称由上层校验回退。
    return name.strip()


def _looks_like_title_prefix(name: str, papers: list[Paper], lit_ids: list[str]) -> bool:
    """拒绝模型把长标题截断后直接当作主题名。"""
    compact_name = _re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", name or "")
    if len(compact_name) < 6:
        return False
    by_id = {p.lit_id: p for p in papers}
    for lit_id in lit_ids:
        title = _re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", by_id.get(lit_id).title if by_id.get(lit_id) else "")
        if title and len(title) > len(compact_name) and title.startswith(compact_name):
            return True
    return False


def _group_name_conflicts(name: str, used_names: set[str]) -> bool:
    """Reject exact or containment-equivalent theme names."""
    compact = _re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", name or "")
    if not compact:
        return True
    for used in used_names:
        other = _re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", used or "")
        if compact == other or compact in other or other in compact:
            return True
    return False


def _derive_group_name(papers: list[Paper], lit_ids: list[str], index: int = 1) -> str:
    """从真实代表文献标题提取可读主题名，不依赖任何领域词典。

    主题名是展示层信息，不能把模型截断片段或 ``主题一`` 这样的序号
    伪装成章节标题。优先取代表标题中的连续中文问题短语，英文标题则
    取包含研究问题信号的实词组合；无法提取时返回空串，让上层拒绝该组。
    """
    by_id = {p.lit_id: p for p in papers}
    representative = next((by_id[lid] for lid in lit_ids if lid in by_id), None)
    if representative is None:
        return ""
    title = (representative.title or "").strip()
    phrases = []
    for phrase in _re.findall(r"[\u4e00-\u9fff]{4,}", title):
        candidate = phrase
        for lead in _TITLE_LEAD_WORDS:
            candidate = _re.sub(rf"^{lead}", "", candidate)
        # 标题中的“X的Y”“X背景下Y”通常已经给出可读的研究对象；
        # 优先保留前置对象，避免把整句标题硬截断成半个词。
        for marker in _TITLE_CLAUSE_MARKERS:
            if marker in candidate:
                prefix = candidate.split(marker, 1)[0]
                if len(prefix) >= 4:
                    candidate = prefix
                    break
        while len(candidate) > 12 and candidate[-1] in "与和及的等之其路":
            candidate = candidate[:-1]
        # 不再按固定字符数截断标题。固定截断会把“自主着陆”截成
        # “应急自”这类半个词；只有在自然学术结尾处才收束，超长且
        # 没有边界的片段直接交给可接受性校验拒绝。
        if len(candidate) > _GROUP_NAME_MAX_LEN:
            # 只按标题自身的标点或自然分句边界收束，不使用领域词表截断。
            pieces = [p.strip() for p in _re.split(r"[：:，,；;。.!！?？]", candidate) if p.strip()]
            candidate = max(pieces, key=len) if pieces else ""
        if len(candidate) >= 4 and _group_name_acceptable(candidate):
            phrases.append(candidate)
    if phrases:
        return max(phrases, key=len)[:_GROUP_NAME_MAX_LEN]
    words = [
        w for w in _re.findall(r"[A-Za-z][A-Za-z0-9-]{2,}", title)
        if w.lower() not in _ENGLISH_STOPWORDS
    ]
    meaningful = [w for w in words if len(w) > 2]
    if len(meaningful) >= 2:
        return " ".join(meaningful[:4])[:48].strip()
    return ""


def _unique_evidence_group_name(
    papers: list[Paper], lit_ids: list[str], used_names: set[str], index: int,
) -> str:
    """从代表文献标题生成稳定且不重复的展示名。"""
    by_id = {p.lit_id: p for p in papers}
    representative = next((by_id[lid] for lid in lit_ids if lid in by_id), None)
    if representative is None:
        return ""
    # 尝试簇内多个标题，而不是只看第一个代表标题。大池子中第一个标题
    # 可能是短标题/重复标题，单一代表会退化成“子主题N”或直接失败。
    candidates: list[str] = []
    ordered_ids = list(dict.fromkeys(lit_ids))
    # 中文综述在主题可混合的前提下优先使用中文证据命名；语言不是
    # 聚类维度，但展示层不应因为簇首恰好是英文文献而出现英文章节。
    ordered_ids.sort(key=lambda lid: (0 if by_id.get(lid) and _is_chinese_paper(by_id[lid]) else 1))
    for lid in ordered_ids:
        candidate = _clean_group_name(_derive_group_name(papers, [lid], index))
        if candidate:
            candidates.append(candidate)
        paper = by_id.get(lid)
        title = (paper.title if paper else "") or ""
        zh = [x for x in _re.findall(r"[\u4e00-\u9fff]{4,}", title)]
        candidates.extend(_clean_group_name(x) for x in zh)
        words = [w for w in _re.findall(r"[A-Za-z][A-Za-z0-9-]{2,}", title)
                 if w.lower() not in _ENGLISH_STOPWORDS]
        if len(words) >= 2:
            candidates.append(" ".join(words[:4])[:48].strip())
    # 保留顺序去重，优先使用最有证据的完整短语。
    candidates = list(dict.fromkeys(c for c in candidates if c))
    has_chinese_evidence = any(
        lid in by_id and _is_chinese_paper(by_id[lid]) for lid in lit_ids
    )
    for candidate in candidates:
        # 只要该主题含有中文证据，章节名必须优先保持中文；英文仅在
        # 整个主题确实没有中文文献时才允许，避免混合主题出现英文章节。
        if has_chinese_evidence and not _re.search(r"[\u4e00-\u9fff]", candidate):
            continue
        if (
            candidate and not _group_name_conflicts(candidate, used_names)
            and _group_name_acceptable(candidate)
            and not _looks_like_title_prefix(candidate, papers, lit_ids)
        ):
            return candidate
    return ""


def _finalize_theme_groups(
    papers: list[Paper], groups: list[Group], topic: str,
) -> list[Group]:
    """统一主题结果的名称、覆盖率和最低分组数。

    LLM 可能返回截断名称、重复归属或漏掉文献。此处是分类器的最终
    不变量：每篇输入文献恰好归入一组；规模足够时至少达到规模化主题
    数；任何不可靠名称都从真实标题重新提取，绝不展示占位标题。
    """
    valid = {p.lit_id for p in papers}
    normalized: list[Group] = []
    seen_ids: set[str] = set()
    seen_names: set[str] = set()
    for group in groups or []:
        ids = [lid for lid in group.lit_ids if lid in valid and lid not in seen_ids]
        if not ids:
            continue
        name = _clean_group_name(group.name)
        if (
            not _group_name_acceptable(name)
            or _looks_like_title_prefix(name, papers, ids)
            or _group_name_conflicts(name, seen_names)
        ):
            name = _unique_evidence_group_name(
                papers, ids, seen_names, len(normalized) + 1,
            )
        if not name or not _group_name_acceptable(name) or _group_name_conflicts(name, seen_names):
            continue
        normalized.append(Group(name=name, lit_ids=ids))
        seen_ids.update(ids)
        seen_names.add(name)

    if not normalized:
        normalized = _deterministic_fallback_groups(papers, topic)
        return normalized

    # 漏分文献按标题相似度回收到最接近的主题，保证分类覆盖全集。
    _reassign_orphans(papers, [p.lit_id for p in papers if p.lit_id not in seen_ids], normalized)

    target = min(_desired_auto_groups(len(papers)), len(papers))
    # 若模型只给出过少主题，拆分最大簇；拆分名仍从真实标题提取。
    while len(normalized) < target:
        source = max(normalized, key=lambda g: len(g.lit_ids))
        if len(source.lit_ids) < 2:
            break
        cut = max(1, len(source.lit_ids) // 2)
        moved = source.lit_ids[cut:]
        source.lit_ids = source.lit_ids[:cut]
        new_name = _unique_evidence_group_name(
            papers, moved, {g.name for g in normalized}, len(normalized) + 1,
        )
        if not new_name:
            # 标题短语不足时保留原组名并拒绝伪造新标题；下一轮会停止。
            source.lit_ids.extend(moved)
            break
        normalized.append(Group(name=new_name, lit_ids=moved))

    # 不能让“目标 4 组、实际 2 组”的退化结果进入正文。重新用确定性
    # 标题证据聚类，并再次收敛名称；该分支不依赖领域词典或 LLM。
    if len(normalized) < target:
        fallback = _deterministic_fallback_groups(papers, topic)
        fallback_normalized: list[Group] = []
        used: set[str] = set()
        for index, fallback_group in enumerate(fallback, start=1):
            ids = [lid for lid in fallback_group.lit_ids if lid in valid]
            name = _unique_evidence_group_name(papers, ids, used, index)
            if ids and name:
                fallback_normalized.append(Group(name=name, lit_ids=ids))
                used.add(name)
        if len(fallback_normalized) >= target:
            normalized = fallback_normalized
            seen_ids = {lid for group in normalized for lid in group.lit_ids}
            _reassign_orphans(
                papers,
                [p.lit_id for p in papers if p.lit_id not in seen_ids],
                normalized,
            )

    normalized = [g for g in normalized if g.lit_ids]
    _rebalance_language_groups(normalized, papers)
    return normalized


def _groups_acceptable(groups: list[Group]) -> bool:
    """LLM 分组结果是否全部可接受(数量达标 + 组名合格)。"""
    return bool(groups) and all(_group_name_acceptable(g.name) for g in groups)


def _is_chinese_paper(paper: Paper) -> bool:
    return paper.source in _CHINESE_SOURCES


def _language_counts(lit_ids: list[str], by_id: dict[str, Paper]) -> tuple[int, int]:
    cn = sum(1 for lid in lit_ids if lid in by_id and _is_chinese_paper(by_id[lid]))
    return cn, len(lit_ids) - cn


def _mixed_language_feasible(papers: list[Paper], group_count: int) -> bool:
    cn, en = _language_counts([p.lit_id for p in papers], {p.lit_id: p for p in papers})
    return group_count > 1 and cn >= group_count and en >= group_count


def _groups_language_balanced(groups: list[Group], papers: list[Paper]) -> bool:
    """语言仅作质量约束：可行时每个主题都应同时含中英文文献。"""
    if not groups:
        return False
    if not _mixed_language_feasible(papers, len(groups)):
        return True
    by_id = {p.lit_id: p for p in papers}
    return all(_language_counts(g.lit_ids, by_id)[0] > 0 and _language_counts(g.lit_ids, by_id)[1] > 0 for g in groups)


def _rebalance_language_groups(groups: list[Group], papers: list[Paper]) -> None:
    """在语义分组完成后修复语言分仓；语言是质量约束，不是聚类主轴。

    可行时不仅要求每组同时有中英文，还尽量按全池语言比例分配，避免
    出现某组几乎全中文、另一组几乎全英文的隐性语言分仓。
    """
    if not groups or not _mixed_language_feasible(papers, len(groups)):
        return
    by_id = {p.lit_id: p for p in papers}
    total_cn, total_en = _language_counts([p.lit_id for p in papers], by_id)
    min_cn = total_cn // len(groups)
    min_en = total_en // len(groups)
    # 先补齐每组的语言底线，再按余数尽量均衡；每次只移动最相近证据。
    for _ in range(len(groups) * 4):
        changed = False
        for target in groups:
            cn, en = _language_counts(target.lit_ids, by_id)
            need_cn = cn < max(1, min_cn)
            need_en = en < max(1, min_en)
            for need_chinese, needed in ((True, need_cn), (False, need_en)):
                if not needed:
                    continue
                candidates = []
                for donor in groups:
                    if donor is target:
                        continue
                    dcn, den = _language_counts(donor.lit_ids, by_id)
                    count = dcn if need_chinese else den
                    floor = max(1, min_cn if need_chinese else min_en)
                    if count <= floor:
                        continue
                    for lid in donor.lit_ids:
                        p = by_id.get(lid)
                        if p and _is_chinese_paper(p) == need_chinese:
                            score = max((_title_similarity(p.title, by_id[x].title)
                                         for x in target.lit_ids if x in by_id), default=0.0)
                            candidates.append((score, donor, lid))
                if candidates:
                    _, donor, lid = max(candidates, key=lambda x: x[0])
                    donor.lit_ids.remove(lid)
                    target.lit_ids.append(lid)
                    changed = True
        if not changed:
            break


def classify_by_locale(papers: list[Paper]) -> list[Group]:
    """国内外分类:中文源(CNKI / 中文手动导入)为国内,其余为国外。

    v9.6:此前只有 USER_IMPORTED 算国内,CNKI 中文文献全部落入「国外研究」
    章节——与 orchestrator/qa 模块「CNKI 算中文源」的事实标准(_CHINESE_SOURCES)
    直接矛盾。
    """
    chinese = {Source.CNKI, Source.USER_IMPORTED}
    domestic = [p.lit_id for p in papers if p.source in chinese]
    foreign = [p.lit_id for p in papers if p.source not in chinese]
    groups: list[Group] = []
    if domestic:
        groups.append(Group(name=LOCALE_GROUP_DOMESTIC, lit_ids=domestic))
    if foreign:
        groups.append(Group(name=LOCALE_GROUP_FOREIGN, lit_ids=foreign))
    return groups


def _salvage_groups_json(raw: str) -> list[dict] | None:
    """从「分析文字 + JSON」混合输出中抢救 groups 数组。

    MiniMax 偶发无视 json_object 约束,先写大段分析过程再给 JSON;
    parse_llm_json 只认「整段是合法 JSON」,这里做括号配对二次抢救。
    返回 None 表示全文确实没有可解析的 {"groups": [...]} 结构(如被截断)。
    """
    marker = raw.find('{"groups"')
    if marker == -1:
        marker = raw.find('{ "groups"')
    if marker == -1:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(marker, len(raw)):
        ch = raw[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = _json.loads(raw[marker : i + 1])
                except Exception:
                    depth = 0  # 伪闭合(JSON 无效),继续向后找完整结构
                    continue
                groups = obj.get("groups") if isinstance(obj, dict) else None
                if isinstance(groups, list):
                    return [item for item in groups if isinstance(item, dict)]
                return None
    return None  # 未闭合(输出被截断),无法抢救


def _salvage_truncated_groups(raw: str) -> list[dict] | None:
    """从「未闭合」(输出被截断)的 JSON 里抢救已完整写出的分组对象。

    大池子(300+ 篇)时输出可能在中途被 max_tokens 截断:
        {"groups": [{"name": "A", "ids": [1, 2]}, {"name": "B", "ids":
    此时最后半个对象作废,前面完整的分组对象仍然有效。缺组的文献由
    _reassign_orphans 按标题相似度归组 —— 截断不再等于整体失败。
    """
    marker = raw.find('"groups"')
    if marker == -1:
        return None
    arr_start = raw.find("[", marker)
    if arr_start == -1:
        return None
    items: list[dict] = []
    i, n = arr_start + 1, len(raw)
    while i < n:
        while i < n and raw[i] in " \t\r\n,":
            i += 1
        if i >= n or raw[i] != "{":
            break
        depth, in_str, esc, j = 0, False, False, i
        while j < n:
            ch = raw[j]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        break
            j += 1
        if j >= n:
            break  # 最后一个对象未闭合,丢弃
        try:
            obj = _json.loads(raw[i : j + 1])
        except Exception:
            obj = None
        if isinstance(obj, dict):
            items.append(obj)
        i = j + 1
    return items or None


def _salvage_text_groups(raw: str) -> list[dict] | None:
    """抢救模型输出的自然语言分组(例如 ``Group 1: xxx - #1, #2``)。

    部分模型在 response_format=json_object 下仍会输出分析文字；只要其中
    已经给出了组名和编号，就直接转成协议对象，避免整块再次重试。
    """
    text = str(raw or "")
    headings = list(_re.finditer(
        r"(?im)^\s*(?:\*{1,2})?(?:group|分组|主题)\s*\d+\s*[:：]\s*(.+?)(?:\*{1,2})?\s*$",
        text,
    ))
    groups: list[dict] = []
    for index, match in enumerate(headings):
        name = match.group(1).strip().strip("* ")
        body_end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        body = text[match.end():body_end]
        # 只认带 # 的文献编号，避免把年份、百分比、模型代号误当 ids。
        ids = [int(x) for x in _re.findall(r"#(\d+)\b", body)]
        # 将模型的解释性长标题收束为章节短语，但不引入任何领域词。
        slash_parts = [part.strip() for part in _SLASH_CHAIN_RE.split(name) if part.strip()]
        if len(slash_parts) == 2:
            name = "与".join(slash_parts)
        elif len(slash_parts) > 2:
            # 多个并列价值/约束后通常跟“导向的X”；保留最后一个概括项和
            # 论证对象，比把整串关键词作为标题更可读。
            name = slash_parts[-1]
        for marker in ("下的", "条件下的", "背景下的", "情境下的"):
            if marker in name:
                prefix = name.split(marker, 1)[0]
                if 4 <= len(prefix) <= _GROUP_NAME_MAX_LEN:
                    name = prefix
                    break
        name = _clean_group_name(name)
        if name and ids:
            groups.append({"name": name, "ids": ids})
    return groups or None

def _parse_group_response(raw: str) -> list[dict]:
    data = parse_llm_json(raw)
    if isinstance(data, dict):
        # 兼容模型偶发使用 themes / clusters / data 包装，避免 HTTP 200 但解析成空组。
        for key in ("groups", "themes", "clusters", "categories"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        for key in ("data", "result", "output"):
            nested = data.get(key)
            if isinstance(nested, dict):
                for group_key in ("groups", "themes", "clusters", "categories"):
                    value = nested.get(group_key)
                    if isinstance(value, list):
                        return [item for item in value if isinstance(item, dict)]
    # json_object 失效时模型常输出「分析过程 + JSON」混合文本,先抢救
    salvaged = _salvage_groups_json(raw)
    if salvaged:
        return salvaged
    # 输出被 max_tokens 截断(JSON 未闭合):抢救已完整写出的分组
    truncated = _salvage_truncated_groups(raw)
    if truncated:
        return truncated
    text_groups = _salvage_text_groups(raw)
    if text_groups:
        return text_groups
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    return []


def _build_groups(items: list[dict], valid_ids: set[str]) -> tuple[list[Group], set[str]]:
    covered: set[str] = set()
    groups: list[Group] = []
    names_seen: set[str] = set()
    for item in items:
        name = item.get("name") or item.get("theme")
        if not name:
            continue
        # 一文一主题:已被前面组认领的 lit_id 不再重复归属
        ids = [i for i in item.get("lit_ids", []) if i in valid_ids and i not in covered]
        if not ids:
            continue
        name = _clean_group_name(name)
        if not _group_name_acceptable(name):
            continue
        if name in names_seen:
            # 同名主题无法形成可读章节，保留首次出现的组，后续文献交给
            # 统一的孤儿文献归组逻辑；若因此组数不足，外层会启用兜底聚类。
            continue
        covered.update(ids)
        names_seen.add(name)
        groups.append(Group(name=name, lit_ids=ids))
    return groups, covered


def _min_groups(n_papers: int) -> int:
    """主题组数只设防退化下限，不预设领域必须拆成几类。

    九篇以上至少形成两个可比较板块；实际主题数由研究问题结构决定。
    """
    if n_papers >= 9:
        return 2
    return 1


def _desired_auto_groups(n_papers: int) -> int:
    """Return a scale-aware target for automatic theme discovery.

    This is a generic corpus-size rule, not a domain-specific topic count.
    Small pools can be summarized in two themes; larger pools need more
    independent sections so that hundreds of papers do not collapse into a
    single "other" bucket or two oversized chapters.
    """
    if n_papers <= 0:
        return 0
    if n_papers < 9:
        return 1
    if n_papers < 30:
        return 2
    if n_papers < 70:
        return 3
    if n_papers < 120:
        return 4
    return min(6, max(5, round(math.sqrt(n_papers / 80))))


def _title_similarity(a: str, b: str) -> float:
    """标题字符 bigram Jaccard 相似度(纯确定性,零 LLM 成本)。"""
    a, b = (a or "").strip(), (b or "").strip()
    if len(a) < 2 or len(b) < 2:
        return 1.0 if a == b else 0.0
    ga = {a[i : i + 2] for i in range(len(a) - 1)}
    gb = {b[i : i + 2] for i in range(len(b) - 1)}
    union = len(ga | gb)
    return len(ga & gb) / union if union else 0.0


def _reassign_orphans(papers: list[Paper], rest: list[str], groups: list[Group]) -> None:
    """无组文献按标题相似度挂到最相近的现有组。

    取代旧逻辑「全部追加进最后一组」—— 那会让最后一个主题变成大杂烩,
    破坏主题互斥性;相似度同分时偏向更大的组,保持规模均衡。
    """
    if not groups:
        return
    by_id = {p.lit_id: p for p in papers}
    for lid in rest:
        p = by_id.get(lid)
        if p is None:
            groups[-1].lit_ids.append(lid)  # 池外异常 id,保守保留不丢文献
            continue
        best = max(
            groups,
            key=lambda g: (
                max(
                    (
                        _title_similarity(p.title, by_id[m].title)
                        for m in g.lit_ids
                        if m in by_id
                    ),
                    default=0.0,
                ),
                len(g.lit_ids),
            ),
        )
        best.lit_ids.append(p.lit_id)


def _deterministic_fallback_groups(papers: list[Paper], topic: str) -> list[Group]:
    """LLM 不可用时的可写作兜底：按标题相似度稳定拆成多组。

    兜底的目标是保证流程可继续且一篇不丢，不冒充 LLM 的高质量语义分类。
    小池子保留单组；9 篇以上至少三组，30 篇以上至少四组。
    """
    if not papers:
        return []
    k = min(5, _desired_auto_groups(len(papers)))
    if k <= 1:
        return [Group(name=_clean_group_name(topic) or "研究综述",
                      lit_ids=[p.lit_id for p in papers])]
    # 选取分散的种子，再按标题 bigram 相似度归组；结果完全确定、无需额外服务。
    seeds = [papers[round(i * (len(papers) - 1) / (k - 1))] for i in range(k)]
    buckets: list[list[str]] = [[] for _ in range(k)]
    for paper in papers:
        idx = max(range(k), key=lambda i: _title_similarity(paper.title, seeds[i].title))
        buckets[idx].append(paper.lit_id)
    # 极端情况下某个桶为空，按顺序均衡分配，保证每组可写。
    for i, bucket in enumerate(buckets):
        if bucket:
            continue
        donor = max(range(k), key=lambda j: len(buckets[j]))
        if len(buckets[donor]) > 1:
            bucket.append(buckets[donor].pop())

    # 大池子写作时每个主题都需要足够证据，避免出现只有几篇文献的
    # 退化小组。这里仅做规模均衡，不按任何领域词汇改写主题边界。
    min_bucket_size = 10 if len(papers) >= k * 10 else max(2, len(papers) // k)
    for i, bucket in enumerate(buckets):
        while len(bucket) < min_bucket_size:
            donors = [j for j in range(k) if j != i and len(buckets[j]) > min_bucket_size]
            if not donors:
                break
            donor = max(donors, key=lambda j: len(buckets[j]))
            bucket.append(buckets[donor].pop())
    by_id = {p.lit_id: p for p in papers}
    groups = []
    used_names: set[str] = set()
    for i, bucket in enumerate(buckets):
        if not bucket:
            continue
        representative = next(
            (by_id[lid] for lid in bucket if _is_chinese_paper(by_id.get(lid))),
            by_id[bucket[0]],
        )
        # 兜底组名必须和 LLM 结果走同一套证据校验：优先从代表文献标题
        # 提取完整的问题短语，拒绝“标题前十个字”这种半截片段。
        label = _unique_evidence_group_name(
            papers, bucket, used_names, i + 1,
        )
        # 混合语言主题优先使用中文证据命名；不再用英文标题兜底覆盖中文章节。
        if not label or _group_name_conflicts(label, used_names) or not _group_name_acceptable(label):
            # 使用同一簇其他文献中的真实短语做语义消歧；绝不拼接“研究议题N”。
            for lid in bucket[1:]:
                candidate = _unique_evidence_group_name(papers, [lid], used_names, i + 1)
                if candidate and not _group_name_conflicts(candidate, used_names):
                    label = candidate
                    break
        if not label or _group_name_conflicts(label, used_names) or not _group_name_acceptable(label):
            # 标题高度重复时，从标题中的第二个自然短语取证（例如“需求约束”“
            # 风险控制”等）。这是文献证据，不是领域词典或编号占位符。
            rep_title = representative.title or ""
            phrases = [x for x in _re.findall(r"[\u4e00-\u9fff]{2,8}", rep_title)
                       if x not in _GENERIC_GROUP_NAMES]
            for phrase in phrases:
                candidate = _clean_group_name(phrase)
                if (_group_name_acceptable(candidate)
                        and not _group_name_conflicts(candidate, used_names)):
                    label = candidate
                    break
        if not label or _group_name_conflicts(label, used_names) or not _group_name_acceptable(label):
            # 无法从标题获得可靠差异时合并回已有主题，而不是产出伪标题。
            if groups:
                groups[0].lit_ids.extend(x for x in bucket if x not in groups[0].lit_ids)
            continue
        groups.append(Group(name=label, lit_ids=bucket))
        used_names.add(label)
    if not groups:
        # 极端标题缺失时只能使用用户给定的研究主题作为单一总主题；这比
        # 输出编号/占位章节更诚实，也不会写死任何领域词。
        label = _clean_group_name(topic)
        if _group_name_acceptable(label):
            return [Group(name=label, lit_ids=[p.lit_id for p in papers])]
    covered = {lid for g in groups for lid in g.lit_ids}
    _reassign_orphans(papers, [p.lit_id for p in papers if p.lit_id not in covered], groups)
    return groups


def _semantic_cluster_groups(papers: list[Paper], topic: str, progress=None) -> list[Group]:
    """本地摘要语义近似聚类：大文献池不再依赖逐块 LLM 编号输出。

    使用标题+摘要的中文二元词/英文词 TF-IDF 与确定性 k-means。它不是替代
    专业向量模型，而是一个无外部依赖、可在服务端稳定完成的第一阶段聚类器；
    LLM 只在最后为已经形成的簇命名，命名失败不会影响分组结果。
    """
    if not papers:
        return []
    n = len(papers)
    k = max(6, min(10, round(math.sqrt(n / 18)))) if n >= 120 else max(3, min(6, round(math.sqrt(n / 12))))
    k = min(k, n)

    def tokens(text: str) -> list[str]:
        text = (text or "").lower()
        words = _re.findall(r"[a-z][a-z0-9+.-]{1,}|[\u4e00-\u9fff]", text)
        chars = [w for w in words if len(w) == 1 and _re.match(r"[\u4e00-\u9fff]", w)]
        bigrams = [chars[i] + chars[i + 1] for i in range(len(chars) - 1)]
        return words + bigrams

    docs = [tokens(f"{p.title} {p.abstract or ''}") for p in papers]
    df: dict[str, int] = {}
    for doc in docs:
        for term in set(doc):
            df[term] = df.get(term, 0) + 1
    vocab = {term: i for i, (term, count) in enumerate(df.items()) if count >= 2}
    vectors: list[dict[int, float]] = []
    for doc in docs:
        counts: dict[str, int] = {}
        for term in doc:
            if term in vocab:
                counts[term] = counts.get(term, 0) + 1
        vec = {vocab[t]: (1.0 + math.log(c)) * math.log((n + 1) / (df[t] + 1)) for t, c in counts.items()}
        norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
        vectors.append({i: v / norm for i, v in vec.items()})

    def sim(a: dict[int, float], b: dict[int, float]) -> float:
        if len(a) > len(b):
            a, b = b, a
        return sum(v * b.get(i, 0.0) for i, v in a.items())

    # 确定性 farthest-first 种子，避免随机聚类导致同一批结果漂移。
    seeds = [0]
    while len(seeds) < k:
        nxt = max((i for i in range(n) if i not in seeds), key=lambda i: min(1 - sim(vectors[i], vectors[s]) for s in seeds))
        seeds.append(nxt)
    assignments = [-1] * n
    for _ in range(8):
        buckets = [[] for _ in range(k)]
        for i, vec in enumerate(vectors):
            idx = max(range(k), key=lambda j: sim(vec, vectors[seeds[j]]))
            assignments[i] = idx
            buckets[idx].append(i)
        changed = False
        for j, bucket in enumerate(buckets):
            if not bucket:
                continue
            medoid = max(bucket, key=lambda i: sum(sim(vectors[i], vectors[x]) for x in bucket))
            if medoid != seeds[j]:
                seeds[j] = medoid
                changed = True
        if not changed:
            break

    def local_name(indices: list[int], index: int) -> str:
        # 先从簇中心文献标题提取真实研究对象。它比把高频二元字机械拼成
        # “无人与人机与配送”更接近可读的学术主题，也完全不依赖领域词典。
        representative_title = papers[seeds[index]].title or papers[indices[0]].title or ""
        chinese_phrases = [
            _re.sub(r"^(基于|关于|面向)", "", phrase)
            for phrase in _re.findall(r"[\u4e00-\u9fff]{4,}", representative_title)
        ]
        chinese_phrases = [
            _re.sub(r"(研究|分析|探讨|综述)$", "", phrase).strip()
            for phrase in chinese_phrases
        ]
        chinese_phrases = [phrase for phrase in chinese_phrases if len(phrase) >= 4]
        if chinese_phrases:
            return max(chinese_phrases, key=len)[:_GROUP_NAME_MAX_LEN]

        title_words = [
            word for word in _re.findall(r"[A-Za-z][A-Za-z0-9-]{2,}", representative_title)
            if word.lower() not in _ENGLISH_STOPWORDS
        ]
        if len(title_words) >= 2:
            return " ".join(title_words[:4])[:48].strip()

        # 不能把高频二元词机械拼成“无人与人机与配送”一类伪标题。
        # 兜底名称必须优先来自簇中心真实标题；只有标题完全不可用时，
        # 才从摘要中提取少量连续实词并直接连接，不人为添加“与/和”。
        stop = _GENERIC_GROUP_NAMES | _ENGLISH_STOPWORDS
        freq: dict[str, int] = {}
        for i in indices:
            for term in docs[i]:
                if len(term) >= 2 and term not in stop and not _re.fullmatch(r"[与和及的之]", term):
                    freq[term] = freq.get(term, 0) + 1
        terms = [term for term, _ in sorted(freq.items(), key=lambda kv: (-kv[1], kv[0]))[:3]]
        chinese_terms = [term for term in terms if _re.search(r"[\u4e00-\u9fff]", term)]
        label = "".join(chinese_terms[:3]) if chinese_terms else " ".join(terms[:3])
        label = _re.sub(r"^[与和及的之]+|[与和及的之]+$", "", label)
        if not label or label.count("与") >= 2:
            title = papers[seeds[index]].title or papers[indices[0]].title or ""
            chinese = "".join(_re.findall(r"[\u4e00-\u9fff]", title))
            label = chinese[:12] if chinese else _re.sub(r"[\\/:：;；,.，。()（）\[\]【】]+", " ", title).strip()[:36]
        return label[:48].strip()

    groups = []
    for j in range(k):
        indices = [i for i, a in enumerate(assignments) if a == j]
        if indices:
            groups.append(Group(name=local_name(indices, j), lit_ids=[papers[i].lit_id for i in indices]))
    # 本地关键词可能生成重复组名；给重复簇使用代表标题中的有效词重命名，
    # 避免确认面板出现两个无法区分的“of与in与to”或同名主题。
    used_names: set[str] = set()
    by_lid = {p.lit_id: p for p in papers}
    for index, group in enumerate(groups, start=1):
        if group.name in used_names or not _group_name_acceptable(group.name):
            representative = by_lid.get(group.lit_ids[0], papers[0])
            chinese = "".join(_re.findall(r"[\u4e00-\u9fff]", representative.title or ""))
            words = [
                w for w in _re.findall(r"[A-Za-z][A-Za-z0-9-]{1,}", representative.title or "")
                if w.lower() not in _ENGLISH_STOPWORDS
            ]
            base = chinese[:8] if chinese else " ".join(words[:2])
            if not base:
                # 没有可提取标题词时不伪造领域名称；让上层明确拒绝该簇，
                # 由重新划分或人工确认处理，而不是展示“主题方向N”。
                continue
            group.name = (base[:9] + str(index)).strip()
        used_names.add(group.name)
    groups = [g for g in groups if g.lit_ids]
    if progress:
        progress({"kind": "local_cluster_done", "count": len(groups), "covered": n, "message": f"本地摘要聚类完成，共形成 {len(groups)} 个候选主题簇。"})

    # 一次性命名，不让命名模型参与归属决策。请求设置短超时：成功时得到
    # 中文问题导向标题，超时则立即保留上面从簇中心标题提取的证据名称，
    # 不能再为等待“美化标题”阻塞数分钟。
    if groups:
        try:
            if progress:
                progress({"kind": "naming_started", "count": len(groups), "message": "主题簇已形成，正在生成可读主题名称..."})
            catalog = "\n".join(f"#{i + 1} 代表标题：{papers[seeds[i]].title}；摘要：{(papers[seeds[i]].abstract or '')[:240]}" for i in range(len(groups)))
            raw = messages_create(
                system=(
                    "你是文献综述主题命名助手，只给已有主题簇命名，不重新分配文献。"
                    "命名必须概括该簇共同回答的研究问题或待解释对象，不能使用语言、数据库、期刊、作者名，"
                    "也不能把孤立方法名或摘要英文碎片当主题。只输出JSON。"
                ),
                user=(
                    f"研究主题：{topic}\n{catalog}\n"
                    f"输出：{{\"names\":[\"4-10字中文、问题导向的学术主题\"]}}，数量必须为{len(groups)}。"
                ),
                max_tokens=1200, temperature=0.2, response_format={"type": "json_object"},
                timeout=45.0, max_retries=1,
            )
            data = parse_llm_json(raw)
            names = data.get("names", []) if isinstance(data, dict) else []
            used_names = {g.name for g in groups}
            for group, name in zip(groups, names):
                clean = _clean_group_name(str(name))
                original = group.name
                used_names.discard(original)
                if _group_name_acceptable(clean) and clean not in used_names:
                    group.name = clean
                elif _group_name_acceptable(original) and original not in used_names:
                    group.name = original
                used_names.add(group.name)
        except Exception as exc:
            logger.warning("本地聚类主题命名失败，保留证据主题名: %s", exc)
    # 命名器可能少返回或返回重复名；最终再做一次全量收敛。
    used_names: set[str] = set()
    for index, group in enumerate(groups, start=1):
        if not _group_name_acceptable(group.name) or group.name in used_names:
            representative = by_lid.get(group.lit_ids[0], papers[0])
            evidence_name = local_name(
                [i for i, paper in enumerate(papers) if paper.lit_id in set(group.lit_ids)],
                min(index - 1, len(seeds) - 1),
            )
            if _group_name_acceptable(evidence_name) and evidence_name not in used_names:
                group.name = evidence_name
            else:
                raise ValueError(
                    f"第 {index} 个主题簇无法生成可靠名称，代表文献：{representative.title}"
                )
        used_names.add(group.name)
    return groups


def _output_token_budget(papers: list[Paper]) -> int:
    """分类输出的 max_tokens 预算,随池子规模缩放。

    历史教训:固定 5000 时,300+ 篇池子(每个 lit_id 是 lit_cnki_+16 位十六进制
    ≈ 10-18 token)的回显需求即达 4000-6000 token,JSON 必然被截断。
    编号协议下每篇仅 ~4 token,仍按池子规模放大并设 32k 硬顶
    (各 provider 输出上限不一,过大值会被 API 拒绝)。
    """
    return min(32000, max(5000, 1500 + 6 * len(papers)))


def _map_group_items(items: list[dict], index_of: dict[str, str]) -> list[dict]:
    """把模型输出的文献编号(int/str)映射回 lit_id。

    编号协议:模型只回显行首编号(1,2,3...),不抄 lit_id 原文 ——
    25 字符十六进制串 vs 1-2 位数字,输出体积差一个数量级。
    兼容模型仍按旧格式回 lit_id 的情形(原样保留,由 valid_ids 过滤)。
    """
    mapped: list[dict] = []
    for item in items:
        raw_ids: list = item.get("ids") or item.get("lit_ids") or []
        out: list[str] = []
        for v in raw_ids:
            key = str(v).strip().lstrip("#")
            lid = index_of.get(key)
            if lid:
                out.append(lid)
            elif isinstance(v, str) and v.startswith("lit_"):
                out.append(v)
        mapped.append({**item, "lit_ids": out})
    return mapped


def _merge_chunk_groups(chunk_groups: list[list[Group]], papers: list[Paper], topic: str, progress=None) -> list[Group]:
    """合并分块结果；同名主题合并，近似主题按标题相似度归并，保证全量覆盖。"""
    merged: list[Group] = []
    by_id = {p.lit_id: p for p in papers}
    for groups in chunk_groups:
        for group in groups:
            target = next((g for g in merged if g.name == group.name), None)
            if target is None:
                target = Group(name=group.name, lit_ids=[])
                merged.append(target)
            for lid in group.lit_ids:
                if lid not in target.lit_ids:
                    target.lit_ids.append(lid)
    if progress:
        progress({
            "kind": "merge_candidates",
            "count": len(merged),
            "themes": [g.name for g in merged[:20]],
            "message": f"各文献块共形成 {len(merged)} 个候选主题，正在进行二级主题归并...",
        })

    # 二级聚类只传候选主题及少量代表标题，输入远小于 540 篇全文；让模型按语义
    # 归并跨块同类主题，而不是按组名字符相似度硬压成几个大桶。
    if len(merged) > 12:
        catalog_lines = []
        for index, group in enumerate(merged, start=1):
            titles = [by_id[lid].title for lid in group.lit_ids[:3] if lid in by_id]
            catalog_lines.append(f"#{index} {group.name} | 代表文献:{'；'.join(titles)}")
        try:
            raw = messages_create(
                system=(
                    "你是文献主题聚类助手。将候选主题按真实研究内容归并为6-10个并列的学术主题。"
                    "不要使用机制、方法路径、研究分析、其他等空泛名称。只输出JSON。"
                ),
                user=(
                    f"研究主题：{topic}\n候选主题如下：\n" + "\n".join(catalog_lines)
                    + '\n只输出格式：{"groups":[{"name":"主题名","ids":[1,2]}]}。'
                    "每个候选编号必须且只能出现一次。"
                ),
                max_tokens=6000,
                temperature=0.2,
                response_format={"type": "json_object"},
            )
            meta_items = _parse_group_response(raw)
            consolidated: list[Group] = []
            used: set[int] = set()
            for item in meta_items:
                name = _clean_group_name(str(item.get("name") or item.get("theme") or ""))
                ids = item.get("ids") or item.get("lit_ids") or []
                indexes = []
                for value in ids:
                    try:
                        idx = int(str(value).lstrip("#")) - 1
                    except ValueError:
                        continue
                    if 0 <= idx < len(merged) and idx not in used:
                        indexes.append(idx)
                        used.add(idx)
                if name and indexes and _group_name_acceptable(name):
                    lit_ids = []
                    for idx in indexes:
                        lit_ids.extend(lid for lid in merged[idx].lit_ids if lid not in lit_ids)
                    consolidated.append(Group(name=name, lit_ids=lit_ids))
            if 6 <= len(consolidated) <= 12 and len(used) == len(merged):
                merged = consolidated
                if progress:
                    progress({"kind": "merge_done", "count": len(merged), "themes": [g.name for g in merged]})
        except Exception as exc:
            logger.warning("二级主题归并失败，保留确定性候选主题: %s", exc)
    # 主题过多时只合并“明显相近”的主题，并设置硬上限，避免全量两两
    # 贪心合并在异常输出下退化为近似立方复杂度、长时间卡在合并阶段。
    target_max = min(12, max(5, _desired_auto_groups(len(papers)) + 2))
    if len(merged) > target_max:
        merged.sort(key=lambda g: len(g.lit_ids), reverse=True)
        kept = merged[:target_max]
        for extra in merged[target_max:]:
            best = max(
                kept,
                key=lambda g: (_title_similarity(extra.name, g.name), len(g.lit_ids)),
            )
            best.lit_ids.extend(x for x in extra.lit_ids if x not in best.lit_ids)
        merged = kept
    covered = {lid for g in merged for lid in g.lit_ids}
    rest = [p.lit_id for p in papers if p.lit_id not in covered]
    _reassign_orphans(papers, rest, merged)
    _rebalance_language_groups(merged, papers)
    return merged or _deterministic_fallback_groups(papers, topic)


def classify_by_theme(papers: list[Paper], topic: str, *, _allow_chunking: bool = True, progress=None) -> list[Group]:
    """主题分类:LLM 将文献归入若干主题。

    强约束(代码层兜底):
      - 九篇以上至少形成两个可比较主题，不硬编码固定主题数量;
      - 三次调用逐级升级:常规(0.3) → 显式返工(0.7) → 极简输入(0.7);
      - 输出用「编号协议」(模型只回显行首编号,不抄 lit_id),大池子不再截断;
      - 输出万一仍被截断,抢救已完整的分组,缺组文献按标题相似度归入;
      - 组名不合格的组剔除,其文献按标题相似度归入最相近的合格组;
      - LLM 彻底失败时,单组全量确定性兜底(组名 = 清洗后的主题名)。
    """
    if not papers:
        return []

    # 超大池仅作防御性本地聚类；正常写作链会先筛选到至多 100 篇，再让
    # 模型基于中英文标题与摘要统一判定研究问题，避免本地词频按语言分簇。
    if _allow_chunking and len(papers) >= 500:
        if progress:
            progress({"kind": "local_cluster_started", "count": len(papers), "message": f"正在对 {len(papers)} 篇文献的标题与摘要做本地语义聚类..."})
        return _semantic_cluster_groups(papers, topic, progress=progress)

    # 产品级分块：避免 80+ 篇文献一次性进入上下文。每块独立调用/重试，
    # 单块失败时仍可由本地兜底完成，最后统一合并并校验覆盖。
    chunk_size = 50
    if _allow_chunking and len(papers) > 120:
        chunk_count = max(1, math.ceil(len(papers) / chunk_size))
        chunks = [[] for _ in range(chunk_count)]
        # 交错分配语言属性，避免前几块全中文、后几块全英文；语言不参与主题命名。
        for bucket in ([p for p in papers if _is_chinese_paper(p)], [p for p in papers if not _is_chinese_paper(p)]):
            for i, paper in enumerate(bucket):
                chunks[i % chunk_count].append(paper)
        chunks = [c for c in chunks if c]
        chunk_groups = []
        for index, chunk in enumerate(chunks, start=1):
            if progress:
                progress({"kind": "chunk_started", "chunk": index, "total_chunks": len(chunks), "count": len(chunk)})
            def chunk_progress(data, *, _index=index):
                if progress:
                    progress({**data, "chunk": _index, "total_chunks": len(chunks)})
            result = classify_by_theme(chunk, topic, _allow_chunking=False, progress=chunk_progress)
            chunk_groups.append(result)
            if progress:
                progress({
                    "kind": "chunk_done",
                    "chunk": index,
                    "total_chunks": len(chunks),
                    "count": len(chunk),
                    "groups": len(result),
                    "covered": len({lid for group in result for lid in group.lit_ids}),
                    "themes": [group.name for group in result],
                })
        if progress:
            progress({"kind": "merge_started", "total_chunks": len(chunks), "message": "正在合并各文献块的主题结果..."})
        return _merge_chunk_groups(chunk_groups, papers, topic, progress=progress)

    # 分组依据:标题 + 摘要。同主题文献标题高度相似,
    # 区分「方法流派/应用情境/机制要素」的信息主要在摘要里。
    # 行首给短编号,模型输出只回显编号 —— 大池子时输出体积可控。
    index_of = {str(i + 1): p.lit_id for i, p in enumerate(papers)}
    # 主题聚类必须依据摘要证据；大池子仅缩短摘要片段，避免上下文过大。
    title_only = False
    abstract_limit = 600 if len(papers) >= 70 else 1200
    catalog = "\n".join(
        (
            f"- #{i + 1} | {p.title} | {p.year or 'N/A'}"
            if title_only
            else f"- #{i + 1} | {p.title} | {p.year or 'N/A'} | 摘要:{(p.abstract or '').strip()[:abstract_limit]}..."
        )
        for i, p in enumerate(papers)
    )
    valid_ids = {p.lit_id for p in papers}
    min_groups = _desired_auto_groups(len(papers))
    token_budget = _output_token_budget(papers)

    def _ask(attempt: int) -> list[Group]:
        """attempt 1=常规;2=显式返工(升温+强制组数);3=极简输入兜底。"""
        # 只提正向要求,不列举反面例子 —— 负面清单会诱导 LLM 联想到坏组名;
        # 组名不合格由代码校验链(_group_name_acceptable)兜底,无需 prompt 层禁令。
        topic_prefix_reminder = (
            f"\n\n## 组名要求\n"
            f"每个组名都要像学术综述的章节标题:简洁的中文学术名词短语(4-10 字),"
            f"围绕主题「{topic}」,优先概括本组共同回答的研究问题或待解释对象。"
            "方法、语言、数据库、期刊不能单独构成一级主题；同组文献应能比较继承、补充、冲突或演进关系。"
        )
        # 输出纪律:MiniMax 偶发把「逐篇分析过程」当正文输出,
        # 上限给小了 JSON 还没写就被截断 → 解析失败。
        # 低温 + 提高上限 + 硬指令三管齐下压制。
        output_discipline = (
            "\n\n## 输出纪律(最高优先级)\n"
            "禁止输出分析过程、逐篇归属说明、Markdown 列表或任何解释文字;"
            "ids 里只写文献清单行首的编号数字,严禁抄写 lit_id 原文;"
            "你的全部输出必须是一个以 { 开头、以 } 结尾的 JSON 对象,格式:\n"
            '{"groups": [{"name": "<组名>", "ids": [1, 2, 3]}]}'
        )
        if attempt >= 3:
            # 第三道防线:缩短摘要 + 一句话指令，仍然保留摘要证据。
            catalog_min = "\n".join(
                f"#{i + 1} {p.title[:60]} 摘要:{(p.abstract or '').strip()[:300]}"
                for i, p in enumerate(papers)
            )
            system = (
                "你是文献计量助手。把文献按摘要中的实际研究主题分成 2-5 组。"
                "只输出 JSON 对象,不输出任何其他文字。"
            )
            user = (
                f'主题「{topic}」的文献如下(每行:编号、标题、摘要)。'
                f"按摘要中的实际研究主题分为 {min_groups}-5 组,组名为 4-10 字中文学术名词短语,"
                '每篇文献恰属一组,ids 只写行首编号数字。只输出:\n'
                '{"groups": [{"name": "<组名>", "ids": [1, 2, 3]}]}'
                f"\n\n{catalog_min}"
            )
        else:
            if attempt >= 2:
                # 返工要求只在重试时出现 —— 首调就指责模型犯错会污染输出。
                rework = (
                    f"\n\n## 返工要求(第 {attempt} 次)\n"
                    f"上一次的分组不满足「至少 {min_groups} 组」的硬要求。"
                    "请按研究问题/方法、应用情境、机制要素等维度拆出并列子主题,"
                    f"共输出 {min_groups}-5 组,每组一个独立的章节式组名。\n"
                ) + topic_prefix_reminder
            else:
                rework = topic_prefix_reminder
            system = render(
                "literature-review:classify",
                topic=topic,
                classify_mode="theme",
                papers_catalog=catalog,
            )
            # catalog 只在 system 出现一次;user 只带增量指令,避免清单双份发送
            user = f"研究主题:{topic}{rework}{output_discipline}"
        try:
            if progress:
                progress({"kind": "llm_attempt", "attempt": attempt, "count": len(papers)})
            raw = messages_create(
                system=system, user=user, max_tokens=token_budget,
                temperature=0.3 if attempt == 1 else 0.7,
                response_format={"type": "json_object"},
                timeout=75.0, max_retries=1,
            )
            items = _parse_group_response(raw)
            logger.info(
                "主题分类响应已解析(attempt=%d, papers=%d, groups=%d, names=%s)",
                attempt, len(papers), len(items),
                [str(item.get("name") or item.get("theme") or "")[:20] for item in items[:8]],
            )
            if not items:
                logger.warning("主题分类返回空分组，原文预览=%s", str(raw).replace("\n", " ")[:500])
        except Exception as exc:
            logger.warning(
                "主题分类 LLM 调用失败(attempt=%d, papers=%d, title_only=%s, input_chars=%d): %s",
                attempt, len(papers), title_only, len(system) + len(user), exc,
            )
            return []
        items = _map_group_items(items, index_of)
        groups, _ = _build_groups(items, valid_ids)
        if progress:
            progress({
                "kind": "llm_result",
                "attempt": attempt,
                "papers": len(papers),
                "parsed_groups": len(items),
                "valid_groups": len(groups),
                "covered": len({lid for group in groups for lid in group.lit_ids}),
                "themes": [group.name for group in groups],
            })
        return groups

    groups = _ask(1)
    if len(groups) < min_groups or not _groups_language_balanced(groups, papers):
        groups = _ask(2)
    if (len(groups) < min_groups or not _groups_language_balanced(groups, papers)) and len(papers) > 120:
        logger.warning("主题分类前两次调用未达标,启用极简输入第三次重试")
        groups = _ask(3)
    # 组名不合格的组剔除(其文献由 _reassign_orphans 回收,不丢文献)
    groups = [g for g in groups if _group_name_acceptable(g.name)] if groups else []
    if not groups or len(groups) < min_groups or not _groups_language_balanced(groups, papers):
        # 80 篇以下仍保留“模型失败即要求重划分”的严格交互；接近正式综述
        # 规模的大池子不能因一次模型格式抖动卡死，改用确定性证据兜底。
        if _allow_chunking and len(papers) < 90:
            raise ValueError(
                "主题分类模型连续返回无效或不完整结果，系统已停止生成，"
                "不会再用截断标题或编号主题冒充分类结果。请重新划分主题（按语言分裂或主题不足）。"
            )
        # LLM 失败不能把用户卡在“划分主题中”。立即使用可追溯的
        # 标题相似度兜底，并在进度事件中明确标记为 fallback；正文前仍由
        # 用户确认主题归属，符合“划分→确认→写作”的两阶段架构。
        logger.warning(
            "主题分类三次调用均未产出有效分组(输入 %d 篇),启用确定性多组兜底",
            len(papers),
        )
        fallback = _deterministic_fallback_groups(papers, topic)
        if progress:
            progress({
                "kind": "fallback_result",
                "papers": len(papers),
                "groups": len(fallback),
                "covered": len({lid for group in fallback for lid in group.lit_ids}),
                "themes": [group.name for group in fallback],
            })
        return _finalize_theme_groups(papers, fallback, topic)

    # 无组文献按标题相似度归入最相近的组(避免漏 paper,不再倒进最后一组)
    covered = {lid for g in groups for lid in g.lit_ids}
    rest = [p.lit_id for p in papers if p.lit_id not in covered]
    _reassign_orphans(papers, rest, groups)
    return _finalize_theme_groups(papers, groups, topic)


def classify(papers: list[Paper], topic: str, mode: str, progress=None) -> list[Group]:
    if mode == "locale":
        return classify_by_locale(papers)
    if mode == "theme":
        return classify_by_theme(papers, topic, progress=progress)
    raise ValueError(f"unknown classify mode: {mode}")
