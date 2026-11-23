"""光谱/同色异谱路由（V0.10）。

- POST /api/spectral/metamerism
- POST /api/spectral/compare
- GET  /api/spectral/sessions/{sid}/summary
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..models.schemas import (
    SpectralCompareRequest,
    SpectralCompareResponse,
    SpectralMetamerismRequest,
    SpectralMetamerismResponse,
)
from ..services.measurements import MeasurementStore
from ..services.spectral import compare_sessions, metamerism, session_summary

router = APIRouter(prefix="/api/spectral", tags=["spectral"])

_measurements = MeasurementStore()


@router.post("/metamerism", response_model=SpectralMetamerismResponse)
def spectral_metamerism(req: SpectralMetamerismRequest):
    try:
        return metamerism(
            req.spectral_a,
            req.spectral_b,
            conditions=req.conditions,
            label_a=req.label_a,
            label_b=req.label_b,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/compare", response_model=SpectralCompareResponse)
def spectral_compare(req: SpectralCompareRequest):
    session_a = _measurements.get_session(req.session_a)
    if session_a is None:
        raise HTTPException(404, "主测量会话不存在")
    session_b = _measurements.get_session(req.session_b)
    if session_b is None:
        raise HTTPException(404, "对比测量会话不存在")
    try:
        return compare_sessions(session_a, session_b, top_n=req.top_n)
    except ValueError as exc:
        raise HTTPException(422, f"光谱比对失败：{exc}") from exc


@router.get("/sessions/{sid}/summary")
def spectral_session_summary(sid: str):
    session = _measurements.get_session(sid)
    if session is None:
        raise HTTPException(404, "测量会话不存在")
    return session_summary(session, _measurements.list_sessions())
