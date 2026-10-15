"""证据抽取模块:v9.9 新增。

把每篇文献的题名/摘要升级为 LLM 结构化证据(研究问题/理论/方法/发现/局限/
核心变量/结论极性),供主题综合(synthesis)与引用审计消费。

设计原则(与全库一致):
- 诚实性:摘要未报告的字段一律 null/"unclear",禁止凭常识补全;
- 可降级:LLM 抽取失败不阻塞写作主流程,降级为极简占位证据;
- 可追溯:每份证据带 extractor_version,便于重算与回归。
"""
from evidence.extractor import (
    EXTRACTOR_VERSION,
    POLARITY_VALUES,
    extract_pool_evidence,
    fallback_evidence,
)

__all__ = [
    "EXTRACTOR_VERSION",
    "POLARITY_VALUES",
    "extract_pool_evidence",
    "fallback_evidence",
]
