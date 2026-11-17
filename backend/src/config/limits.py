"""任务级硬上限集中配置(2026-11-18 P0-4 上限显式化)。

原则:所有「防失控」的计数上限在这一个模块声明,env 可覆盖,不散落在
调用方的字面量里。此前 orchestrator/section_writer 内的 range(2) 是
隐性配置——改上限要翻代码,审计要看 git。

实现形态:上限以**函数**提供而非模块级常量——env 在每次调用时读取,
不做 import 时固化(测试与长驻进程里改 env 即生效,无陈旧值陷阱)。

超限行为由调用方决定:
- 写作侧:质量重写超限走「诚实薄稿+密度警告」出口,硬失败走「题录
  恢复稿」出口,恢复稿占比 ≥0.5 由 orchestrator 交付门禁整场拦截;
- 检索侧:命中低于 retrieval_min_pool_size() 判 RETRIEVAL_HIT_COUNT_LOW
  (见 errors.task_errors)暂停求助,不带冷清文献池硬写。
"""
from __future__ import annotations

import os


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        return default
    return value if value >= 0 else default


# P0-1:检索命中门槛。检索池低于该值判失败并给出行动指引——
# 写综述的硬下界是每主题簇 ≥5 篇可比较评述(orchestrator 既有约束),
# 主题数 × 15(theme 最小池) ÷ 命中有效率 ≈ 30 为经验地板。
_RETRIEVAL_MIN_POOL_SIZE_DEFAULT = 30


def retrieval_min_pool_size() -> int:
    return _env_int("RETRIEVAL_MIN_POOL_SIZE", _RETRIEVAL_MIN_POOL_SIZE_DEFAULT)


# P0-3:章节质量重写上限(择优记忆不变;超限保留诚实薄稿+密度警告)。
def writing_quality_retry_max() -> int:
    return _env_int("WRITING_QUALITY_RETRY_MAX", 2)


# article_lint 确定性修复轮数上限(修复函数幂等,超限保留现状如实标注)。
def writing_lint_fix_max_rounds() -> int:
    return _env_int("WRITING_LINT_FIX_MAX_ROUNDS", 2)


# 自评重写闭环轮数上限(50 分制 ≥30 达标;开关 WRITING_SELF_EVAL 另管)。
def writing_self_eval_max_rounds() -> int:
    return _env_int("WRITING_SELF_EVAL_MAX_ROUNDS", 2)
