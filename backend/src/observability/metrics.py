"""最小观测埋点(方案 §9 MVP-4 "观测")。

设计:
- 不引入 Prometheus / OpenTelemetry 重型依赖;
- 暴露 metrics 内存计数器 + JSON dump 接口;
- 关键指标:任务总数、各状态阶段进入次数、外部调用重试次数、LLM token 估算。
"""
from __future__ import annotations

import json
import logging
import threading
from collections import Counter

log = logging.getLogger(__name__)


class Metrics:
    """进程内指标收集器(单例)。"""

    _instance: "Metrics | None" = None
    _lock = threading.Lock()

    def __init__(self):
        self._counters: Counter[str] = Counter()
        self._histograms: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    @classmethod
    def instance(cls) -> "Metrics":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def inc(self, name: str, amount: int = 1, **labels) -> None:
        key = self._key(name, labels)
        with self._lock:
            self._counters[key] += amount

    def observe(self, name: str, value: float, **labels) -> None:
        key = self._key(name, labels)
        with self._lock:
            self._histograms.setdefault(key, []).append(value)
            # 限制长度,避免内存爆炸
            if len(self._histograms[key]) > 10000:
                self._histograms[key] = self._histograms[key][-5000:]

    @staticmethod
    def _key(name: str, labels: dict) -> str:
        if not labels:
            return name
        return f"{name}{{{','.join(f'{k}={v}' for k, v in sorted(labels.items()))}}}"

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "counters": dict(self._counters),
                "histograms": {
                    k: {
                        "count": len(v),
                        "avg": (sum(v) / len(v)) if v else 0.0,
                        "max": max(v) if v else 0.0,
                        "p95": sorted(v)[int(len(v) * 0.95)] if v else 0.0,
                    }
                    for k, v in self._histograms.items()
                },
            }

    def to_json(self) -> str:
        return json.dumps(self.snapshot(), ensure_ascii=False, indent=2)

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._histograms.clear()


# 业务指标
M_TASKS_TOTAL = "lit_review.tasks.total"
M_STAGE_ENTERED = "lit_review.stage.entered"
M_RETRIES = "lit_review.external.retries"
M_RENDER_OK = "lit_review.render.ok"
M_RENDER_FAIL = "lit_review.render.fail"
M_HUMANIZE_ACCEPT = "lit_review.humanize.accept"
M_HUMANIZE_REJECT = "lit_review.humanize.reject"


__all__ = ["Metrics", "M_TASKS_TOTAL", "M_STAGE_ENTERED", "M_RETRIES", "M_RENDER_OK", "M_RENDER_FAIL", "M_HUMANIZE_ACCEPT", "M_HUMANIZE_REJECT"]