"""核查规则、阈值与统一状态枚举。

本模块只定义"规则层面"的对象,不做实际校验。校验逻辑见:
  qa.accuracy / qa.binding / qa.quota / qa.citation_format
执行机制见:
  qa.runner
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any


class QACheckStatus(str, Enum):
    """每条核查项的执行状态。"""

    PASS = "pass"                  # 通过
    WARN = "warn"                  # 通过但需要关注
    FAIL = "fail"                  # 不通过,阻断最终输出
    SKIPPED = "skipped"            # 数据不足,跳过


def _env_int(name: str, default: int) -> int:
    """读取整数环境变量,缺失或非法时回落到默认值。"""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass
class QuotaThresholds:
    """文献数量合规性的阈值集合,可通过环境变量覆盖默认。

    默认值与 orchestrator 既有的 _REFERENCE_LIMIT_MIN / _REFERENCE_LIMIT_MAX
    (70-90 篇)、中文占比 2/3 对齐;近期文献默认要求 ≥ 60%(基于投稿综述常见要求)。
    """

    total_min: int = 70
    total_max: int = 90
    chinese_ratio_min: float = 2 / 3            # 中文文献下限占比
    english_ratio_max: float = 1 / 3            # 英文文献上限占比(指导值,非硬卡)
    core_ratio_min: float = 0.50                # 核心(高相关)文献占总入选文献的下限
    recent_years: int = 5                       # "近年"窗口
    recent_ratio_min: float = 0.60              # 近年文献占比下限

    @classmethod
    def from_env(cls) -> "QuotaThresholds":
        return cls(
            total_min=_env_int("QA_QUOTA_TOTAL_MIN", 70),
            total_max=_env_int("QA_QUOTA_TOTAL_MAX", 90),
            chinese_ratio_min=_env_float("QA_QUOTA_CN_RATIO_MIN", 2 / 3),
            english_ratio_max=_env_float("QA_QUOTA_EN_RATIO_MAX", 1 / 3),
            core_ratio_min=_env_float("QA_QUOTA_CORE_RATIO_MIN", 0.50),
            recent_years=_env_int("QA_QUOTA_RECENT_YEARS", 5),
            recent_ratio_min=_env_float("QA_QUOTA_RECENT_RATIO_MIN", 0.60),
        )


@dataclass
class QARuleSet:
    """单次完整核查的配置。

    - check_accuracy / binding / quota / citation_format: 是否启用该类核查
    - fail_fast: 任一 FAIL 是否立即中止后续检查(默认 False,产出全量报告)
    - required_pass_rate: 0-1,所有启用检查项的通过率下界(默认 1.0 = 100%)
    """

    check_accuracy: bool = True
    check_binding: bool = True
    check_quota: bool = True
    check_citation_format: bool = True
    fail_fast: bool = False
    required_pass_rate: float = 1.0
    quota: QuotaThresholds = field(default_factory=QuotaThresholds)
    citation_style: str = "china-national-standard-gb-t-7714-2025-numeric"

    @classmethod
    def default(cls) -> "QARuleSet":
        return cls(quota=QuotaThresholds.from_env())

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        # QuotaThresholds 已是 dataclass,asdict 会嵌套处理;保留原样
        return data


default_rule_set: QARuleSet = QARuleSet.default()
