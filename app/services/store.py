"""服务层：设备管理、测量采集、工作流等业务逻辑。

MVP 阶段使用内存存储，后续可替换为 PostgreSQL。
"""

from __future__ import annotations

import uuid
from datetime import datetime

import numpy as np

from ..algorithms import convert_color, create_srgb_profile, load_profile_from_bytes
from ..ai import closed_loop_correct, detect_color_checker, generate_calibration_profile
from ..models.schemas import (
    DeviceCreate,
    DeviceOut,
    DeviceUpdate,
    Measurement,
    MeasurementOut,
    CorrectionResponse,
    CorrectionRequest,
    ClosedLoopRequest,
    ClosedLoopResponse,
    LoopIteration,
    DeltaERequest,
    DeltaEResponse,
    ColorCheckerDetectRequest,
    ColorCheckerDetectResult,
    ColorCheckerPatch,
)


class DeviceStore:
    """内存设备存储。"""

    def __init__(self) -> None:
        self._devices: dict[str, DeviceOut] = {}

    def create(self, data: DeviceCreate) -> DeviceOut:
        device = DeviceOut(
            id=uuid.uuid4().hex[:12],
            name=data.name,
            model=data.model,
            space=data.space,
            description=data.description,
        )
        self._devices[device.id] = device
        return device

    def get(self, device_id: str) -> DeviceOut | None:
        return self._devices.get(device_id)

    def list(self) -> list[DeviceOut]:
        return list(self._devices.values())

    def update(self, device_id: str, data: DeviceUpdate) -> DeviceOut | None:
        device = self._devices.get(device_id)
        if not device:
            return None
        updates = data.model_dump(exclude_none=True)
        for k, v in updates.items():
            setattr(device, k, v)
        return device

    def delete(self, device_id: str) -> bool:
        return self._devices.pop(device_id, None) is not None

    def set_calibrated(self, device_id: str, calibrated: bool, profile_ready: bool = True) -> DeviceOut | None:
        device = self._devices.get(device_id)
        if device:
            device.calibrated = calibrated
            device.profile_created = profile_ready
        return device


class MeasurementStore:
    """内存测量记录存储。"""

    def __init__(self) -> None:
        self._records: dict[str, MeasurementOut] = {}

    def add(self, data: Measurement) -> MeasurementOut:
        lab = convert_color(data.rgb, data.space, "lab")
        rec = MeasurementOut(
            id=uuid.uuid4().hex[:12],
            device_id=data.device_id,
            rgb=[round(v, 4) for v in data.rgb],
            space=data.space,
            lab=[round(v, 2) for v in lab],
            conditions_ok=data.conditions_ok,
            note=data.note,
        )
        self._records[rec.id] = rec
        return rec

    def list(self, device_id: str | None = None) -> list[MeasurementOut]:
        if device_id:
            return [r for r in self._records.values() if r.device_id == device_id]
        return list(self._records.values())


class WorkflowStore:
    """内存工作流存储。"""

    def __init__(self) -> None:
        self._runs: dict[str, dict] = {}

    def create(self, device_id: str, type: str, result: dict) -> dict:
        run = {
            "id": uuid.uuid4().hex[:12],
            "device_id": device_id,
            "type": type,
            "status": "completed",
            "result": result,
            "created_at": datetime.now().isoformat(),
        }
        self._runs[run["id"]] = run
        return run

    def list(self) -> list[dict]:
        return list(self._runs.values())


# ---------------------------------------------------------------------------
# 业务服务
# ---------------------------------------------------------------------------

def run_correction(store: DeviceStore, req: CorrectionRequest) -> CorrectionResponse:
    """智能校正：生成校正矩阵并计算校正前后 ΔE。"""
    from ..algorithms import delta_e_from_rgb
    de_before = delta_e_from_rgb(req.measured_rgb, req.target_rgb, req.space, req.method.value)

    # 简单线性校正：测量值 -> 目标值的最小二乘矩阵
    measured = np.array([req.measured_rgb])
    target = np.array([req.target_rgb])
    M = np.linalg.lstsq(measured, target, rcond=None)[0].T
    corrected = (measured @ M)[0]
    de_after = delta_e_from_rgb(corrected, req.target_rgb, req.space, req.method.value)

    converged = de_after <= req.tolerance
    return CorrectionResponse(
        device_id=req.device_id,
        correction_matrix=[[round(v, 4) for v in row] for row in M],
        delta_e_before=round(de_before, 4),
        delta_e_after=round(de_after, 4),
        converged=converged,
        iterations=1,
        status="converged" if converged else "needs_iteration",
    )


def run_closed_loop(req: ClosedLoopRequest) -> ClosedLoopResponse:
    """闭环验证迭代：自动迭代直至 ΔE 达标。"""
    result = closed_loop_correct(
        req.initial_measured_rgb,
        req.target_rgb,
        req.space,
        req.method.value,
        req.tolerance,
        req.max_iterations,
    )
    iterations = [
        LoopIteration(**it) for it in result["iterations"]
    ]
    return ClosedLoopResponse(
        device_id=req.device_id,
        target_rgb=req.target_rgb,
        converged=result["converged"],
        iterations=iterations,
        final_delta_e=result["final_delta_e"],
        iterations_used=result["iterations_used"],
    )


def run_color_checker_detection(req: ColorCheckerDetectRequest) -> ColorCheckerDetectResult:
    """色卡检测与标定。"""
    from ..ai import decode_base64_image
    image_bytes = decode_base64_image(req.image_bytes)
    patches, detected, quality = detect_color_checker(
        image_bytes, req.expected_patches, req.space
    )
    patch_out = [
        ColorCheckerPatch(
            index=p.index,
            x=round(p.x, 4),
            y=round(p.y, 4),
            width=round(p.width, 4),
            height=round(p.height, 4),
            rgb=[round(v, 4) for v in p.rgb],
            lab=[round(v, 2) for v in p.lab],
        )
        for p in patches
    ]
    return ColorCheckerDetectResult(
        detected=detected,
        patches_found=len(patches),
        patches=patch_out,
        profile_ready=detected,
        quality=quality,
    )


def run_delta_e(req: DeltaERequest) -> DeltaEResponse:
    """ΔE 检测。"""
    from ..algorithms import convert_color, delta_e
    lab1 = convert_color(req.rgb1, req.space, "lab")
    lab2 = convert_color(req.rgb2, req.space, "lab")
    de = delta_e(lab1, lab2, req.method.value)
    return DeltaEResponse(
        delta_e=round(de, 4),
        method=req.method.value,
        lab1=[round(v, 2) for v in lab1],
        lab2=[round(v, 2) for v in lab2],
        passed=de <= 2.0,
    )
