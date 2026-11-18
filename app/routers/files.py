"""印前文件路由：预检、PDF/X-4 结构封装与经工艺 ICC 的色彩转换。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from ..models.schemas import (
    FileConvertRequest,
    FileConvertResponse,
    FilePdfxResponse,
    PreflightReport,
    PreflightRequest,
)
from ..services.files import convert_image, preflight
from ..services.pdfx import MAX_PDF_BYTES, PdfxError, package_pdfx
from ..services.processes import ProcessStore
from ..services.displays import DisplayStore

router = APIRouter(prefix="/api/files", tags=["files"])

_processes = ProcessStore()
_displays = DisplayStore()


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
        raise HTTPException(400, f"无法解析文件（图像或 PDF 均失败）: {e}") from e
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
):
    """把 PDF 封装为 PDF/X-4 结构文件（OutputIntent + XMP + 盒子）。

    请求体为 PDF 原始字节；返回 base64 成品与逐项检查结论。
    结构级合规，不替代 RIP 端全量 PDF/X 校验。
    """
    proc = _processes.get(process_id)
    if not proc:
        raise HTTPException(404, "工艺不存在，请先在工艺库中创建")
    data = await request.body()
    if not data:
        raise HTTPException(400, "缺少 PDF 文件内容（raw bytes body）")
    if len(data) > MAX_PDF_BYTES:
        raise HTTPException(413, f"PDF 过大（>{MAX_PDF_BYTES // 1024 // 1024}MB），请拆分后再封装")
    try:
        result = package_pdfx(
            data,
            _processes.icc_bytes(process_id),
            proc.get("icc_meta") or {},
            process_name=proc.get("name", ""),
            filename=filename,
            strict=strict,
            allow_rgb=allow_rgb,
        )
    except PdfxError as e:
        raise HTTPException(422, str(e)) from e
    return FilePdfxResponse(**result)
