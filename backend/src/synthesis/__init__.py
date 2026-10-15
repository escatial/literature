"""主题级综合推理模块(v9.9 新增,方案 M3)。

在主题分类之后、分章写作之前,按主题产出结构化的共识/争议/方法差异/研究
空白四类条目,作为写作素材注入 section prompt,并渲染《研究结论比较表》。

设计原则:
- 综合层的每个条目必须绑定主题池内的真实文献(数字编号回填,防截断幻觉);
- "争议原因"是推断不是事实,prompt 层强制假设语气,渲染层标注 type=hypothesis;
- 研究空白区分 confirmed(有证据的空白)与 not_retrieved(仅未检索到),
  对齐参考方案 §11 第 10 条(不得把"没有检索到"表述为"尚无研究");
- 单主题综合失败不阻塞整体,降级为跳过该主题。
"""
from synthesis.agent import (
    SynthesisError,
    build_all_syntheses,
    build_theme_synthesis,
    synthesis_enabled,
)

__all__ = [
    "SynthesisError",
    "build_all_syntheses",
    "build_theme_synthesis",
    "synthesis_enabled",
]
