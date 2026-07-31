"""中文批量导入 API。/api/import/cn

一次请求支持多条 GB/T 7714 引文,逐行解析。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from import_cn.parser import parse_batch

router = APIRouter()


class ImportCnRequest(BaseModel):
    raw_text: str = Field(..., description="用户一次性粘贴的多行 GB/T 7714 引文")


class ImportCnResponse(BaseModel):
    total: int
    parsed_ok: int
    parsed_fail: int
    citations: list[dict]


@router.post("/import/cn", response_model=ImportCnResponse)
async def import_cn(req: ImportCnRequest):
    """批量解析 GB/T 7714-2025 引文。

    返回每条的 parsed_ok 标志,前端可显示成功/失败列表。
    解析成功的 raw_text 保留原样,后续供用户原样存入文献池。
    """
    if not req.raw_text.strip():
        raise HTTPException(400, "请粘贴至少一条引文")

    lines = req.raw_text.splitlines()
    results = parse_batch(lines)
    parsed_ok = sum(1 for r in results if r.parsed_ok)
    return ImportCnResponse(
        total=len(results),
        parsed_ok=parsed_ok,
        parsed_fail=len(results) - parsed_ok,
        citations=[r.to_dict() for r in results],
    )
