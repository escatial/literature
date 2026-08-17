"""动态综述章节规格。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class SectionSpec:
    """单个章节的规格。"""

    key: str
    title: str
    instruction: str
