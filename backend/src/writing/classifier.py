"""综述分类器:按国内外 / 按主题 对文献分组。

使用 prompts/literature-review.md 模板中的 `classify` 段作为 system prompt。
"""
from __future__ import annotations

import json as _json
import logging
import math
import re as _re
import time
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
    research_question: str | None = None


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
    # 模型照抄 prompt/格式示例的占位名("主题名"/"<组名>"/"示例主题"等):
    # 整词拒绝,交给证据重命名链路,不能让占位词出现在确认面板。
    r"|^[<【(\"“‘']?(?:中文|示例|学术|新)?(?:主题名称|主题名|组名|标题名|主题|分组)[>】)\"”'']?$"
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
# 组名长度上限:classify-plan 的口径是 3-12 字;代码级兜底取 16 ——
# 既有回归用例里有 13/16 字合法名(如"突发公共卫生事件对水产行业的影响"),
# 而线上泄漏的标题中段碎片是 19/20 字
# ("背景下福清沙浦镇海产品内容营销策略研究"),16 恰好拦住后者。
_GROUP_NAME_MAX_LEN = 16
# 标题切头残渣:主题名以这些接缝字开头,说明是从某篇标题中段切出来的
# 碎片而非综合命名(线上案例:"的新疆昌吉市鲜活水产品的营销渠道优化研究")。
_TITLE_FRAGMENT_SEAM_RE = _re.compile(r"^(?:的|了|之)")
# 标题修饰词:名字与标题的残余差若只含这些词,属于"去修饰的泛化改写"
# (回归用例:"我国水产品出口企业国际营销策略研究"→"水产品出口企业国际
# 营销策略"),不算标题复制;残余含内容词(如"乡村振兴""沙浦镇")才是碎片。
_TITLE_MODIFIER_TOKENS = (
    "我国", "中国", "国内", "国外", "背景下", "情境下", "条件下", "环境下",
    "研究", "分析", "探讨", "综述", "述评", "对策", "启示", "现状", "进展",
)
# 两遍式阈值:达到该规模的主题池走「全局定轴 → 固定主题归组」两遍式,
# 小池子单次调用一步完成定轴+归组。模块级常量便于测试覆盖默认行为。
_CHUNK_THRESHOLD = 30
# 拒绝“与/和/及/以及”这类并列连接词。两侧均为内容词时说明 LLM 把两
# 个研究主题合并了，应当重划。单侧为词中字符(如“载人与”/“需求和”)也
# 会让标题读起来像“未定主语”的堆叠短语，一律拒绝。
_COORDINATING_GROUP_RE = _re.compile(r"[与和及]|(?:以及)")


def _group_name_acceptable(
    name: str,
    *,
    pool_language: str | None = None,
) -> bool:
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
    # 叙述性短语没有信息量,只能用作正文过渡,严禁作为章节标题
    if name in _NARRATIVE_GROUP_NAMES:
        return False
    if _TITLE_LEAD_RE.match(name):
        return False
    # 斜杠、竖线连接的对象词是关键词清单，不是研究问题导向主题。
    if _SLASH_CHAIN_RE.search(name):
        return False
    if _PLACEHOLDER_GROUP_RE.fullmatch(name):
        return False
    if _COORDINATING_GROUP_RE.search(name):
        return False
    if len(name) > 2 and _TRUNCATED_GROUP_SUFFIX_RE.search(name):
        return False
    # 个案研究尾缀碎片("以乳山市为例")是论文标题的副标题片段,
    # 不是研究主题;整词拒绝,交给证据重命名。
    if _re.fullmatch(r"以.{1,16}为例", name):
        return False
    # 伪装的兜底桶:"特定XX综合研究/XX概述/XX总结"本质是"其他"组,
    # 说明模型没有给出可比较的主题边界,一律拒绝。
    if _re.search(r"(?:综合|概述|概览|总结)(?:研究|分析|评述|进展)?$", name):
        return False
    chinese_chars = _re.findall(r"[\u4e00-\u9fff]", name)
    latin_words = [w.lower() for w in _re.findall(r"[A-Za-z][A-Za-z0-9-]*", name)]
    if _LATIN_ONLY_GROUP_RE.fullmatch(name):
        # 英文文献簇允许使用包含至少两个实质词的英文主题名；
        # 纯英文功能词/单词碎片仍然拒绝，避免出现 such/data/future 一类伪标题。
        meaningful = [w for w in latin_words if w not in _ENGLISH_STOPWORDS]
        if len(name) > 80 or not meaningful:
            return False
        # 中文主题池下，纯英文通用话术("E-commerce marketing strategies"
        # 这种) 不能作为一级主题 — 它反映的是英文营销通用词而不是
        # 本组文献的具体研究问题。
        # 中文主题池:拒绝纯英文通用词短语(没有中文字符且没有学术缩写)。
        # 判定方式:整段字符串不含中文字符且没有 B2B/AI/IoT 这类缩写就被拒;
        # 真正的中文学术主题(包括短中英混合)都会被保留。
        # 注意:不再按 60 字符长度放行 —— 线上案例里 62/75 字符的英文长名
        # 正是从这个口径漏进中文综述确认面板的。
        if pool_language == "zh":
            has_chinese = any("\u4e00" <= ch <= "\u9fff" for ch in name)
            if not has_chinese:
                # 缩写豁免必须是白名单内的学术缩写(AI/B2B/ERP...),而不是
                # "含任意全大写词"——旧正则下英文标题里的普通大写词(COVID)
                # 会让英文标题碎片名在中文池直接放行。
                tokens = _re.findall(r"[A-Za-z][A-Za-z0-9-]*", name)
                if not any(tok.upper() in _EN_ACRONYM_ALLOWLIST for tok in tokens):
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
    """轻度清洗:剥掉前缀编号("1." / "一、"/ "(1)")、markdown 加粗残渣
    ("**主题**")和尾部"...相关研究"。

    不改变语义,只把 LLM 偶发的修饰性前缀/后缀去掉,得到学术名词短语。
    """
    name = (name or "").strip()
    # markdown 强调残渣:LLM 返回 "**主题名**" 时,星号会一路泄漏进
    # 章节标题与正文模板句(线上案例:"一、突发冲击下海鲜系统的韧性**")
    name = _re.sub(r"[*_`]+", "", name).strip()
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


def _title_residual_is_modifier_only(title: str, name: str) -> bool:
    """名字是标题子串时,判断未被覆盖的残余是否只含修饰词(泛化改写)。"""
    idx = title.find(name)
    if idx < 0:
        return False
    residual = title[:idx] + title[idx + len(name):]
    for token in sorted(_TITLE_MODIFIER_TOKENS, key=len, reverse=True):
        residual = residual.replace(token, "")
    residual = _TITLE_LEAD_RE.sub("", residual)
    return len(residual) < 2


def _looks_like_title_prefix(name: str, papers: list[Paper], lit_ids: list[str]) -> bool:
    """拒绝模型把论文标题(整段/去头中段)直接当作主题名。

    两层判定:
    - 前缀/连续子串:名字是组内某篇标题的连续片段。前缀(≥6 字)与中段
      子串(≥12 字,降低合成名偶然命中的误伤)分别设阈值 —— 此前只查
      startswith,"乡村振兴背景下福清…"砍头成"背景下福清…"即可逃逸。
    - 切头残渣:名字以"的/了/之"开头,必然是标题接缝碎片。
    """
    compact_name = _re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", name or "")
    if len(compact_name) < 6:
        return False
    if _TITLE_FRAGMENT_SEAM_RE.match((name or "").strip()):
        return True
    by_id = {p.lit_id: p for p in papers}
    allow_substring = len(compact_name) >= 12
    for lit_id in lit_ids:
        title = _re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", by_id.get(lit_id).title if by_id.get(lit_id) else "")
        if not title or len(title) < len(compact_name):
            continue
        if title.startswith(compact_name):
            return True
        if (
            allow_substring
            and compact_name in title
            and not _title_residual_is_modifier_only(title, compact_name)
        ):
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


def _best_title_phrase(title: str, pool_language: str | None = None) -> str:
    """从单篇标题提取最长合格中文主题短语(降级命名的核心提取器)。

    规则(2026-10-15 两起线上短碎片名事故后重写):
    - 短语门槛 6 字:4-5 字的口号/半截词("世界都在吃")不作为主题名原料;
    - 标题超过长度上限时才按从句切分,且取信息量更大的半边 ——
      此前无条件取标记词前缀,"产业链视角下福建泉州海洋渔业产业现代化
      发展研究"被切成"产业链视角"(视角残片),而对象词"福建泉州海洋渔业
      产业现代化发展"在标记之后;
    - 切出的半边剥掉学术尾缀(研究/分析/探讨/综述/述评),避免超长触发
      标点收束而整个丢弃。
    """
    best = ""
    for phrase in _re.findall(r"[\u4e00-\u9fff]{6,}", title or ""):
        candidate = phrase
        for lead in _TITLE_LEAD_WORDS:
            candidate = _re.sub(rf"^{lead}", "", candidate)
        if len(candidate) > 12:
            for marker in _TITLE_CLAUSE_MARKERS:
                if marker in candidate:
                    prefix, _, after = candidate.partition(marker)
                    sides = []
                    for part in (prefix, after):
                        part = _re.sub(r"(研究|分析|探讨|综述|述评)$", "", part).strip()
                        if len(part) >= 4:
                            sides.append(part)
                    if sides:
                        candidate = max(sides, key=len)
                    break
        while len(candidate) > 12 and candidate[-1] in "与和及的等之其路":
            candidate = candidate[:-1]
        if len(candidate) > _GROUP_NAME_MAX_LEN:
            pieces = [p.strip() for p in _re.split(r"[：:，,；;。.!！?？]", candidate) if p.strip()]
            candidate = max(pieces, key=len) if pieces else ""
        if (
            len(candidate) >= 6
            and not _TITLE_FRAGMENT_SEAM_RE.match(candidate)
            and _group_name_acceptable(candidate, pool_language=pool_language)
        ):
            if len(candidate) > len(best):
                best = candidate
    return best[:_GROUP_NAME_MAX_LEN]


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
    return _best_title_phrase(title, _pool_dominant_language(papers))
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
            and _group_name_acceptable(candidate, pool_language=_pool_dominant_language(papers))
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
        _pool_lang = _pool_dominant_language(papers)
        if (
            not _group_name_acceptable(name, pool_language=_pool_lang)
            or _looks_like_title_prefix(name, papers, ids)
            or _group_name_conflicts(name, seen_names)
        ):
            name = _unique_evidence_group_name(
                papers, ids, seen_names, len(normalized) + 1,
            )
        if (
            not name
            or not _group_name_acceptable(name, pool_language=_pool_lang)
            or _group_name_conflicts(name, seen_names)
        ):
            continue
        normalized.append(
            Group(name=name, lit_ids=ids, research_question=group.research_question)
        )
        seen_ids.update(ids)
        seen_names.add(name)

    if not normalized:
        raise ValueError("主题结果没有形成可验证的研究问题边界，请重新划分")

    # 中文池的纯英文桶必须持中文组名(展示层硬约束),再回收孤儿文献。
    _rename_english_groups_for_zh_pool(papers, normalized, topic)

    # 漏分文献按标题相似度回收到最接近的主题，保证分类覆盖全集。
    _reassign_orphans(papers, [p.lit_id for p in papers if p.lit_id not in seen_ids], normalized)

    normalized = [g for g in normalized if g.lit_ids]
    return _balance_oversize_groups(papers, normalized, topic)


def _groups_acceptable(groups: list[Group], pool_language: str | None = None) -> bool:
    """LLM 分组结果是否全部可接受(数量达标 + 组名合格)。"""
    return bool(groups) and all(
        _group_name_acceptable(g.name, pool_language=pool_language) for g in groups
    )


def _zh_theme_name_too_short(name: str, pool_language: str | None) -> bool:
    """用户可见的中文主题名是否短于 6 字(策略级,非底层格式校验)。

    4-5 字的名称往往是产品名/口号/视角残片,信息量不足以概括一个主题
    (线上案例:"高邮大虾"名下 24 篇)。仅在面向用户的策略点调用
    (轴规划 normalize / finalize / retitle / 面板终检),内部占位名
    不受影响。
    """
    if pool_language == "en":
        return False
    chinese = [ch for ch in (name or "") if "一" <= ch <= "鿿"]
    return 0 < len(chinese) < 6


def _zh_label_for_english_group(topic: str, used_names: set[str]) -> str:
    """中文池里纯英文主题组的确定性中文兜底名。

    语言不是主题,但英文证据无法确定性译成中文学术短语;此时用研究主题
    派生「XX国际研究」式诚实标签(该组内容就是主题的英文文献),绝不让
    英文标题碎片直接出现在中文综述的确认面板
    (线上案例:主题 3 = "Sea Table Role Traceability")。
    """
    base = (_clean_group_name(topic or "") or "研究主题")[:8]
    for candidate in (
        f"{base}国际研究",
        f"{base}国际研究进展",
        f"{base}英文文献研究",
    ):
        if candidate not in used_names and _group_name_acceptable(
            candidate, pool_language="zh",
        ):
            return candidate
    for idx in range(2, 10):
        candidate = f"{base}国际研究{idx}"
        if candidate not in used_names and _group_name_acceptable(
            candidate, pool_language="zh",
        ):
            return candidate
    return ""


def _rename_english_groups_for_zh_pool(
    papers: list[Paper], groups: list[Group], topic: str,
) -> None:
    """zh 主导池中,纯英文文献组的组名必须是中文(就地修改)。

    英文桶本身允许存在(该主题确实只有单一语言证据),但章节名是中文
    综述的展示层信息;组内没有任何中文证据、且当前名字不含中文时,
    替换为主题派生的中文标签。
    """
    if _pool_dominant_language(papers) != "zh":
        return
    by_id = {p.lit_id: p for p in papers}
    used = {g.name for g in groups}
    for group in groups:
        has_chinese = any(
            lid in by_id and _is_chinese_paper(by_id[lid]) for lid in group.lit_ids
        )
        if has_chinese or _re.search(r"[一-鿿]", group.name or ""):
            continue
        label = _zh_label_for_english_group(topic, used - {group.name})
        if label:
            logger.warning(
                "中文池纯英文主题组「%s」更名为「%s」(组内 %d 篇均为英文文献)",
                group.name, label, len(group.lit_ids),
            )
            used.discard(group.name)
            group.name = label
            used.add(label)


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
    """从「分析文字 + JSON」混合输出中抢救 groups/themes 数组。

    MiniMax 偶发无视 json_object 约束,先写大段分析过程再给 JSON;
    parse_llm_json 只认「整段是合法 JSON」,这里做括号配对二次抢救。
    返回 None 表示全文确实没有可解析的 {"groups": [...]} 结构(如被截断)。
    """
    marker = -1
    for head in ('{"groups"', '{ "groups"', '{"themes"', '{ "themes"'):
        marker = raw.find(head)
        if marker != -1:
            break
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
                groups = None
                if isinstance(obj, dict):
                    for key in ("groups", "themes", "clusters", "categories"):
                        value = obj.get(key)
                        if isinstance(value, list):
                            groups = value
                            break
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
    themes_marker = raw.find('"themes"')
    if themes_marker != -1 and (marker == -1 or themes_marker < marker):
        marker = themes_marker
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


def _repair_invalid_group_response(raw: str, *, topic: str, papers: list[Paper],
                                   progress=None) -> list[dict]:
    """将模型已明确给出的 Markdown/自然语言分组修复为结构化 JSON。

    这里只做格式转换，不重新设计主题，也不允许模型补造文献编号；
    这样可以把协议违约从“0 主题”转换成可审计的修复步骤。
    """
    text = str(raw or "").strip()
    if not text:
        return []
    catalog = "\n".join(f"#{i + 1} {p.title}" for i, p in enumerate(papers))
    system = (
        "你是结构化输出修复器。把给定的文献主题分组说明转换成 JSON。"
        "只能保留原文明确出现的主题名称和编号，不能新增、改写或猜测主题，"
        "ids 只能使用文献目录中的编号。只输出 JSON，不要解释。"
    )
    user = (
        f"研究主题：{topic}\n文献目录：\n{catalog}\n\n"
        f"原始分组结果：\n{text[:12000]}\n\n"
        '输出格式：{"groups":[{"name":"<主题名>","ids":[1,2,3]}]}；'
        "只输出 JSON。name 必须是 3-12 字、概括该组文献的具体中文名词短语,"
        "严禁照抄示例里的占位文字。"
    )
    if progress:
        progress({"kind": "format_repair_started", "message": "正在修复主题分类输出格式..."})
    import threading as _thr_rep
    _hb_stop = _thr_rep.Event()
    _heartbeat_progress(
        progress,
        {"papers": len(papers), "stage": "format_repair"},
        _hb_stop, interval=5.0,
    )
    try:
        try:
            repaired = messages_create(
                system=system, user=user, max_tokens=min(1800, max(500, len(papers) * 12)),
                temperature=0.0, response_format={"type": "json_object"},
                timeout=20.0, max_retries=0,
            )
        finally:
            _hb_stop.set()
        items = _parse_group_response(repaired)
        if progress:
            progress({"kind": "format_repair_done", "groups": len(items)})
        return items
    except Exception as exc:
        logger.warning("主题分类格式修复失败: %s", exc)
        if progress:
            progress({"kind": "format_repair_failed", "message": str(exc)[:300]})
        return []


def _build_groups(
    items: list[dict], valid_ids: set[str], pool_language: str | None = None,
) -> tuple[list[Group], set[str]]:

    for item in items:
        item.setdefault("research_question", None)
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
        if not _group_name_acceptable(name, pool_language=pool_language):
            continue
        if name in names_seen:
            # 同名主题无法形成可读章节，保留首次出现的组，后续文献交给
            # 统一的孤儿文献归组逻辑；若因此组数不足，外层会启用兜底聚类。
            continue
        covered.update(ids)
        names_seen.add(name)
        groups.append(Group(name=name, lit_ids=ids, research_question=item.get("research_question")))
    return groups, covered


def _pool_dominant_language(papers) -> str:
    """Sniff whether a paper pool is Chinese-dominant.

    Uses the Chinese-character density inside titles (and falls
    back to source if titles are empty). We deliberately do NOT
    also weight abstracts because chunk prompts may have
    truncated them.
    """
    if not papers:
        return "en"
    cn_chars = 0
    total_chars = 0
    cn_source = sum(
        1 for paper in papers
        if getattr(paper, "source", "") in (Source.CNKI, Source.USER_IMPORTED)
    )
    for paper in papers:
        text = (getattr(paper, "title", "") or "").strip()
        if not text:
            continue
        total_chars += len(text)
        cn_chars += sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    cn_share = (cn_chars / total_chars) if total_chars else 0
    src_share = cn_source / len(papers)
    combined = 0.6 * cn_share + 0.4 * src_share
    return "zh" if combined >= 0.4 else "en"


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


def _split_group_balanced(
    papers: list[Paper],
    group: Group,
    cap: int,
    topic: str,
    used_names: set[str],
    depth: int = 0,
) -> list[Group]:
    """把单个超大组按组内标题相似度分散种子拆成 ≤cap 的均衡子组(确定性,零 LLM)。

    相似度不可分(并列/全零)时按序号轮转,保证子组规模均衡且不丢文献;
    子组名优先取组内真实标题证据,取不到时用「父组名·第N部分」消歧。
    """
    by_id = {p.lit_id: p for p in papers}
    members = [by_id[lid] for lid in group.lit_ids if lid in by_id]
    k = math.ceil(len(members) / cap)
    if k <= 1 or depth >= 2 or len(members) < 2:
        return [group]
    seeds = [members[round(i * (len(members) - 1) / (k - 1))] for i in range(k)]
    sims = [
        [_title_similarity(paper.title, seeds[j].title) for j in range(k)]
        for paper in members
    ]
    # 贪心轮转分配:每轮把与当前种子最相似且尚未分配的成员划给它。
    # 桶规模由构造保证均衡(≤ceil(n/k)≤cap),不受同质标题下相似度
    # 细微差异导致的 argmax 一边倒影响(argmax 会拆出 60/8/7 这种桶)。
    per = math.ceil(len(members) / k)
    remaining = list(range(len(members)))
    buckets: list[list[str]] = []
    for j in range(k):
        remaining.sort(key=lambda i: (-sims[i][j], i))
        take = max(1, min(per, len(remaining) - (k - 1 - j)))
        chosen = remaining[:take]
        buckets.append([members[i].lit_id for i in chosen])
        remaining = remaining[take:]
    buckets[-1].extend(members[i].lit_id for i in remaining)
    parts: list[Group] = []
    for j, bucket in enumerate(b for b in buckets if b):
        payload = Group(name=group.name, lit_ids=bucket,
                        research_question=group.research_question)
        if len(bucket) > cap and depth + 1 < 2:
            parts.extend(_split_group_balanced(
                papers, payload, cap, topic, used_names, depth + 1,
            ))
            continue
        name = _unique_evidence_group_name(papers, bucket, used_names, j + 1)
        if not name or _group_name_conflicts(name, used_names) or not _group_name_acceptable(
            name, pool_language=_pool_dominant_language(papers),
        ):
            base = _clean_group_name(group.name) or _clean_group_name(topic) or "研究综述"
            base_name = f"{base[:12]}·第{j + 1}部分"
            name = base_name
            counter = 2
            while _group_name_conflicts(name, used_names) or not _group_name_acceptable(
                name, pool_language=_pool_dominant_language(papers),
            ):
                name = f"{base_name}({counter})"
                counter += 1
        payload.name = name
        used_names.add(name)
        parts.append(payload)
    return parts


def _balance_oversize_groups(
    papers: list[Paper], groups: list[Group], topic: str,
) -> list[Group]:
    """拆分过度集中的巨型主题组,防止「90 篇里 75 篇挤进一个泛化主题」复发。

    上限与 harness 的 theme_balance 检查(最大主题过半即失衡)对齐:真主题
    允许长到一半 —— 证据密集的问题簇就该是大章节,35% 时代的强拆只会把
    完整主题肢解成伪主题;超过一半说明定轴失败,原地拆成均衡子组保住可写性。
    小池子(<16 篇)不做处理。
    """
    if len(papers) < 16 or not groups:
        return groups
    cap = max(12, math.ceil(len(papers) * 0.5))
    if not any(len(g.lit_ids) > cap for g in groups):
        return groups
    used_names = {g.name for g in groups}
    result: list[Group] = []
    for group in groups:
        if len(group.lit_ids) <= cap:
            result.append(group)
            continue
        # 父组被拆散,其名字不再占用;子组名从组内标题证据重新取证。
        used_names.discard(group.name)
        result.extend(_split_group_balanced(papers, group, cap, topic, used_names))
    return [g for g in result if g.lit_ids]


def _retitle_groups_for_pool(
    papers: list[Paper], groups: list[Group], topic: str, progress=None,
) -> list[Group]:
    """池级主题名统一校验:语言一致性 + 泛化名/标题碎片,证据重命名优先。

    分块各自 finalize 时用的是块内主导语言,交错分块下含英文文献的块可能
    给出英文主题名;二级归并/超限折叠产生的新组合名也不再经过池级校验
    (线上案例:中文综述确认面板出现英文章节名和"以乳山市为例")。这里以
    全池主导语言重校:能从组内证据重命名的直接重命名;纯英文文献组交给
    一次批量 LLM 改名;LLM 失败保留原名(结构优先,用户可手动改名)。
    """
    if not groups:
        return groups
    pool_lang = _pool_dominant_language(papers)
    used = {g.name for g in groups}
    need_llm: list[Group] = []
    for index, group in enumerate(groups, start=1):
        name_ok = _group_name_acceptable(group.name, pool_language=pool_lang)
        title_copy = _looks_like_title_prefix(group.name, papers, group.lit_ids)
        if name_ok and not title_copy:
            continue
        renamed = _unique_evidence_group_name(
            papers, group.lit_ids, used - {group.name}, index,
        )
        renamed_ok = (
            renamed
            and renamed not in used
            and _group_name_acceptable(renamed, pool_language=pool_lang)
            and not _looks_like_title_prefix(renamed, papers, group.lit_ids)
        )
        if renamed_ok:
            used.discard(group.name)
            used.add(renamed)
            group.name = renamed
        else:
            need_llm.append(group)
    if not need_llm:
        return groups
    if progress:
        progress({
            "kind": "retitle_started",
            "count": len(need_llm),
            "message": f"正在规范化 {len(need_llm)} 个主题的命名(语言一致性/泛化名)...",
        })
    new_names = _llm_retitle_group_names(topic, pool_lang, need_llm, papers)
    for group, new_name in zip(need_llm, new_names):
        if (new_name
                and new_name not in used
                and _group_name_acceptable(new_name, pool_language=pool_lang)):
            used.discard(group.name)
            used.add(new_name)
            group.name = new_name
    return groups


def _llm_retitle_group_names(
    topic: str, pool_lang: str, groups: list[Group], papers: list[Paper],
) -> list[str]:
    """一次批量调用,把不合格主题名改写为池语言的学术主题名。

    只做命名,不动任何文献归属;失败时返回空串列表,调用方保留原名。
    """
    by_id = {p.lit_id: p for p in papers}
    lines = []
    for index, group in enumerate(groups, start=1):
        reps = [by_id[lid].title for lid in group.lit_ids[:3] if lid in by_id]
        lines.append(f"#{index} 当前主题名:{group.name} | 代表文献:{'；'.join(reps)}")
    target = "中文" if pool_lang == "zh" else "English"
    try:
        raw = messages_create(
            system=(
                "你是学术综述主题命名器。把给定的主题名逐条改写为简明、相互区分的学术主题名。"
                "概括各组文献共同回答的研究问题;不使用'综合研究''概述'等泛化词,"
                "不使用'以XX为例'这类个案研究副标题,也不照抄任何单篇文献标题;"
                "名称内部不要使用'与/和/及'连接词。只输出JSON。"
            ),
            user=(
                f"研究主题:{topic}\n目标语言:{target}(3-12字,语言必须一致)\n"
                + "\n".join(lines)
                + '\n只输出:{"names":["主题名1","主题名2"]},数量必须与输入一致。'
            ),
            max_tokens=1000, temperature=0.2,
            response_format={"type": "json_object"},
            timeout=30.0, max_retries=1,
        )
        data = parse_llm_json(raw)
        names_raw = data.get("names", []) if isinstance(data, dict) else []
        cleaned = [_clean_group_name(str(item)) for item in names_raw]
        while len(cleaned) < len(groups):
            cleaned.append("")
        return cleaned
    except Exception as exc:
        logger.warning("主题名批量改写失败,保留原名: %s", exc)
        return [""] * len(groups)


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
    pool_lang = _pool_dominant_language(papers)
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
        if not label or _group_name_conflicts(label, used_names) or not _group_name_acceptable(label, pool_language=pool_lang):
            # 使用同一簇其他文献中的真实短语做语义消歧；绝不拼接“研究议题N”。
            for lid in bucket[1:]:
                candidate = _unique_evidence_group_name(papers, [lid], used_names, i + 1)
                if candidate and not _group_name_conflicts(candidate, used_names):
                    label = candidate
                    break
        if not label or _group_name_conflicts(label, used_names) or not _group_name_acceptable(label, pool_language=pool_lang):
            # 标题高度重复时，从标题中的第二个自然短语取证（例如“需求约束”“
            # 风险控制”等）。这是文献证据，不是领域词典或编号占位符。
            rep_title = representative.title or ""
            phrases = [x for x in _re.findall(r"[\u4e00-\u9fff]{2,8}", rep_title)
                       if x not in _GENERIC_GROUP_NAMES]
            for phrase in phrases:
                candidate = _clean_group_name(phrase)
                if (_group_name_acceptable(candidate, pool_language=pool_lang)
                        and not _group_name_conflicts(candidate, used_names)):
                    label = candidate
                    break
        if not label or _group_name_conflicts(label, used_names) or not _group_name_acceptable(label, pool_language=pool_lang):
            # 标题高度同质、各桶证据名同名冲突时，用代表标题中文前缀+序号
            # 消歧（与 _semantic_cluster_groups 的 base+index 先例一致，根
            # 仍是真实文献标题而非占位词）。不能把所有桶并回一组：几十篇
            # 并成单主题既没法写章节，也过不了最低主题数结构核查。
            representative = next(
                (by_id[lid] for lid in bucket if _is_chinese_paper(by_id.get(lid))),
                by_id[bucket[0]],
            )
            chinese = "".join(_re.findall(r"[\u4e00-\u9fff]", representative.title or ""))[:8]
            base = chinese or _clean_group_name(topic) or "研究综述"
            candidate = f"{base[:9]}{i + 1}"
            if _group_name_acceptable(candidate, pool_language=pool_lang):
                groups.append(Group(name=candidate, lit_ids=bucket))
                used_names.add(candidate)
                continue
            # 实在无法命名时合并回已有主题，而不是产出伪标题。
            if groups:
                groups[0].lit_ids.extend(x for x in bucket if x not in groups[0].lit_ids)
            continue
        groups.append(Group(name=label, lit_ids=bucket))
        used_names.add(label)
    if not groups:
        # 极端标题缺失时只能使用用户给定的研究主题作为单一总主题；这比
        # 输出编号/占位章节更诚实，也不会写死任何领域词。
        label = _clean_group_name(topic)
        if _group_name_acceptable(label, pool_language=_pool_dominant_language(papers)):
            return [Group(name=label, lit_ids=[p.lit_id for p in papers])]
    covered = {lid for g in groups for lid in g.lit_ids}
    _reassign_orphans(papers, [p.lit_id for p in papers if p.lit_id not in covered], groups)
    return _balance_oversize_groups(papers, groups, topic)


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
        chinese_name = _best_title_phrase(
            representative_title, _pool_dominant_language(papers),
        )
        if chinese_name:
            return chinese_name

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
    pool_lang = _pool_dominant_language(papers)
    for index, group in enumerate(groups, start=1):
        if group.name in used_names or not _group_name_acceptable(group.name, pool_language=pool_lang):
            representative = by_lid.get(group.lit_ids[0], papers[0])
            chinese = "".join(_re.findall(r"[\u4e00-\u9fff]", representative.title or ""))
            words = [
                w for w in _re.findall(r"[A-Za-z][A-Za-z0-9-]{1,}", representative.title or "")
                if w.lower() not in _ENGLISH_STOPWORDS
            ]
            if chinese:
                group.name = (chinese[:9] + str(index)).strip()
            else:
                # zh 池的纯英文簇:语言不是主题,展示层必须中文。用主题派生
                # 的「XX国际研究」标签保证命名防线可收敛 —— 此前这里会
                # 生成英文标签再被质量检查打回,整个 local_fallback 阻塞为
                # 0 组(线上复现:423 篇池三策略全 blocked)。
                label = (
                    _zh_label_for_english_group(topic, used_names)
                    if pool_lang == "zh" else ""
                )
                if not label:
                    continue
                group.name = label
        used_names.add(group.name)
    groups = [g for g in groups if g.lit_ids]
    # 退化防护:同质/低区分度池子里,文献与各簇中心的相似度几乎相同,
    # k-means 会把全部文献落进同一簇、其余簇为空。单主题撑不起综述章节
    # 结构:按分散种子+标题相似度原地重拆,相似度不可分(并列/全零)时
    # 按序号轮转保证非退化;随后仍走统一的去重命名与 LLM 命名流程。
    if len(groups) < 2 and n >= 9:
        k2 = max(2, _desired_auto_groups(n))
        seeds2 = [papers[round(i * (n - 1) / (k2 - 1))] for i in range(k2)]
        buckets2: list[list[int]] = [[] for _ in range(k2)]
        for i, paper in enumerate(papers):
            sims = [_title_similarity(paper.title, seeds2[j].title) for j in range(k2)]
            best = max(range(k2), key=lambda j: sims[j])
            if sims[best] <= 0.0 or sims.count(sims[best]) > 1:
                idx = i % k2
            else:
                idx = best
            buckets2[idx].append(i)
        for j in range(k2):
            if buckets2[j]:
                continue
            donor = max(range(k2), key=lambda t: len(buckets2[t]))
            if len(buckets2[donor]) > 1:
                buckets2[j].append(buckets2[donor].pop())
        groups = [
            Group(
                name=local_name(b, min(j, len(seeds) - 1)),
                lit_ids=[papers[i].lit_id for i in b],
            )
            for j, b in enumerate(buckets2)
            if b
        ]
    if progress:
        progress({"kind": "local_cluster_done", "count": len(groups), "covered": n, "message": f"本地摘要聚类完成，共形成 {len(groups)} 个候选主题簇。"})

    # 一次性命名，不让命名模型参与归属决策。请求设置短超时：成功时得到
    # 中文问题导向标题，超时则立即保留上面从簇中心标题提取的证据名称，
    # 不能再为等待“美化标题”阻塞数分钟。
    if groups:
        try:
            if progress:
                progress({"kind": "naming_started", "count": len(groups), "message": "主题簇已形成，正在生成可读主题名称..."})
            catalog = "\n".join(
                f"#{i + 1} 代表标题：{(by_lid.get(groups[i].lit_ids[0]) or papers[0]).title}"
                f"；摘要：{((by_lid.get(groups[i].lit_ids[0]) or papers[0]).abstract or '')[:240]}"
                for i in range(len(groups))
            )
            raw = messages_create(
                system=(
                    "你是文献综述主题命名助手，只给已有主题簇命名，不重新分配文献。"
                    "命名必须概括该簇共同回答的研究问题或待解释对象，不能使用语言、数据库、期刊、作者名，"
                    "也不能把孤立方法名或摘要英文碎片当主题。只输出JSON。"
                ),
                user=(
                    f"研究主题：{topic}\n{catalog}\n"
                    f"输出：{{\"names\":[\"中文、问题导向的学术主题\"]}}，名称数量必须为{len(groups)}。"
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
                if (_group_name_acceptable(
                        clean, pool_language=_pool_dominant_language(papers))
                        and clean not in used_names):
                    group.name = clean
                elif (_group_name_acceptable(
                        original, pool_language=_pool_dominant_language(papers))
                        and original not in used_names):
                    group.name = original
                used_names.add(group.name)
        except Exception as exc:
            logger.warning("本地聚类主题命名失败，保留证据主题名: %s", exc)
    # 中文池的纯英文簇同样必须持中文组名。
    _rename_english_groups_for_zh_pool(papers, groups, topic)
    # 命名器可能少返回或返回重复名；最终再做一次全量收敛。
    used_names: set[str] = set()
    resolved: list[Group] = []
    for index, group in enumerate(groups, start=1):
        candidate = None
        if (_group_name_acceptable(
                group.name, pool_language=_pool_dominant_language(papers))
                and group.name not in used_names):
            candidate = group.name
        if candidate is None:
            # 证据改名走统一的中文优先链路(_unique_evidence_group_name
            # 会优先取簇内中文文献标题并带池语言校验):此前用 local_name
            # 裸命名 + 无池语言检查,英文簇的英文名在 zh 池永远不合格,
            # 整个 local_fallback 被命名防线打回为 0 组。
            evidence_name = _unique_evidence_group_name(
                papers, group.lit_ids, used_names, index,
            )
            if (
                _group_name_acceptable(evidence_name, pool_language=pool_lang)
                and evidence_name not in used_names
            ):
                candidate = evidence_name
        if candidate is None:
            # 命名模型失败且证据名冲突:用代表标题中文前缀+序号消歧,
            # 不让单个簇的命名问题把整次可用的聚类结果作废。
            representative = by_lid.get(group.lit_ids[0], papers[0])
            chinese = "".join(_re.findall(r"[\u4e00-\u9fff]", representative.title or ""))[:8]
            base = chinese or _clean_group_name(topic) or "研究综述"
            numbered = f"{base[:9]}{index}"
            if (_group_name_acceptable(numbered, pool_language=pool_lang)
                    and numbered not in used_names):
                candidate = numbered
        if candidate is None and resolved:
            # 仍无法命名的簇并入前一组:文献不丢、组名唯一,优于整体报错。
            resolved[-1].lit_ids.extend(group.lit_ids)
            continue
        group.name = candidate or group.name
        resolved.append(group)
        used_names.add(group.name)
    # 池级终检:语言一致性 + 标题碎片(LLM 命名也可能产出近似标题复制名),
    # 与 LLM 分组路径共用同一套出口校验。
    retitled = _retitle_groups_for_pool(papers, resolved, topic)
    return _balance_oversize_groups(papers, retitled, topic)


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


# ================= 两遍式主题划分:全局定轴 + 固定主题归组 =================
# 旧分块法:每块 15 篇各自发明主题,再按标题词面相似度跨块合并。同一真主题
# 在不同块里会被起不同名字,词面不像就永远合不回去,真主题裂成伪主题;行业词
# 当组名又把问题维度切碎。两遍式让模型先通读全部题录定出互斥主题轴,再按固定
# 轴归组 —— 主题命名天然跨全集一致,归组阶段不存在"合并"这一步。

_AXIS_MAX = 8


def _abstract_limit_for(papers: list[Paper], *, minimal: bool) -> int:
    """两遍式共用的摘要截断长度:池子越大截得越短,保证单次调用放得下。"""
    if minimal:
        return 220
    n = len(papers)
    if n >= 350:
        return 120
    if n >= 200:
        return 200
    return 320


def _catalog_lines(papers: list[Paper], abstract_limit: int) -> str:
    """编号目录:行首短编号供模型回显,lit_id 原文绝不出现在输出协议里。"""
    return "\n".join(
        f"- #{i + 1} | {p.title} | {p.year or 'N/A'} | 摘要:{(p.abstract or '').strip()[:abstract_limit]}..."
        for i, p in enumerate(papers)
    )


def _axis_catalog(axes: list[Group]) -> str:
    lines = []
    for i, axis in enumerate(axes, start=1):
        line = f"{i}. {axis.name}"
        if axis.research_question:
            line += f" —— {axis.research_question}"
        lines.append(line)
    return "\n".join(lines)


def _parse_axis_response(raw: str) -> list[dict]:
    data = parse_llm_json(raw)
    items: list[dict] | None = None
    if isinstance(data, dict):
        for key in ("themes", "groups", "clusters", "categories"):
            value = data.get(key)
            if isinstance(value, list):
                items = [x for x in value if isinstance(x, dict)]
                break
    if items is None:
        items = _salvage_groups_json(raw) or []
    if items is None or not items:
        # json_object 并不可靠:MiniMax 偶发按「Theme N: 名称 + Research
        # question: ...」小作文输出主题轴。语义内容是好的,补文本抢救。
        items = _salvage_text_axes(raw)
    return items


_THEME_HEADING_RE = _re.compile(
    r"(?im)^\s*(?:\*{1,2})?(?:theme|主题轴|主题)\s*\d+\s*[:：.、]\s*(.+?)\s*$"
)
_THEME_RQ_RE = _re.compile(
    r"(?im)^\s*(?:\*{1,2})?(?:research question|研究问题)\s*[:：]\s*(.+?)\s*$"
)


def _salvage_text_axes(raw: str) -> list[dict]:
    """抢救「Theme N: 名称 … Research question: …」式自然语言主题轴。

    只做格式转换:主题名与研究问题必须原文出现,不猜不补。
    """
    text = str(raw or "")
    headings = list(_THEME_HEADING_RE.finditer(text))
    axes: list[dict] = []
    for index, match in enumerate(headings):
        name = match.group(1).strip().strip("*#· ")
        # 剥掉模型附带的英文对照注释("xxx - English gloss")
        name = name.split(" - ")[0].split(" — ")[0].strip()
        body_end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        body = text[match.end():body_end]
        rq_match = _THEME_RQ_RE.search(body)
        axes.append({
            "name": name,
            "research_question": rq_match.group(1).strip() if rq_match else "",
        })
    return axes


def _normalize_axes(items: list[dict], n_papers: int, pool_language: str | None = None) -> list[Group]:
    """主题轴校验:名字合格(含池语言一致性)、互斥,数量落在下限与 _AXIS_MAX。

    互斥不仅查包含关系,还查 bigram 相似度 —— 「数智化驱动的企业营销策略
    转型」与「数智化驱动的营销策略转型」互不包含却是同一主题的两种措辞,
    同时保留会让归组阶段无所适从。不合格/相近轴丢弃,数量不足下限返回空列表。
    """
    axes: list[Group] = []
    seen: set[str] = set()
    for item in items:
        name = _clean_group_name(str(item.get("name") or item.get("theme") or ""))
        if not name or not _group_name_acceptable(name, pool_language=pool_language):
            continue
        if _zh_theme_name_too_short(name, pool_language):
            # 过短名称保留但记录告警(不丢弃——丢弃会导致归组阶段无轴可用,
            # 整个分类 blocked)。质量提示由 harness theme_name_quality 报告。
            logger.warning("主题轴名偏短(%d字): %s (保留,建议用户手动修改)", len(name), name)
        if _group_name_conflicts(name, seen):
            continue
        if any(_title_similarity(name, used) >= 0.5 for used in seen):
            continue
        rq = str(item.get("research_question") or item.get("question") or "").strip()
        axes.append(Group(name=name, lit_ids=[], research_question=rq or None))
        seen.add(name)
        if len(axes) >= _AXIS_MAX:
            break
    if len(axes) < max(2, _min_groups(n_papers)):
        return []
    return axes


def _salvage_axes_from_prose(
    raw: str, papers: list[Paper], topic: str, language: str, *,
    progress=None,
) -> list[Group]:
    """把 MiniMax 写成自由文本的分组方案抢救成主题轴(一次转换调用)。

    线上散文日实测:第一次输出的分组内容就是好的,只是没按 JSON 输出。
    每次尝试失败后立即抢救,比等三次全败再抢救省 2 轮 x 1-2 分钟。
    """
    if not (raw or "").strip():
        return []
    try:
        if progress:
            progress({
                "kind": "axis_plan_salvage_started",
                "message": "模型输出格式异常,正在抢救已生成的分组方案...",
            })
        salvage_raw = messages_create(
            system=(
                "你是格式转换器。把输入的主题分组方案(自由文本)转成严格 JSON,"
                "保留全部分组信息,不新增、不合并、不重命名。"
                "每个主题的 name 必须是 ≤12 字的中文学术名词短语,"
                "research_question 是一句话中文研究问题。只输出 JSON 对象:"
                '{"themes": [{"name": "...", "research_question": "..."}]}'
            ),
            user=(
                f"研究主题:{topic}"
                "\n\n分组方案文本:"
                f"\n{raw[:3500]}"
            ),
            max_tokens=2500, temperature=0.0,
            response_format={"type": "json_object"},
            timeout=90.0, max_retries=0,
        )
        axes = _normalize_axes(_parse_axis_response(salvage_raw), len(papers), language)
        if axes:
            logger.info("主题轴散文抢救成功:%d 个主题", len(axes))
        return axes
    except Exception as exc:  # noqa: BLE001
        logger.warning("主题轴散文抢救失败: %s", exc)
        return []


def _corpus_display_author_year(paper: Paper) -> str:
    """大文档条目的作者(年份)显示;脏数据首作者回退为「佚名」。"""
    authors = [str(a).strip() for a in (paper.authors or []) if str(a).strip()]
    year = f"（{paper.year}）" if paper.year else ""
    if not authors:
        return f"佚名{year}"
    first = authors[0]
    if _re.search(r"[一-鿿]", first):
        compact = _re.sub(r"[^一-鿿]", "", first)
        name = first if len(compact) >= 2 else "佚名"
    else:
        tokens = [tok for tok in _re.findall(r"[A-Za-z][A-Za-z\-']*", first) if len(tok) >= 2]
        name = tokens[-1] if tokens else "佚名"
    if len(authors) > 1:
        name += "等" if _re.search(r"[一-鿿]", name) else " et al."
    return f"{name}{year}"


_CORPUS_MIN_ABSTRACT_CHARS = 10


def _corpus_candidates(papers: list[Paper]) -> list[Paper]:
    """拼接初筛:剔除没有摘要(缺失或过短残句)的文献。

    用户规则:没有摘要的观点卡没有内容可拼,直接剔除,不进大文档。
    """
    return [
        p for p in papers
        if len((p.abstract or "").strip()) >= _CORPUS_MIN_ABSTRACT_CHARS
    ]


def _build_corpus_document(papers: list[Paper], budget_chars: int | None = None) -> str:
    """把全池文献拼接成一篇"大文档",作为主题轴规划与写作的统一输入。

    用户策略(v9.10):先拼接、再划分 —— 每篇文献一条观点卡:
        XXX（年份）认为：<摘要全文>
    - 拼接前剔除无摘要文献(_corpus_candidates);
    - 总预算(PLAN_CORPUS_CHARS,默认 24 万字符)放不下时按篇均摊缩减
      摘要;极端时剩余文献只保留「作者（年份）认为：(摘要缺失)」占位
      —— 陈述句形态保持不变,任何文献都不丢。
    """
    import os as _os
    if budget_chars is None:
        try:
            budget_chars = int(_os.environ.get("PLAN_CORPUS_CHARS", "240000"))
        except ValueError:
            budget_chars = 240_000
    n = len(papers)
    if not papers:
        return ""
    per_paper = max(300, budget_chars // max(1, n))
    lines: list[str] = []
    used = 0
    for idx, paper in enumerate(papers, start=1):
        abstract = (paper.abstract or "").strip()
        if len(abstract) > per_paper:
            abstract = abstract[:per_paper].rstrip() + "…"
        display = _corpus_display_author_year(paper)
        block = f"{display}认为：{abstract or '(摘要缺失)'}"
        if used + len(block) > budget_chars and lines:
            for j in range(idx, n + 1):
                placeholder = _corpus_display_author_year(papers[j - 1])
                lines.append(f"{placeholder}认为：(摘要缺失)")
            break
        lines.append(block)
        used += len(block) + 2
    return "\n\n".join(lines)


def _parse_prose_axes(raw: str, n_papers: int, language: str) -> list[Group]:
    """从 MiniMax 输出的散文/编号列表中直接提取主题轴(不依赖 JSON)。

    线上实测 MiniMax 在 JSON 模式下仍输出 markdown 编号列表:
      1. **区域水产品品牌建设** - 相关论文…
      2. **电商营销渠道** - …
    这些散文的主题簇归纳质量很好,只是格式不是 JSON。用正则直接提取,
    不需要 LLM 二次转换 —— 这是比 _salvage_axes_from_prose 更快、更稳定
    的兜底,也是 agent 循环中"格式失败→换一种解析方式再试"的体现。
    """
    axes: list[Group] = []
    seen: set[str] = set()
    # 先走编号列表分支(能从 " - 说明" 尾巴里带走研究问题),
    # 粗体分支只兜不编号的裸标题;同名以编号分支的为准。
    # 匹配编号列表模式: "1. 主题名" 或 "1. **主题名**"
    # MULTILINE 的 ^ 是零宽断言,不消费换行 —— 否则上一条的 (?:\n) 会让
    # finditer 隔行漏匹配(实测 1/3 命中、2 被跳过)。
    for m in _re.finditer(r"^[ \t]*\d+[.、][ \t]*(.+?)[ \t]*$", raw or "", _re.M):
        raw_name = m.group(1).strip()
        # 剥掉 markdown 粗体;破折号后的说明文字作为研究问题保留
        name = _re.sub(r"\*\*", "", raw_name).strip()
        rq: str | None = None
        rq_m = _re.search(r"\s+[-–—:：]\s+(.+)$", name)
        if rq_m:
            tail = rq_m.group(1).strip()
            if len(tail) >= 8:  # 足够长才像研究问题,否则是格式噪音
                rq = tail
        name = _re.sub(r"\s+[-–—:：]\s+.*$", "", name).strip()
        name = _re.sub(r"[（(].*$", "", name).strip()    # 剥掉 " (解释)"
        name = _clean_group_name(name)
        if not name or name in seen:
            continue
        if not _group_name_acceptable(name, pool_language=language):
            continue
        axes.append(Group(name=name, lit_ids=[], research_question=rq))
        seen.add(name)
    # 兜底:不编号的裸粗体标题 "**主题**"(MiniMax 也会输出这种形态)
    for m in _re.finditer(r"\*\*(.{4,20}?)\*\*", raw or ""):
        name = _clean_group_name(m.group(1).strip())
        if not name or name in seen:
            continue
        if not _group_name_acceptable(name, pool_language=language):
            continue
        axes.append(Group(name=name, lit_ids=[], research_question=None))
        seen.add(name)
    return axes


def _plan_theme_axes(
    papers: list[Paper], topic: str, language: str, *,
    abstract_limit: int, progress=None,
) -> list[Group]:
    """第一遍:单次调用通读全部题录,产出全局主题轴(名称 + 研究问题)。"""
    # 用户策略:先拼接成大文档再划分 —— 完整摘要进上下文,
    # 主题轴基于通读全部文献而非残缺目录;无摘要文献先剔除
    candidates = _corpus_candidates(papers)
    if not candidates:
        raise ValueError(
            "文献池中没有带摘要的文献，无法拼接大文档进行主题划分。"
            "请导入含摘要的文献后重试。"
        )
    catalog = _build_corpus_document(candidates)
    system = render(
        "literature-review:classify-plan",
        topic=topic,
        papers_catalog=catalog,
        topic_language=(language or "zh").upper(),
        axis_min=max(2, _min_groups(len(papers))),
    )
    lang_name = "中文" if (language or "zh") == "zh" else "English"
    user = (
        f"研究主题:{topic}\n请通读以上 {len(papers)} 篇文献,输出主题划分方案。"
        f"name 与 research_question 必须使用{lang_name}。"
        "只输出 JSON 对象,不输出任何解释文字。"
    )
    # 防偷懒下限:18 篇以上的异质文献池通常不止 2 个研究问题簇,
    # 前两次尝试少于 3 个主题轴按"未完成"返工;最后一次仍偷懒则接受,
    # 交给确认面板人工调整,不无限重试。
    lazy_floor = 3 if len(papers) >= 18 else max(2, _min_groups(len(papers)))
    floor = max(2, _min_groups(len(papers)))
    last_error: Exception | None = None
    last_raw = ""
    for attempt, temperature in ((1, 0.2), (2, 0.4), (3, 0.6)):
        import threading as _thr_plan
        _hb_stop = _thr_plan.Event()
        attempt_user = user
        if attempt >= 2:
            attempt_user = (
                f"{user}\n\n返工要求:上一次输出的主题轴不可用(语言不符、"
                f"占位名、缺少 research_question 或数量不足)。主题名必须全部"
                f"使用{lang_name};每个主题轴的 research_question 不得为空;"
                f"至少 {lazy_floor} 个主题。"
            )
        try:
            if progress:
                progress({
                    "kind": "llm_attempt", "attempt": attempt,
                    "papers": len(papers), "stage": "axis_plan",
                })
            _heartbeat_progress(
                progress,
                {"attempt": attempt, "papers": len(papers), "stage": "axis_plan"},
                _hb_stop, interval=5.0,
            )
            try:
                raw = messages_create(
                    system=system, user=attempt_user, max_tokens=2500,
                    temperature=temperature,
                    response_format={"type": "json_object"},
                    timeout=120.0, max_retries=1,
                )
            finally:
                _hb_stop.set()
            last_raw = raw
        except Exception as exc:
            last_error = exc
            logger.warning("主题轴规划 LLM 调用失败(attempt=%d): %s", attempt, exc)
            if progress:
                progress({
                    "kind": "llm_error", "attempt": attempt,
                    "papers": len(papers), "stage": "axis_plan",
                    "error_type": type(exc).__name__, "message": str(exc)[:500],
                })
            continue
        axes = _normalize_axes(_parse_axis_response(raw), len(papers), language)
        if len(axes) < floor and raw.strip():
            # agent 循环"策略调整"层:JSON 解析不出轴时,先换解析器
            # (正则直提散文主题,零 LLM 成本),再降级到 LLM 格式转换。
            axes = _parse_prose_axes(raw, len(papers), language) or axes
        if len(axes) < floor and raw.strip():
            # 每次失败立即尝试散文抢救(不等三次全败,散文日第一次即可成功)
            axes = _salvage_axes_from_prose(
                raw, papers, topic, language, progress=progress,
            ) or axes
        if len(axes) >= floor and (len(axes) >= lazy_floor or attempt >= 3):
            if progress:
                progress({
                    "kind": "axis_plan_done", "themes": [a.name for a in axes],
                    "message": f"主题轴规划完成,共 {len(axes)} 个主题:{'、'.join(a.name for a in axes)}",
                })
            return axes
        logger.warning(
            "主题轴规划第 %d 次输出不可用(axes=%d, floor=%d, lazy_floor=%d),原文预览=%s",
            attempt, len(axes), floor, lazy_floor, str(raw)[:300],
        )
    # MiniMax 偶发把「分组内容可用、只是没按 JSON 输出」的方案写成
    # markdown 散文(线上实测:三次输出都是编号列表,主题簇归纳得很好)。
    # 直接放弃会整场作废并降级到本地聚类;用一次廉价转换调用把最后一版
    # 散文转成严格 JSON,内容不重排、命名交给既有校验。
    if last_raw.strip():
        try:
            if progress:
                progress({
                    "kind": "axis_plan_salvage_started",
                    "message": "模型输出格式异常,正在抢救已生成的分组方案...",
                })
            salvage_raw = messages_create(
                system=(
                    "你是格式转换器。把输入的主题分组方案(自由文本)转成严格 JSON,"
                    "保留全部分组信息,不新增、不合并、不重命名。"
                    "每个主题的 name 必须是 ≤12 字的中文学术名词短语,"
                    "research_question 是一句话中文研究问题。只输出 JSON 对象:"
                    '{"themes": [{"name": "...", "research_question": "..."}]}'
                ),
                user=f"研究主题:{topic}\n\n分组方案文本:\n{last_raw[:3500]}",
                max_tokens=2500, temperature=0.0,
                response_format={"type": "json_object"},
                timeout=90.0, max_retries=0,
            )
            axes = _normalize_axes(_parse_axis_response(salvage_raw), len(papers), language)
            if len(axes) >= floor:
                if progress:
                    progress({
                        "kind": "axis_plan_done",
                        "themes": [a.name for a in axes],
                        "message": f"分组方案抢救成功,共 {len(axes)} 个主题。",
                    })
                return axes
            logger.warning("主题轴散文抢救输出不可用(axes=%d)", len(axes))
        except Exception as exc:  # noqa: BLE001
            logger.warning("主题轴散文抢救失败: %s", exc)
    raise ValueError(
        f"主题轴规划失败:{last_error or '模型输出不包含可用的主题方案'}"
    )


def _anchor_groups_to_axes(groups: list[Group], axes: list[Group]) -> list[Group]:
    """把归组结果对齐回主题轴:按轴序输出,锚定轴的研究问题。

    模型可能微调组名措辞;精确匹配失败时按去噪后的包含关系回退。匹配不到
    任何轴的组作废,其文献由调用方按孤儿回收 —— 脱离主题轴的野组不进确认面板。
    """
    def _compact(name: str) -> str:
        return _re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", name or "")

    by_axis: dict[int, list[Group]] = {}
    for group in groups:
        matched = None
        for idx, axis in enumerate(axes):
            if group.name == axis.name:
                matched = idx
                break
        if matched is None:
            compact = _compact(group.name)
            for idx, axis in enumerate(axes):
                axis_compact = _compact(axis.name)
                if compact and axis_compact and (
                    compact in axis_compact or axis_compact in compact
                ):
                    matched = idx
                    break
        if matched is not None:
            by_axis.setdefault(matched, []).append(group)
    anchored: list[Group] = []
    for idx, axis in enumerate(axes):
        parts = by_axis.get(idx) or []
        ids: list[str] = []
        for part in parts:
            ids.extend(lid for lid in part.lit_ids if lid not in ids)
        if ids:
            anchored.append(Group(
                name=axis.name, lit_ids=ids,
                research_question=axis.research_question,
            ))
    return anchored


def _assign_papers_to_axes(
    papers: list[Paper], axes: list[Group], topic: str, *,
    abstract_limit: int, progress=None,
) -> list[Group]:
    """第二遍:把每篇文献归入固定主题轴,输出与轴一一对应。

    编号协议与抢救/格式修复链路复用单发路径;组名必须逐字来自主题轴,
    脱轴输出的文献按标题相似度回收到最相近的轴。
    """
    index_of = {str(i + 1): p.lit_id for i, p in enumerate(papers)}
    valid_ids = {p.lit_id for p in papers}
    system = render(
        "literature-review:classify-assign",
        topic=topic,
        theme_axes=_axis_catalog(axes),
        axis_count=len(axes),
        papers_catalog=_catalog_lines(papers, abstract_limit),
    )
    token_budget = _output_token_budget(papers)
    axis_names_block = "\n".join(f"- {a.name}" for a in axes)
    best: list[Group] = []
    best_covered = -1
    last_error: Exception | None = None
    for attempt, temperature in ((1, 0.1), (2, 0.3), (3, 0.3)):
        import threading as _thr_assign
        _hb_stop = _thr_assign.Event()
        # MiniMax 对 system 里长目录末尾的轴清单遵循度差,把允许的主题名
        # 放到 user 消息开头(注意力权重更高的位置)并强令逐字使用。
        attempt_user = (
            f"研究主题:{topic}\n"
            f"唯一允许输出的主题名(逐字使用,共 {len(axes)} 个):\n{axis_names_block}\n\n"
            f"任务:把给定文献清单的 {len(papers)} 篇文献逐篇归入上述主题轴,"
            "每篇恰属一组;ids 只写行首编号数字;严禁修改主题名、严禁自拟主题;"
            "不要输出任何分析过程。只输出 JSON:"
            '{"groups": [{"name": "<主题名>", "ids": [1, 2, 3]}]}'
        )
        if attempt >= 2:
            attempt_user += (
                f"\n\n返工要求:上一次输出没有逐字使用给定主题名。"
                f"name 只能取自:\n{axis_names_block}"
            )
        raw = ""
        try:
            if progress:
                progress({"kind": "llm_attempt", "attempt": attempt, "papers": len(papers), "stage": "assign"})
            _heartbeat_progress(
                progress,
                {"attempt": attempt, "papers": len(papers), "stage": "assign"},
                _hb_stop, interval=5.0,
            )
            try:
                raw = messages_create(
                    system=system, user=attempt_user, max_tokens=token_budget,
                    temperature=temperature,
                    response_format={"type": "json_object"},
                    timeout=120.0, max_retries=1,
                )
            finally:
                _hb_stop.set()
        except Exception as exc:
            last_error = exc
            logger.warning("主题归组 LLM 调用失败(attempt=%d): %s", attempt, exc)
            if progress:
                progress({
                    "kind": "llm_error", "attempt": attempt,
                    "papers": len(papers), "stage": "assign",
                    "error_type": type(exc).__name__, "message": str(exc)[:500],
                })
            continue
        items = _parse_group_response(raw)
        logger.info(
            "主题归组响应(attempt=%d, papers=%d, 解析=%d, 原文预览=%s)",
            attempt, len(papers), len(items), str(raw).replace("\n", " ")[:400],
        )
        # 逐行拆分防护(与单发路径同规则):组数 ≥ 文献数说明模型把目录
        # 每行当成了一组,没有可用的主题边界。
        if len(items) >= len(papers):
            items = []
        if not items:
            items = _repair_invalid_group_response(
                raw, topic=topic, papers=papers, progress=progress,
            )
        mapped = _map_group_items(items, index_of)
        built, _ = _build_groups(
            mapped, valid_ids, pool_language=_pool_dominant_language(papers),
        )
        anchored = _anchor_groups_to_axes(built, axes)
        covered = len({lid for g in anchored for lid in g.lit_ids})
        if progress:
            progress({
                "kind": "llm_result", "attempt": attempt,
                "papers": len(papers), "parsed_groups": len(items),
                "valid_groups": len(anchored), "covered": covered,
                "decision": "accept" if covered == len(papers) else "repair",
                "themes": [g.name for g in anchored],
            })
        if covered > best_covered:
            best, best_covered = anchored, covered
        if best_covered == len(papers):
            break
    if not best:
        # 轴绑定三次全败(模型无视给定轴自拟主题是 MiniMax 的常见行为)。
        # 与其报废整个划分,不如顺水推舟:让模型按自己的判断对全量文献
        # 直接分组一次 —— 自拟主题名经过 _build_groups/_finalize_theme_groups
        # 的同一套名称校验链,与人工确认面板的质量底线一致。
        if progress:
            progress({
                "kind": "assign_fallback_started",
                "message": "模型未能按给定主题轴归组,改为让其自主分组...",
            })
        import threading as _thr_free
        _hb_stop = _thr_free.Event()
        free_user = (
            f"研究主题:{topic}\n请通读以上 {len(papers)} 篇文献,按摘要中的实际"
            "研究问题分成 3-6 个主题,每篇恰属一组;组名是简洁的中文学术名词短语,"
            "概括该组共同回答的研究问题;ids 只写行首编号数字;不要输出分析过程。"
            '只输出 JSON:{"groups": [{"name": "<主题名>", "ids": [1, 2, 3]}]}'
        )
        try:
            _heartbeat_progress(
                progress,
                {"attempt": 4, "papers": len(papers), "stage": "assign_free"},
                _hb_stop, interval=5.0,
            )
            try:
                raw = messages_create(
                    system=system, user=free_user, max_tokens=token_budget,
                    temperature=0.2,
                    response_format={"type": "json_object"},
                    timeout=120.0, max_retries=1,
                )
            finally:
                _hb_stop.set()
            items = _parse_group_response(raw)
            if len(items) >= len(papers):
                items = []
            mapped = _map_group_items(items, index_of)
            best, _ = _build_groups(
                mapped, valid_ids, pool_language=_pool_dominant_language(papers),
            )
        except Exception as exc:
            logger.warning("主题自主分组兜底失败: %s", exc)
    if not best:
        raise ValueError(
            f"主题归组失败:{last_error or '模型未把任何文献归入给定主题轴'}"
        )
    # 孤篇组吸收:轴方案可能留下只有 1 篇的组,写不成章节。仅当吸收后
    # 组数仍满足最低组数要求时才合并;否则保留孤篇组交给用户在确认面板
    # 手工处置 —— 塌缩成单组会触发 theme_count 检查,反而整体作废。
    if len(best) > 1:
        keep = [g for g in best if len(g.lit_ids) >= 2]
        if len(keep) >= max(1, _min_groups(len(papers))):
            best = keep
    covered_ids = {lid for g in best for lid in g.lit_ids}
    _reassign_orphans(
        papers, [p.lit_id for p in papers if p.lit_id not in covered_ids], best,
    )
    return best


def _heartbeat_progress(progress, base_event, stop_event, interval=5.0):
    """Daemon thread that emits a still_alive progress event every
    ``interval`` seconds while a long HTTP call is in flight. UI
    counter resets on each emit so the user sees we are alive.
    """
    if not progress:
        return None
    state = {"first": True, "elapsed": 0.0}

    def _run():
        while not stop_event.is_set():
            if state["first"]:
                state["first"] = False
            else:
                state["elapsed"] = round(state["elapsed"] + interval, 2)
                try:
                    progress({**base_event, "kind": "still_alive",
                              "elapsed_since_call": state["elapsed"]})
                except Exception:
                    return
            if stop_event.wait(interval):
                return

    import threading as _thr
    t = _thr.Thread(target=_run, name="heartbeat", daemon=True)
    t.start()
    return t


def classify_by_theme(
    papers: list[Paper], topic: str, *, _allow_chunking: bool = True,
    _strategy: str = "default",  # default | minimal_abstracts | local_fallback
    _raise_on_coverage_failure: bool = True,
    _language: str | None = None,  # "zh" | "en" | None (auto-detect)
    _topic_language_hint: bool = True,
    progress=None, _require_multiple: bool = True,
) -> list[Group]:
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
    # 主题语言探测:由调用方显式传入或自动按文献池主力语言推断。
    if _language is None:
        _language = _pool_dominant_language(papers)
    if _strategy == "local_fallback" or (_allow_chunking and len(papers) >= 500):
        if progress:
            progress({"kind": "local_cluster_started", "count": len(papers), "message": f"正在对 {len(papers)} 篇文献的标题与摘要做本地语义聚类..."})
        return _semantic_cluster_groups(papers, topic, progress=progress)

    # 两遍式主题划分(≥ _CHUNK_THRESHOLD):第一遍全局定轴(模型通读全部
    # 题录,产出互斥主题轴),第二遍按固定轴归组。主题命名跨全集一致,
    # 不再有"块内自造主题 + 标题相似度合并"造成的主题裂变。
    if _allow_chunking and len(papers) >= _CHUNK_THRESHOLD:
        abstract_limit = _abstract_limit_for(
            papers, minimal=(_strategy == "minimal_abstracts"),
        )
        if progress:
            progress({
                "kind": "axis_plan_started", "count": len(papers),
                "message": f"正在通读 {len(papers)} 篇文献的标题与摘要,归纳全局主题轴...",
            })
        axes = _plan_theme_axes(
            papers, topic, _language,
            abstract_limit=abstract_limit, progress=progress,
        )
        groups = _assign_papers_to_axes(
            papers, axes, topic,
            abstract_limit=abstract_limit, progress=progress,
        )
        min_groups = _min_groups(len(papers))
        if len(groups) < min_groups:
            raise ValueError(
                f"主题归组后仅形成 {len(groups)} 个可验证主题，至少需要 {min_groups} 个。"
                "请检查模型返回及主题名称校验记录后重新划分。"
            )
        return _finalize_theme_groups(papers, groups, topic)

    # 分组依据:标题 + 摘要。同主题文献标题高度相似,
    # 区分「方法流派/应用情境/机制要素」的信息主要在摘要里。
    # 行首给短编号,模型输出只回显编号 —— 大池子时输出体积可控。
    index_of = {str(i + 1): p.lit_id for i, p in enumerate(papers)}
    # 主题聚类必须依据摘要证据；大池子仅缩短摘要片段，避免上下文过大。
    title_only = False
    if _strategy == "minimal_abstracts":
        abstract_limit = 250
    else:
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
    # 主题数量由文献证据决定，不再按篇数预设 3/4/5 组。
    min_groups = _min_groups(len(papers)) if _require_multiple else 1
    token_budget = _output_token_budget(papers)
    # 跨 attempt 记录最近一次 LLM 调用失败详情:minimax 单 provider 下,
    # 最终报错必须携带真实原因(超时/欠费/限流),前端才能原样展示给用户。
    last_llm_error: dict[str, str | None] = {"message": None}
    provider_error_streak = [0]

    def _ask(attempt: int) -> list[Group]:
        """attempt 1=常规;2=显式返工;3=极简输入重试。"""
        # 只提正向要求,不列举反面例子 —— 负面清单会诱导 LLM 联想到坏组名;
        # 组名不合格由代码校验链(_group_name_acceptable)兜底,无需 prompt 层禁令。
        topic_prefix_reminder = (
            f"\n\n## 组名要求\n"
            f"每个组名都要像学术综述的章节标题:简洁、完整的中文学术名词短语,"
            f"围绕主题「{topic}」,优先概括本组共同回答的研究问题或待解释对象。"
            "组名必须体现本组文献区别于其他组的具体研究对象或研究问题,"
            "不能只复述研究主题本身,也不能用「一般策略」「XX研究」「XX综述」"
            "这类可以套在整份文献池上的泛称;"
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
                "你是文献计量助手。把文献按摘要中的实际研究问题自然分组，分组数量由文献证据决定。"
                "只输出 JSON 对象,不输出任何其他文字。"
            )
            user = (
                f'主题「{topic}」的文献如下(每行:编号、标题、摘要)。'
                "按摘要中的实际研究问题自然分组，主题数量由证据决定，"
                '每篇文献恰属一组,ids 只写行首编号数字。只输出:\n'
                '{"groups": [{"name": "<组名>", "ids": [1, 2, 3]}]}'
                f"\n\n{catalog_min}"
            )
        else:
            if attempt >= 2:
                # 返工要求只在重试时出现 —— 首调就指责模型犯错会污染输出。
                rework = (
                    f"\n\n## 返工要求(第 {attempt} 次)\n"
                    "上一次没有形成可用的主题边界。请重新依据摘要中的共同研究问题分组，"
                    "不要为满足数量而硬拆或硬合。\n"
                ) + topic_prefix_reminder
            else:
                rework = topic_prefix_reminder
            system = render(
                "literature-review:classify",
                topic=topic,
                classify_mode="theme",
                papers_catalog=catalog,
                topic_language=(_language or "zh").upper(),
                dominant_language=(_language or "zh").upper(),
            )
            # catalog 只在 system 出现一次;user 只带增量指令,避免清单双份发送
            user = f"研究主题:{topic}{rework}{output_discipline}"
        import threading as _thr_ask
        _hb_stop = _thr_ask.Event()
        try:
            if progress:
                progress({"kind": "llm_attempt", "attempt": attempt, "count": len(papers)})
            _heartbeat_progress(
                progress,
                {"attempt": attempt, "papers": len(papers), "stage": "llm_call"},
                _hb_stop, interval=5.0,
            )
            try:
                raw = messages_create(
                    system=system, user=user, max_tokens=token_budget,
                    temperature=0.3 if attempt == 1 else 0.7,
                    response_format={"type": "json_object"},
                    timeout=30.0, max_retries=1,
                )
            finally:
                _hb_stop.set()
            items = _parse_group_response(raw)
            provider_error_streak[0] = 0
            logger.info(
                "主题分类响应已解析(attempt=%d, papers=%d, groups=%d, names=%s)",
                attempt, len(papers), len(items),
                [str(item.get("name") or item.get("theme") or "")[:20] for item in items[:8]],
            )
            # 逐行拆分组防护:组数 ≥ 文献数说明模型把目录每行当成一个组
            # (常见于文本抢救路径,15 篇解析出 30+ 组),这种输出没有可比较
            # 的主题边界,按无效输出处理并进入格式修复。
            if len(items) >= len(papers):
                logger.warning(
                    "主题分类解析出 %d 组(文献 %d 篇),疑似逐条拆分,按无效输出处理",
                    len(items), len(papers),
                )
                items = []
            if not items:
                logger.warning("主题分类返回空分组，原文预览=%s", str(raw).replace("\n", " ")[:500])
                # response_format 不是可靠的协议保证：模型可能返回 Markdown。
                # 在下一次分类尝试前，先用短上下文执行一次纯格式修复。
                items = _repair_invalid_group_response(
                    raw, topic=topic, papers=papers, progress=progress
                )
                if items:
                    logger.info("主题分类格式修复成功(attempt=%d, groups=%d)", attempt, len(items))
        except Exception as exc:
            last_llm_error["message"] = f"{type(exc).__name__}: {exc}"[:300]
            provider_error_streak[0] += 1
            logger.warning(
                "主题分类 LLM 调用失败(attempt=%d, papers=%d, title_only=%s, input_chars=%d): %s",
                attempt, len(papers), title_only, len(system) + len(user), exc,
            )
            if progress:
                progress({
                    "kind": "llm_error",
                    "attempt": attempt,
                    "papers": len(papers),
                    "error_type": type(exc).__name__,
                    "message": str(exc)[:500],
                })
            # 不要覆盖此前已形成的有效分组；调用方会继续进入下一轮修复。
            return []
        items = _map_group_items(items, index_of)
        groups, _ = _build_groups(
            items, valid_ids, pool_language=_pool_dominant_language(papers),
        )
        if progress:
            coverage = len({lid for group in groups for lid in group.lit_ids})
            progress({
                "kind": "llm_result",
                "attempt": attempt,
                "papers": len(papers),
                "parsed_groups": len(items),
                "valid_groups": len(groups),
                "covered": coverage,
                "decision": "accept" if coverage == len(papers) and len(groups) >= min_groups else "repair",
                "issues": (["文献编号未映射或覆盖不完整"] if coverage < len(papers) else []),
                "themes": [group.name for group in groups],
            })
        return groups

    # 每个分类池最多两次主调用；每次主调用的协议失败都会先进入格式修复。
    best_groups: list[Group] = []
    best_score: tuple[int, int, int] = (-1, -1, -1)  # (covered, valid_groups, parsed_items)

    def _score(g: list[Group]) -> tuple[int, int, int]:
        covered_ids = {lid for grp in g for lid in grp.lit_ids}
        return (len(covered_ids), sum(1 for grp in g if grp.lit_ids), 0)

    def _read_provider() -> str | None:
        try:
            from src.llm.client import last_used_provider
            return last_used_provider.get()
        except Exception:
            return None

    def _emit_attempt(
        attempt: int,
        started: float,
        result_groups: list[Group],
        error_kind: str | None,
        next_strategy_reason: str | None = None,
    ) -> None:
        if not progress:
            return
        covered = len({lid for grp in result_groups for lid in grp.lit_ids})
        elapsed = round(time.time() - started, 3)
        progress({
            "kind": "theme_attempt",
            "attempt": attempt,
            "input_scale": len(papers),
            "abstract_limit": abstract_limit,
            "chunked": False,
            "chunk_size": None,
            "provider": _read_provider(),
            "elapsed_seconds": elapsed,
            "valid_groups": sum(1 for grp in result_groups if grp.lit_ids),
            "coverage": covered,
            "expected": len(papers),
            "decision": (
                "accept"
                if (covered == len(papers) and len(result_groups) >= min_groups)
                else "repair"
            ),
            "failure_reason": error_kind,
            "next_strategy_reason": next_strategy_reason,
            "strategy": _strategy,
        })

    def _record_partial(reason: str) -> None:
        if not progress or not best_groups:
            return
        progress({
            "kind": "partial_groups",
            "reason": reason,
            "groups": [
                {"name": g.name, "lit_ids": list(g.lit_ids)} for g in best_groups
            ],
            "covered": len({lid for grp in best_groups for lid in grp.lit_ids}),
            "expected": len(papers),
            "strategy": _strategy,
        })

    failures: list[str] = []  # 记录每轮未产出有效分组的原因，用于最后发出 partial_groups
    for attempt in (1, 2, 3):
        started = time.time()
        try:
            attempt_groups = _ask(attempt)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "主题分类外层调用异常(attempt=%d, papers=%d): %s",
                attempt, len(papers), exc,
            )
            failures.append(f"attempt_{attempt}_exception:{type(exc).__name__}")
            _emit_attempt(
                attempt=attempt,
                started=started,
                result_groups=[],
                error_kind=type(exc).__name__,
            )
            continue
        if not attempt_groups:
            failures.append(f"attempt_{attempt}_empty_result")
            # minimax 单 provider:连续两次连接级失败(超时/断连),第三次
            # 大概率同样失败。直接终止,把真实错误抛给前端,不让用户白等。
            if provider_error_streak[0] >= 2:
                failures.append(f"attempt_{attempt}_provider_unavailable")
                break
        _emit_attempt(
            attempt=attempt,
            started=started,
            result_groups=attempt_groups,
            error_kind=None if attempt_groups else "empty_result",
        )
        score = _score(attempt_groups)
        if score > best_score:
            best_groups = list(attempt_groups)
            best_score = score
        covered_now, valid_now, _ = best_score
        if covered_now == len(papers) and valid_now >= min_groups:
            break

    # Step 5: 任一轮未产出有效分组且已有 best_groups 时，在返回前发出
    # partial_groups，让调用方看到划分过程的最佳部分结果而不是空集。
    if failures and best_groups:
        _record_partial(";".join(failures))

    groups = best_groups
    # 组名不合格的组剣除（其文献由 _reassign_orphans 回收，不丢文献）
    groups = [g for g in groups if _group_name_acceptable(g.name, pool_language=_language)] if groups else []
    # 先把模型漏报的文献按标题相似度回收进现有组，再做覆盖率校验：
    # 旧顺序里校验在回收之前，覆盖差 1-2 篇就整块/整池报 0 主题,
    # 把本可救回的结果整个丢弃（本次线上事故的直接原因）。
    covered = {lid for g in groups for lid in g.lit_ids}
    rest = [p.lit_id for p in papers if p.lit_id not in covered]
    _reassign_orphans(papers, rest, groups)
    coverage_short = (
        len({lid for g in groups for lid in g.lit_ids}) < len(papers)
        if groups else True
    )
    if _raise_on_coverage_failure and (
        len(groups) < min_groups or (groups and coverage_short)
    ):
        provider_note = (
            f"最近一次模型调用失败：{last_llm_error['message']}。"
            if last_llm_error["message"]
            else ""
        )
        raise ValueError(
            f"主题分类未形成可比较的主题（当前 {len(groups)} 个，至少需要 {min_groups} 个）。"
            f"最后一次分类未通过校验，解析到 {len(groups)} 个有效主题。{provider_note}"
            "请展开实时工作记录查看模型请求/响应错误，修复后重新划分。"
        )
    if not groups:
        return []
    return _finalize_theme_groups(papers, groups, topic)


def classify(papers: list[Paper], topic: str, mode: str, progress=None) -> list[Group]:
    if mode == "locale":
        return classify_by_locale(papers)
    if mode == "theme":
        return classify_by_theme(papers, topic, progress=progress)
    raise ValueError(f"unknown classify mode: {mode}")


