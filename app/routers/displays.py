"""显示器档案与软打样门禁路由（V0.9 · P0-4）。

- POST   /api/displays                          新建显示器档案（可带首次校准确认）
- GET    /api/displays                          列表（含校准状态）
- GET    /api/displays/active-status            当前激活显示器的门禁状态
- GET    /api/displays/{id}                     详情（含状态与校准历史）
- PATCH  /api/displays/{id}                     更新档案 / 目标值
- DELETE /api/displays/{id}                     删除档案
- POST   /api/displays/{id}/activate            设为当前显示器（互斥）
- POST   /api/displays/{id}/calibrations        追加校准确认（更新当前校准）
- GET    /api/displays/{id}/status              校准状态
- GET    /api/displays/{id}/report              校准档案报告（JSON 留痕）
- POST   /api/displays/{id}/measurements        导入屏幕测量（raw CGATS.17 / TSV）
- GET    /api/displays/{id}/measurements/latest 原样导出最近测量文件
"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response

from ..models.schemas import (
    DisplayCalibrationRequest,
    DisplayCreateRequest,
    DisplayUpdateRequest,
)
from ..services.displays import DisplayStore, calibration_status

router = APIRouter(prefix="/api/displays", tags=["displays"])

_store = DisplayStore()


def _with_status(doc: dict) -> dict:
    return {**doc, "status": calibration_status(doc)}


def _get_or_404(display_id: str) -> dict:
    doc = _store.get(display_id)
    if doc is None:
        raise HTTPException(404, "显示器档案不存在")
    return doc


@router.post("")
def create_display(req: DisplayCreateRequest):
    try:
        doc = _store.create(req.model_dump())
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    return _with_status(doc)


@router.get("")
def list_displays():
    return [_with_status(doc) for doc in _store.list()]


@router.get("/active-status")
def active_display_status():
    """软打样门禁状态（未登记 / 未校准 / 即将到期 / 过期 / 有效）。"""
    return _store.active_status()


@router.get("/{display_id}")
def get_display(display_id: str):
    return _with_status(_get_or_404(display_id))


@router.patch("/{display_id}")
def update_display(display_id: str, req: DisplayUpdateRequest):
    try:
        doc = _store.update(display_id, req.model_dump(exclude_unset=True))
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    return _with_status(doc)


@router.delete("/{display_id}")
def delete_display(display_id: str):
    _get_or_404(display_id)
    _store.delete(display_id)
    return {"status": "deleted"}


@router.post("/{display_id}/activate")
def activate_display(display_id: str):
    try:
        doc = _store.activate(display_id)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    return _with_status(doc)


@router.post("/{display_id}/calibrations")
def add_calibration(display_id: str, req: DisplayCalibrationRequest):
    try:
        doc = _store.add_calibration(display_id, req.model_dump())
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    return _with_status(doc)


@router.get("/{display_id}/status")
def display_status(display_id: str):
    try:
        return _store.status(display_id)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e


@router.get("/{display_id}/report")
def display_report(display_id: str):
    try:
        return _store.report(display_id)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e


@router.post("/{display_id}/measurements")
async def import_display_measurement(
    display_id: str,
    request: Request,
    filename: str = Query("", description="测量文件名"),
    white_luminance_cd_m2: float | None = Query(None, description="白场绝对亮度（可选，cd/m²）"),
):
    """导入屏幕测量文件（raw CGATS.17 / TSV，需含 XYZ 白/黑色块）。"""
    raw = await request.body()
    try:
        doc = _store.attach_measurement(display_id, raw, filename, white_luminance_cd_m2)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    return _with_status(doc)


@router.get("/{display_id}/measurements/latest")
def export_display_measurement(display_id: str):
    """原样导出最近一次屏幕测量文件（CGATS/TSV），供外部校准工具复核。"""
    try:
        raw, filename, digest = _store.measurement_export(display_id)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    return Response(
        content=raw,
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}",
            "X-Content-SHA256": digest,
        },
    )