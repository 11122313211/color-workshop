"""印前文件路由：预检与经工艺 ICC 的色彩转换。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..models.schemas import (
    FileConvertRequest,
    FileConvertResponse,
    PreflightReport,
    PreflightRequest,
)
from ..services.files import convert_image, preflight
from ..services.processes import ProcessStore

router = APIRouter(prefix="/api/files", tags=["files"])

_processes = ProcessStore()


def _decode_b64(data_b64: str) -> bytes:
    import base64
    # 兼容 data URI 前缀
    if data_b64.startswith("data:"):
        data_b64 = data_b64.split(",", 1)[1]
    try:
        return base64.b64decode(data_b64)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, "无效的 base64 图像数据") from e


@router.post("/preflight", response_model=PreflightReport)
def run_preflight(req: PreflightRequest):
    image_bytes = _decode_b64(req.image_b64)
    try:
        report = preflight(image_bytes, req.filename)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"无法解析图像文件: {e}") from e
    return PreflightReport(**report)


@router.post("/convert", response_model=FileConvertResponse)
def run_convert(req: FileConvertRequest):
    proc = _processes.get(req.process_id)
    if not proc:
        raise HTTPException(404, "工艺不存在，请先在工艺库中创建")
    image_bytes = _decode_b64(req.image_b64)
    try:
        icc_bytes = _processes.icc_bytes(req.process_id)
        result = convert_image(
            image_bytes,
            icc_bytes,
            proc["icc_meta"]["device_space"],
            req.intent,
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"转换失败: {e}") from e
    return FileConvertResponse(**result)
