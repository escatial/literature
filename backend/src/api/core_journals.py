"""核心期刊筛选 API。

数据源:
  - 北大《中文核心期刊要目总览》(2023 年版)
  - 中国科学引文数据库来源期刊列表(2025-2026 年度)

匹配在后端完成(无需落库);筛选逻辑落到 GET /papers 的查询参数。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter

import core_journals as _cj

log = logging.getLogger(__name__)
router = APIRouter(prefix="/core-journals", tags=["core-journals"])


@router.get("/stats")
def get_stats() -> dict:
    """当前加载的核心期刊数量(诊断/前端展示用)。"""
    try:
        return _cj.stats()
    except Exception as exc:
        log.warning("核心期刊加载失败: %s", exc)
        return {"pku_core": 0, "cscd_core": 0, "cscd_ext": 0, "all": 0, "error": str(exc)}


@router.post("/reload")
def reload() -> dict:
    """清除缓存并重新加载(用于 xlsx 文件被替换后)。"""
    try:
        _cj.invalidate_cache()
        return _cj.stats()
    except Exception as exc:
        log.error("核心期刊重载失败: %s", exc)
        return {"error": str(exc)}
