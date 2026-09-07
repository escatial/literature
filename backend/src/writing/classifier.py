"""综述分类器:按国内外 / 按主题 对文献分组。

使用 prompts/literature-review.md 模板中的 `classify` 段作为 system prompt。
"""
from __future__ import annotations

import json as _json
import logging
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
# 有实际学术含义的英文缩写,允许单独作为组名主体(统一大写存储)
_EN_ACRONYM_ALLOWLIST = frozenset({
    "AI", "IOT", "MSC", "ERP", "B2B", "O2O", "SNS", "GDPR",
})
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
# 组名长度上限:超出 12 字的 LLM 描述倾向堆砌,不再像标题
_GROUP_NAME_MAX_LEN = 12


def _group_name_acceptable(name: str) -> bool:
    """组名是否可作为学术综述章节标题。

    硬约束:
      - 1~12 字
      - 不是通用兜底名("其他"/"杂项"/"misc")
      - 不是叙述性短语("近年来"/"然而"/"综上所述" 等)
      - 不是英文碎片(纯英文单词或"英文单词+相关研究"式拼接)
    """
    name = (name or "").strip()
    if not name:
        return False
    if name in _GENERIC_GROUP_NAMES or name in _NARRATIVE_GROUP_NAMES:
        return False
    if len(name) > _GROUP_NAME_MAX_LEN:
        return False
    if _LATIN_ONLY_GROUP_RE.fullmatch(name):
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
    return name.strip()


def _groups_acceptable(groups: list[Group]) -> bool:
    """LLM 分组结果是否全部可接受(数量达标 + 组名合格)。"""
    return bool(groups) and all(_group_name_acceptable(g.name) for g in groups)


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


def _parse_group_response(raw: str) -> list[dict]:
    data = parse_llm_json(raw)
    if isinstance(data, dict) and "groups" in data:
        return [item for item in data["groups"] if isinstance(item, dict)]
    # json_object 失效时模型常输出「分析过程 + JSON」混合文本,先抢救
    salvaged = _salvage_groups_json(raw)
    if salvaged:
        return salvaged
    # 输出被 max_tokens 截断(JSON 未闭合):抢救已完整写出的分组
    truncated = _salvage_truncated_groups(raw)
    if truncated:
        return truncated
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    return []


def _build_groups(items: list[dict], valid_ids: set[str]) -> tuple[list[Group], set[str]]:
    covered: set[str] = set()
    groups: list[Group] = []
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
        covered.update(ids)
        groups.append(Group(name=name, lit_ids=ids))
    return groups, covered


def _min_groups(n_papers: int) -> int:
    """组数下限(单一来源,classify 与 QC agent 共用):
    ≥30 篇至少 4 组;9~29 篇至少 3 组;<9 篇 1 组即可。
    """
    if n_papers >= 30:
        return 4
    if n_papers >= 9:
        return 3
    return 1


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


def classify_by_theme(papers: list[Paper], topic: str) -> list[Group]:
    """主题分类:LLM 将文献归入若干主题。

    强约束(代码层兜底):
      - 组数下限见 _min_groups(≥30 篇至少 4 组,9~29 篇至少 3 组);
      - 三次调用逐级升级:常规(0.3) → 显式返工(0.7) → 极简输入(0.7);
      - 输出用「编号协议」(模型只回显行首编号,不抄 lit_id),大池子不再截断;
      - 输出万一仍被截断,抢救已完整的分组,缺组文献按标题相似度归入;
      - 组名不合格的组剔除,其文献按标题相似度归入最相近的合格组;
      - LLM 彻底失败时,单组全量确定性兜底(组名 = 清洗后的主题名)。
    """
    if not papers:
        return []

    # 分组依据:标题 + 摘要节选。同主题文献标题高度相似,
    # 区分「方法流派/应用情境/机制要素」的信息主要在摘要里。
    # 行首给短编号,模型输出只回显编号 —— 大池子时输出体积可控。
    index_of = {str(i + 1): p.lit_id for i, p in enumerate(papers)}
    catalog = "\n".join(
        f"- #{i + 1} | {p.title} | {p.year or 'N/A'} | 摘要:{(p.abstract or '').strip()[:120]}..."
        for i, p in enumerate(papers)
    )
    valid_ids = {p.lit_id for p in papers}
    min_groups = _min_groups(len(papers))
    token_budget = _output_token_budget(papers)

    def _ask(attempt: int) -> list[Group]:
        """attempt 1=常规;2=显式返工(升温+强制组数);3=极简输入兜底。"""
        # 只提正向要求,不列举反面例子 —— 负面清单会诱导 LLM 联想到坏组名;
        # 组名不合格由代码校验链(_group_name_acceptable)兜底,无需 prompt 层禁令。
        topic_prefix_reminder = (
            f"\n\n## 组名要求\n"
            f"每个组名都要像学术综述的章节标题:简洁的中文学术名词短语(4-10 字),"
            f"围绕主题「{topic}」,按研究对象/方法/视角/应用情境等维度归纳本组的研究角度。"
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
            # 第三道防线:极简输入 + 一句话指令。
            # 输入只剩编号+标题,模型没有「逐篇分析」的发挥空间;
            # 输出只含编号数字,体积比 lit_id 小一个数量级。
            catalog_min = "\n".join(
                f"#{i + 1} {p.title[:60]}" for i, p in enumerate(papers)
            )
            system = (
                "你是文献计量助手。把文献按研究主题分成 3-5 组。"
                "只输出 JSON 对象,不输出任何其他文字。"
            )
            user = (
                f'主题「{topic}」的文献如下(每行:编号 标题)。'
                f"按研究主题分为 {min_groups}-5 组,组名为 4-10 字中文学术名词短语,"
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
            raw = messages_create(
                system=system, user=user, max_tokens=token_budget,
                temperature=0.3 if attempt == 1 else 0.7,
                response_format={"type": "json_object"},
            )
            items = _parse_group_response(raw)
        except Exception as exc:
            logger.warning("主题分类 LLM 调用失败: %s", exc)
            return []
        items = _map_group_items(items, index_of)
        groups, _ = _build_groups(items, valid_ids)
        return groups

    groups = _ask(1)
    if len(groups) < min_groups:
        groups = _ask(2)
    if len(groups) < min_groups:
        logger.warning("主题分类前两次调用未达标,启用极简输入第三次重试")
        groups = _ask(3)
    # 组名不合格的组剔除(其文献由 _reassign_orphans 回收,不丢文献)
    groups = [g for g in groups if _group_name_acceptable(g.name)] if groups else []
    if not groups:
        # LLM 彻底失败(接口不可用/连续输出废品):确定性兜底 ——
        # 单组全量,组名用清洗后的主题名。
        # 不做关键词聚类:主题锚词理应由 LLM 判定,
        # 代码层正则切词凑出的组名质量更差。
        logger.warning(
            "主题分类三次调用均未产出有效分组(输入 %d 篇),降级单组兜底",
            len(papers),
        )
        fallback_name = _clean_group_name(topic) or "研究综述"
        return [Group(name=fallback_name, lit_ids=[p.lit_id for p in papers])]

    # 无组文献按标题相似度归入最相近的组(避免漏 paper,不再倒进最后一组)
    covered = {lid for g in groups for lid in g.lit_ids}
    rest = [p.lit_id for p in papers if p.lit_id not in covered]
    _reassign_orphans(papers, rest, groups)
    return groups


def classify(papers: list[Paper], topic: str, mode: str) -> list[Group]:
    if mode == "locale":
        return classify_by_locale(papers)
    if mode == "theme":
        return classify_by_theme(papers, topic)
    raise ValueError(f"unknown classify mode: {mode}")
