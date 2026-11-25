"""印前文件路由：预检、PDF/X-4 结构封装与经工艺 ICC 的色彩转换。"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Query, Request

from ..models.schemas import (
    FileConvertRequest,
    FileConvertResponse,
    FilePdfxResponse,
    PreflightReport,
    PreflightRequest,
)
from ..services.files import convert_image, preflight
from ..services.lifecycle import archived_detail, is_archived
from ..services.pdfx import MAX_PDF_BYTES, PdfxError, package_pdfx
from ..services.processes import ProcessStore
from ..services.render_check import (
    DEFAULT_BLANK_PAGE_THRESHOLD,
    DEFAULT_INK_DRIFT_THRESHOLD,
    DEFAULT_RENDER_DPI,
)
from ..services.displays import DisplayStore
from ._http import read_body_limited, read_icc_or_409

router = APIRouter(prefix="/api/files", tags=["files"])

_processes = ProcessStore()
_displays = DisplayStore()


def _ensure_active(proc: dict) -> None:
    if is_archived(proc):
        raise HTTPException(409, detail=archived_detail())


def _decode_b64(data_b64: str) -> bytes:
    import base64
    # 兼容 data URI 前缀（无逗号时按非法 data URI 拒绝，避免 IndexError 500）
    if data_b64.startswith("data:"):
        if "," not in data_b64:
            raise HTTPException(400, "无效的 data URI 编码")
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
        raise HTTPException(400, f"无法解析文件（图像或 PDF 均失败）: {e}") from e
    return PreflightReport(**report)


@router.post("/convert", response_model=FileConvertResponse)
def run_convert(req: FileConvertRequest):
    proc = _processes.get(req.process_id)
    if not proc:
        raise HTTPException(404, "工艺不存在，请先在工艺库中创建")
    _ensure_active(proc)
    image_bytes = _decode_b64(req.image_b64)
    icc_bytes = read_icc_or_409(_processes, req.process_id)
    try:
        result = convert_image(
            image_bytes,
            icc_bytes,
            proc["icc_meta"]["device_space"],
            req.intent,
        )
        # V0.9 P0-4：软打样门禁——显示器未校准/过期时，预览仅参考
        result["softproof"] = _displays.softproof_status(req.display_id)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"转换失败: {e}") from e
    return FileConvertResponse(**result)


@router.post("/pdfx", response_model=FilePdfxResponse)
async def make_pdfx(
    request: Request,
    process_id: str = Query(..., description="输出意图绑定的工艺 id"),
    filename: str = Query("", description="源文件名（用于输出命名与 XMP 标题）"),
    strict: bool = Query(True, description="严格模式：字体未嵌入/RGB 图像直接拒绝"),
    allow_rgb: bool = Query(False, description="显式允许 RGB 图像（非严格时可用）"),
    blank_threshold: float = Query(
        DEFAULT_BLANK_PAGE_THRESHOLD,
        ge=0.0,
        le=1.0,
        description="空白页判定非白像素占比阈值（默认 0.001 即 0.1%）",
    ),
    drift_threshold: float = Query(
        DEFAULT_INK_DRIFT_THRESHOLD,
        ge=0.0,
        le=1.0,
        description="封装前后墨量相对漂移阈值（默认 0.05 即 5%）",
    ),
    render_dpi: float = Query(
        DEFAULT_RENDER_DPI,
        ge=36.0,
        le=150.0,
        description="渲染验证分辨率 DPI（默认 72）",
    ),
):
    """把 PDF 封装为 PDF/X-4 结构文件（OutputIntent + XMP + 盒子）并执行渲染验证。

    请求体为 PDF 原始字节（流式限流 + 线程池封装避免阻塞事件循环）。
    """
    proc = _processes.get(process_id)
    if not proc:
        raise HTTPException(404, "工艺不存在，请先在工艺库中创建")
    _ensure_active(proc)
    data = await read_body_limited(
        request,
        MAX_PDF_BYTES,
        too_large_detail=f"PDF 过大（>{MAX_PDF_BYTES // 1024 // 1024}MB），请拆分后再封装",
    )
    if not data:
        raise HTTPException(400, "缺少 PDF 文件内容（raw bytes body）")
    # 异步读取请求体完成后再次校验工艺仍存在且未归档
    proc = _processes.get(process_id)
    if not proc:
        raise HTTPException(404, "工艺不存在，请先在工艺库中创建")
    _ensure_active(proc)
    icc_bytes = read_icc_or_409(_processes, process_id)
    try:
        result = await asyncio.to_thread(
            package_pdfx,
            data,
            icc_bytes,
            proc.get("icc_meta") or {},
            process_name=proc.get("name", ""),
            filename=filename,
            strict=strict,
            allow_rgb=allow_rgb,
            blank_threshold=blank_threshold,
            drift_threshold=drift_threshold,
            render_dpi=render_dpi,
        )
    except PdfxError as e:
        raise HTTPException(422, str(e)) from e
    return FilePdfxResponse(**result)
