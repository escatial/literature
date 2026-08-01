"""7 章综述模板定义。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class SectionSpec:
    """单个章节的规格。"""

    key: str
    title: str
    instruction: str


SECTIONS: list[SectionSpec] = [
    SectionSpec(
        key="introduction",
        title="一、引言",
        instruction=(
            "说明研究主题的背景与意义，交代综述的写作目的与范围。"
            "此处不引用具体文献。"
        ),
    ),
    SectionSpec(
        key="method",
        title="二、文献检索方法",
        instruction=(
            "客观描述文献来源（英文来自 OpenAlex 等开放数据库，中文来自知网等平台的"
            "用户导入）、检索词与筛选标准。此处不引用具体文献。"
        ),
    ),
    SectionSpec(
        key="overview",
        title="三、文献概览",
        instruction=(
            "从发表年份分布、来源期刊/会议、国内外占比等维度对所纳入文献做整体描述。"
            "引用文献时必须使用 [lit_xxx] 锚点。"
        ),
    ),
    SectionSpec(
        key="themes",
        title="四、主题分析",
        instruction=(
            "按研究主题/流派归纳各文献的核心观点，比较不同研究之间的异同。"
            "每个论点必须挂在 [lit_xxx] 锚点上，禁止无锚点陈述他人观点。"
        ),
    ),
    SectionSpec(
        key="methods",
        title="五、方法学分析",
        instruction=(
            "梳理各文献采用的研究方法（实证、案例、问卷、实验、模型等），"
            "评价方法选择的合理性与局限。引用必须使用 [lit_xxx] 锚点。"
        ),
    ),
    SectionSpec(
        key="gaps",
        title="六、研究缺口与未来方向",
        instruction=(
            "基于前述分析指出当前研究的空白、矛盾点与不足，提出未来研究方向。"
            "凡涉及已有研究的表述必须使用 [lit_xxx] 锚点。"
        ),
    ),
    SectionSpec(
        key="conclusion",
        title="七、结论",
        instruction="总结全文，概括主要发现。此处不新增文献引用。",
    ),
]


def get_section(key: str) -> SectionSpec:
    for s in SECTIONS:
        if s.key == key:
            return s
    raise KeyError(f"unknown section key: {key}")
