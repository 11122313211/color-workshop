"""API 路由层：色彩管理闭环产品的所有端点。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..algorithms import convert_color, delta_e, create_srgb_profile
from ..models.schemas import (
    DeviceCreate, DeviceOut, DeviceUpdate,
    Measurement, MeasurementOut,
    ConvertRequest, ConvertResponse,
    CorrectionRequest, CorrectionResponse,
    DeltaERequest, DeltaEResponse, LabDeltaERequest,
    ClosedLoopRequest, ClosedLoopResponse,
    ColorCheckerDetectRequest, ColorCheckerDetectResult,
    GamutVisualizationResponse, SpectrumLocusResponse,
)
from ..services.store import (
    DeviceStore, MeasurementStore, WorkflowStore,
    run_correction, run_closed_loop, run_color_checker_detection, run_delta_e,
)
from ..services.visualization import gamut_visualization_data, delta_e_distribution

# 全局存储（MVP 内存存储）
device_store = DeviceStore()
measurement_store = MeasurementStore()
workflow_store = WorkflowStore()


# ---------------------------------------------------------------------------
# 设备管理
# ---------------------------------------------------------------------------

router_devices = APIRouter(prefix="/api/devices", tags=["devices"])


@router_devices.post("", response_model=DeviceOut)
def create_device(data: DeviceCreate):
    return device_store.create(data)


@router_devices.get("", response_model=list[DeviceOut])
def list_devices():
    return device_store.list()


@router_devices.get("/{device_id}", response_model=DeviceOut)
def get_device(device_id: str):
    device = device_store.get(device_id)
    if not device:
        raise HTTPException(404, "设备不存在")
    return device


@router_devices.put("/{device_id}", response_model=DeviceOut)
def update_device(device_id: str, data: DeviceUpdate):
    device = device_store.update(device_id, data)
    if not device:
        raise HTTPException(404, "设备不存在")
    return device


@router_devices.delete("/{device_id}")
def delete_device(device_id: str):
    if not device_store.delete(device_id):
        raise HTTPException(404, "设备不存在")
    return {"status": "deleted"}


# ---------------------------------------------------------------------------
# 测量与采集
# ---------------------------------------------------------------------------

router_measurements = APIRouter(prefix="/api/measurements", tags=["measurements"])


@router_measurements.post("", response_model=MeasurementOut)
def add_measurement(data: Measurement):
    return measurement_store.add(data)


@router_measurements.get("", response_model=list[MeasurementOut])
def list_measurements(device_id: str | None = None):
    return measurement_store.list(device_id)


# ---------------------------------------------------------------------------
# 色卡识别与标定
# ---------------------------------------------------------------------------

router_color_checker = APIRouter(prefix="/api/color-checker", tags=["color-checker"])


@router_color_checker.post("/detect", response_model=ColorCheckerDetectResult)
def detect_checker(req: ColorCheckerDetectRequest):
    return run_color_checker_detection(req)


# ---------------------------------------------------------------------------
# 色彩转换
# ---------------------------------------------------------------------------

router_convert = APIRouter(prefix="/api/convert", tags=["convert"])


@router_convert.post("", response_model=ConvertResponse)
def convert(req: ConvertRequest):
    result = convert_color(req.rgb, req.from_space, req.to_space)
    return ConvertResponse(
        result=[round(v, 4) for v in result],
        from_space=req.from_space,
        to_space=req.to_space,
    )


# ---------------------------------------------------------------------------
# 智能校正
# ---------------------------------------------------------------------------

router_correction = APIRouter(prefix="/api/correction", tags=["correction"])


@router_correction.post("", response_model=CorrectionResponse)
def correction(req: CorrectionRequest):
    return run_correction(device_store, req)


# ---------------------------------------------------------------------------
# ΔE 检测
# ---------------------------------------------------------------------------

router_delta_e = APIRouter(prefix="/api/delta-e", tags=["delta-e"])


@router_delta_e.post("", response_model=DeltaEResponse)
def delta_e_check(req: DeltaERequest):
    return run_delta_e(req)


@router_delta_e.post("/lab", response_model=DeltaEResponse)
def delta_e_lab_check(req: LabDeltaERequest):
    """直接对两个 Lab 值做色差检测（印前实测比对场景）。"""
    de = delta_e(req.lab1, req.lab2, req.method.value)
    return DeltaEResponse(
        delta_e=round(de, 4),
        method=req.method.value,
        lab1=[round(v, 2) for v in req.lab1],
        lab2=[round(v, 2) for v in req.lab2],
        passed=de <= req.tolerance,
        tolerance=req.tolerance,
    )


# ---------------------------------------------------------------------------
# 闭环验证与迭代
# ---------------------------------------------------------------------------

router_closed_loop = APIRouter(prefix="/api/closed-loop", tags=["closed-loop"])


@router_closed_loop.post("", response_model=ClosedLoopResponse)
def closed_loop(req: ClosedLoopRequest):
    return run_closed_loop(req)


# ---------------------------------------------------------------------------
# 色域可视化
# ---------------------------------------------------------------------------

router_visualization = APIRouter(prefix="/api/visualization", tags=["visualization"])


@router_visualization.get("/gamut", response_model=GamutVisualizationResponse)
def gamut_info(spaces: str = "srgb,dci-p3,adobe-rgb,rec2020"):
    space_list = [s.strip() for s in spaces.split(",") if s.strip()]
    data = gamut_visualization_data(space_list)
    return GamutVisualizationResponse(**data)


@router_visualization.get("/spectrum", response_model=SpectrumLocusResponse)
def spectrum_locus_data():
    """CIE 1931 光谱轨迹（含光谱显示色与紫线闭合），供前端绘制马蹄图。"""
    from ..services.visualization import spectrum_locus

    return SpectrumLocusResponse(**spectrum_locus())


@router_visualization.get("/delta-e-distribution")
def delta_e_dist(
    target_rgb: str = "200,150,100",
    space: str = "srgb",
    method: str = "cie2000",
):
    target = [float(v) for v in target_rgb.split(",")]
    # 生成一组模拟测量点（围绕目标加随机偏移）
    import numpy as np
    np.random.seed(42)
    measured = []
    for _ in range(20):
        noise = np.random.randn(3) * 20
        measured.append([float(v) for v in np.clip(target + noise, 0, 255)])
    return delta_e_distribution(target, measured, space, method)


# ---------------------------------------------------------------------------
# 工作流
# ---------------------------------------------------------------------------

router_workflow = APIRouter(prefix="/api/workflow", tags=["workflow"])


@router_workflow.get("")
def list_workflows():
    return workflow_store.list()


@router_workflow.post("/run")
def run_workflow(device_id: str, type: str = "calibration"):
    """运行工作流（MVP：调用闭环校正）。"""
    target = [200.0, 150.0, 100.0]
    measured = [185.0, 140.0, 90.0]
    result = run_closed_loop(ClosedLoopRequest(
        device_id=device_id,
        target_rgb=target,
        initial_measured_rgb=measured,
    ))
    run = workflow_store.create(device_id, type, {
        "converged": result.converged,
        "final_delta_e": result.final_delta_e,
        "iterations": result.iterations_used,
    })
    return run


# ---------------------------------------------------------------------------
# 系统
# ---------------------------------------------------------------------------

router_system = APIRouter(prefix="/api", tags=["system"])


@router_system.get("/health")
def health():
    return {"status": "ok", "product": "色彩管理闭环AI平台", "version": "0.5.0"}


@router_system.get("/spaces")
def spaces():
    from ..algorithms import SUPPORTED_SPACES
    return {"supported_spaces": list(SUPPORTED_SPACES)}
