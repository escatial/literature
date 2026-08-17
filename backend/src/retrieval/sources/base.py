"""学术数据源协议 + 共享类型。

所有数据源(OpenAlex / PubMed / CNKI...)实现同一 AcademicSource 接口,
使控制器/任务层能用统一代码驱动任意数据源组合。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from retrieval.intent import SearchIntent


@dataclass
class SourcePage:
    """单页查询结果。total / has_next 由数据源自身在第一页估算或实时更新。"""

    papers: list  # list[Paper]
    total: int          # 该查询的总命中数(数据源报告)
    has_next: bool      # 是否还有下一页
    page: int           # 当前页码(1-based)
    raw_query: dict     # 实际发给数据源的参数(便于排错/调试)


@runtime_checkable
class AcademicSource(Protocol):
    """学术数据源协议。所有数据源必须实现以下方法。

    设计原则:
    - build_query 是纯函数(零网络调用),便于测试和复用;
    - execute 必须支持按页翻页,且返回 has_next 用于循环停止;
    - fetch_abstract_if_missing 是可选的异步回填钩子;
    - fetch_references 用于雪球(后向引用);
    - health_check 用于控制器在循环开始前跳过不可用源。
    """

    name: str

    def build_query(self, intent: SearchIntent) -> dict:
        """把 SearchIntent 翻译成本源查询参数(纯函数,无副作用)。"""
        ...

    def execute(self, query: dict, page: int, per_page: int) -> SourcePage:
        """执行一页查询,返回 SourcePage。"""
        ...

    def fetch_abstract_if_missing(self, paper) -> "object | None":
        """补全缺失的摘要。返回更新后的 Paper 或 None(无法补全)。"""
        ...

    def fetch_references(self, paper, depth: int = 1) -> list:
        """雪球:取该 paper 引用的前 N 层参考文献。"""
        ...

    def health_check(self) -> bool:
        """源是否健康(可连接)。用于控制器静默降级。"""
        ...


__all__ = ["SourcePage", "AcademicSource"]
