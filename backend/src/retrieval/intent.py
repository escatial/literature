"""结构化检索意图:LLM 唯一合法产物,与具体数据库无关。

设计原则:
- LLM 不直接生成任何 DB 方言(query_en / query_zh),只填本 schema;
- 各 AcademicSource 实现负责把本 schema 翻译成 OpenAlex / PubMed / CNKI 方言;
- 不含任何领域词表,所有概念/同义词由 LLM 在规划时生成;
- 失败不允许"启发式兜底",LLM 必须按 schema 重出。
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator


SearchField = Literal["title", "title_abstract", "abstract"]
LoopOperator = Literal["AND", "OR"]


class Concept(BaseModel):
    """一个检索概念:一个核心子主题 + 若干英文同义词 + 检索字段 + 权重。"""

    id: str = Field(..., description="A/B/C... 短标识,供 boolean_template 引用")
    label_en: str = Field(..., min_length=2, description="该概念的英文短标签")
    label_zh: str = Field(default="", description="该概念的中文短标签")
    synonyms_en: list[str] = Field(
        default_factory=list,
        description="英文同义词,覆盖学界常见说法,2-4 个",
    )
    synonyms_zh: list[str] = Field(
        default_factory=list,
        description="中文同义词,覆盖学界常见说法,2-4 个",
    )
    field: SearchField = Field(
        default="title_abstract",
        description="该概念检索的目标字段",
    )

    @model_validator(mode="before")
    @classmethod
    def _coerce_field(cls, data):
        """LLM 偶发填非法 field(如 "Algorithms");落 title_abstract 不阻塞链路。"""
        if isinstance(data, dict) and data.get("field") not in ("title", "title_abstract", "abstract"):
            data["field"] = "title_abstract"
        return data

    @model_validator(mode="before")
    @classmethod
    def _coerce_label_en(cls, data):
        """LLM 偶发给空 label_en;用第一个英文同义词兜底(同义词才是检索核心)。"""
        if isinstance(data, dict):
            label_en = (data.get("label_en") or "").strip()
            syns = data.get("synonyms_en") or []
            if not label_en and syns and isinstance(syns[0], str) and syns[0].strip():
                data["label_en"] = syns[0].strip()
        return data

    @model_validator(mode="before")
    @classmethod
    def _coerce_weight(cls, data):
        """LLM 偶发填超范围 weight(如 5)或字符串语义值(如 'high'/'primary');
        clamp 到 [0,2],字符串按语义映射,不阻塞链路。"""
        if isinstance(data, dict):
            w = data.get("weight")
            if isinstance(w, (int, float)):
                data["weight"] = max(0.0, min(2.0, float(w)))
            elif w is not None and not isinstance(w, bool):
                # 字符串或任意非数值:先试数字,再按语义映射,兜底默认 1.0
                mapping = {"high": 1.5, "medium": 1.0, "low": 0.5,
                           "primary": 1.5, "normal": 1.0, "secondary": 0.8}
                try:
                    data["weight"] = max(0.0, min(2.0, float(w)))
                except (TypeError, ValueError):
                    data["weight"] = mapping.get(str(w).lower().strip(), 1.0)
        return data

    weight: float = Field(
        default=1.0,
        ge=0.0,
        le=2.0,
        description="相关性评分权重,1.0 为中性",
    )

    @model_validator(mode="after")
    def _check_synonyms(self):
        if len(self.synonyms_en) < 1:
            raise ValueError(f"Concept {self.id} 至少需要 1 个英文同义词")
        return self


class HardFilters(BaseModel):
    """硬筛选规则:在 LLM 筛选之前由控制器确定性执行,不消耗 LLM 调用。"""

    min_year: int | None = Field(default=None, ge=1900, le=2100)
    max_year: int | None = Field(default=None, ge=1900, le=2100)
    language: list[str] = Field(default_factory=lambda: ["en"])
    require_abstract: bool = True
    allowed_types: list[str] = Field(
        default_factory=lambda: ["article", "review"],
        description="OpenAlex type 字段:article/review/book-chapter/editorial/letter...",
    )
    min_citations: int = 0
    require_doi: bool = False
    require_peer_reviewed: bool = False

    @model_validator(mode="after")
    def _year_range(self):
        if (self.min_year is None) != (self.max_year is None):
            raise ValueError("min_year 与 max_year 必须同时设置或同时为空")
        if self.min_year is not None and self.max_year is not None and self.max_year < self.min_year:
            raise ValueError("max_year 必须 >= min_year")
        return self


class SnowballConfig(BaseModel):
    """雪球检索配置。确定性执行,无 LLM 参与。"""

    enabled: bool = False
    forward_depth: int = Field(default=0, ge=0, le=2, description="几层前向引用")
    backward_depth: int = Field(default=1, ge=0, le=2, description="几层后向引用")
    max_seeds: int = Field(default=100, ge=1, le=1000)
    max_results: int = Field(default=500, ge=1, le=5000)

    @model_validator(mode="before")
    @classmethod
    def _coerce_zero(cls, data):
        """LLM 偶发把 max_seeds/max_results 填 0(语义=不启用雪球);
        schema 要求 >=1,clamp 到默认值,不阻塞链路。"""
        if isinstance(data, dict):
            if data.get("max_seeds") in (0, None):
                data["max_seeds"] = 100
            if data.get("max_results") in (0, None):
                data["max_results"] = 500
        return data


class LoopConfig(BaseModel):
    """循环控制器配置:每个数据源独立的翻页/停止规则。"""

    max_pages_per_source: int = Field(default=20, ge=1, le=200)
    max_results_per_source: int = Field(default=5000, ge=1, le=50000)
    stop_on_consecutive_empty: int = Field(default=3, ge=1, le=10)
    per_page: int = Field(default=50, ge=10, le=200)


class SearchIntent(BaseModel):
    """LLM 输出的结构化检索意图,与具体数据库无关。

    每个 AcademicSource.build_query(intent) 负责翻译成本地语法;
    LLM 永远不需要知道 OpenAlex filter 表达式或 PubMed MeSH。
    """

    topic_summary: str = Field(..., min_length=10, description="一句英文研究问题")
    # 下限 1:LLM 规划要求 3~5,偶发只给 1 个概念时也接受(不启发式补全,
    # 宁宽勿挂);零结果自动降级仍有 relax_intent 保证最小 2 概念才降级
    concepts: list[Concept] = Field(..., min_length=1, max_length=5)
    boolean_template: str = Field(
        ...,
        description='概念组合模板,如 "(A) AND (B) AND (C)",A/B/C 对应 concepts.id',
    )
    exclude_terms: list[str] = Field(default_factory=list)
    filters: HardFilters = Field(default_factory=HardFilters)
    snowball: SnowballConfig = Field(default_factory=SnowballConfig)
    loop: LoopConfig = Field(default_factory=LoopConfig)

    @model_validator(mode="after")
    def _validate_template_references(self):
        ids = {c.id for c in self.concepts}
        referenced = set()
        import re
        for m in re.finditer(r"\b([A-Z])\b", self.boolean_template):
            referenced.add(m.group(1))
        missing = referenced - ids
        if missing:
            raise ValueError(f"boolean_template 引用了未定义的概念: {sorted(missing)}")
        unused = ids - referenced
        if unused:
            raise ValueError(f"boolean_template 未引用的概念: {sorted(unused)}")
        return self


def core_concepts(intent: SearchIntent, max_count: int = 2) -> list[Concept]:
    """取检索必须保留的核心概念(LLM 规划的前 max_count 个,即研究对象/主体)。

    与 relax_intent 的核心定义(concepts[:2])保持一致,供各数据源 build_query
    压缩检索式时统一口径:OpenAlex 对布尔操作符有 <=5 硬上限,PubMed 等无上限
    的源也用它避免"多概念全 AND 交集过窄导致零结果"。
    """
    return list(intent.concepts[:max_count])


def relax_intent(intent: SearchIntent) -> SearchIntent | None:
    """生成放宽后的 SearchIntent 副本;无法再放宽时返回 None。

    放宽策略(按优先级,确定性执行,不调 LLM):
    1. 去掉"非核心概念"里 weight 最小的一个(核心=前两个概念,即研究对象/主体;
       至少保留 2 个概念,并同步重写 boolean_template);
    2. 已只剩核心 2 概念但仍零结果时,先清空 exclude_terms 重试——排除词往往
       恰好把仅有的少量相关文献全部排掉(如 PubMed 上"无人机×地面车"几乎全是
       农业应用,排掉 agriculture 后从 17 篇骤降到 1 篇),比放宽年份更合理;
    3. 再把 min_year 往前放宽 5 年(下限 1995);
    4. 仍无法放宽 -> None,由调用方决定放弃。

    供检索循环在"某源零结果"时自动降级重检使用。
    """
    import copy
    import re

    if len(intent.concepts) > 2:
        relaxed = copy.deepcopy(intent)
        core = {c.id for c in relaxed.concepts[:2]}
        drop = min(
            (c for c in relaxed.concepts if c.id not in core),
            key=lambda c: (c.weight, len(c.synonyms_en)),
        )
        relaxed.concepts = [c for c in relaxed.concepts if c.id != drop.id]
        # 从 "(A) AND (B) AND (C)" 中移除被删概念的组及其 AND
        pattern = re.compile(
            rf"\s*AND\s*\(\s*{drop.id}\s*\)|\(\s*{drop.id}\s*\)\s*AND\s*"
        )
        relaxed.boolean_template = re.sub(
            r"\s{2,}", " ",
            pattern.sub("", relaxed.boolean_template),
        ).strip()
        # 重建实例触发引用一致性校验
        return SearchIntent(
            topic_summary=relaxed.topic_summary,
            concepts=relaxed.concepts,
            boolean_template=relaxed.boolean_template,
            exclude_terms=relaxed.exclude_terms,
            filters=relaxed.filters,
            snowball=relaxed.snowball,
            loop=relaxed.loop,
        )

    if intent.exclude_terms:
        relaxed = copy.deepcopy(intent)
        relaxed.exclude_terms = []
        return SearchIntent(
            topic_summary=relaxed.topic_summary,
            concepts=relaxed.concepts,
            boolean_template=relaxed.boolean_template,
            exclude_terms=relaxed.exclude_terms,
            filters=relaxed.filters,
            snowball=relaxed.snowball,
            loop=relaxed.loop,
        )

    if intent.filters.min_year is not None and intent.filters.min_year > 1995:
        relaxed = copy.deepcopy(intent)
        relaxed.filters.min_year = max(1995, intent.filters.min_year - 5)
        return relaxed

    return None


__all__ = [
    "SearchField",
    "LoopOperator",
    "Concept",
    "HardFilters",
    "SnowballConfig",
    "LoopConfig",
    "SearchIntent",
    "core_concepts",
]
