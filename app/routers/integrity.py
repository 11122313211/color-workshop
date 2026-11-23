"""数据完整性审计与修复路由（V0.11）。

- GET  /api/integrity/report    只读扫描报告
- POST /api/integrity/repair    修复预览/应用（body {dry_run, actions}，默认 dry_run=true）
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..models.schemas import IntegrityRepairRequest
from ..services.integrity import build_report, repair

router = APIRouter(prefix="/api/integrity", tags=["integrity"])


@router.get("/report")
def integrity_report():
    """扫描 active/archived 工艺、孤儿记录、缺失与未引用 ICC。"""
    return build_report()


@router.post("/repair")
def integrity_repair(req: IntegrityRepairRequest | None = None):
    """按策略修复；默认 dry_run=true，仅显式 false 才写盘。"""
    request = req or IntegrityRepairRequest()
    try:
        return repair(dry_run=request.dry_run, actions=request.actions)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
