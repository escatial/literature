"""写作模块配置项。

- 默认值在源码中以中文常量提供
- 测试/部署可以通过环境变量覆盖
- 写作模块不再硬编码业务文案
"""

from __future__ import annotations

import os


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value


SECTION_COMMENT_TITLE: str = _env(
    "WRITING_SECTION_COMMENT_TITLE",
    "文献述评",
)

SECTION_COMMENT_INSTRUCTION: str = _env(
    "WRITING_SECTION_COMMENT_INSTRUCTION",
    "综合评价上述主题研究，指出研究空白和本文切入点。此处不新增文献引用。",
)

SECTION_THEME_INSTRUCTION_TEMPLATE: str = _env(
    "WRITING_SECTION_THEME_INSTRUCTION_TEMPLATE",
    "围绕『{name}』归纳相关文献的核心观点、共识、分歧与不足。"
    "本节只讨论该并列主题，不要写数据库来源或筛选流程。",
)

SECTION_LOCALE_INSTRUCTION_TEMPLATE: str = _env(
    "WRITING_SECTION_LOCALE_INSTRUCTION_TEMPLATE",
    "围绕『{name}』相关文献的核心观点进行归纳与对比。本节只讨论该分组文献，"
    "不要写数据库来源或筛选流程。",
)

LOCALE_GROUP_DOMESTIC: str = _env(
    "WRITING_LOCALE_GROUP_DOMESTIC",
    "国内研究",
)

LOCALE_GROUP_FOREIGN: str = _env(
    "WRITING_LOCALE_GROUP_FOREIGN",
    "国外研究",
)