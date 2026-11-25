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
    SpotCheckRequest,
    SpotCheckResponse,
)
from ..services.bridge import (
    check_spot_colors,
    consume_printguard_report,
    evaluate_passport_gate,
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


def _resolve_process_and_icc(process_id: str) -> tuple[dict | None, bytes | None]:
    pid = (process_id or "").strip()
    if not pid:
        return None, None
    proc = _processes.get(pid)
    if not proc:
        return None, None
    try:
        icc_bytes = _processes.icc_bytes(pid)
    except Exception:  # noqa: BLE001
        icc_bytes = None
    return proc, icc_bytes


@router.post("/preflight", response_model=PreflightReport)
def run_preflight(req: PreflightRequest):
    # 通道 1：直接消费 PrintGuard Pro 导出的机读 JSON 预检报告
    if req.printguard_report is not None:
        embedded_pp = (
            req.printguard_report.get("process_passport")
            if isinstance(req.printguard_report, dict) and isinstance(req.printguard_report.get("process_passport"), dict)
            else {}
        )
        eff_pid = (req.process_id or str(embedded_pp.get("process_id") or "")).strip()
        proc, icc_bytes = _resolve_process_and_icc(eff_pid)
        try:
            report = consume_printguard_report(
                req.printguard_report,
                filename=req.filename,
                process_doc=proc,
                icc_bytes=icc_bytes,
                passport_hash=req.passport_hash,
                expected_icc_sha256=req.expected_icc_sha256,
                spot_colors=req.spot_colors,
                tolerance=req.spot_tolerance,
            )
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        return PreflightReport(**report)

    # 通道 2：原始图像 / PDF 预检 + 可选工艺护照指纹门禁与专色色域核验
    if not req.image_b64:
        raise HTTPException(400, "缺少待预检文件（image_b64）或 PrintGuard 机读报告（printguard_report）")
    image_bytes = _decode_b64(req.image_b64)
    try:
        report = preflight(image_bytes, req.filename)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"无法解析文件（图像或 PDF 均失败）: {e}") from e

    eff_pid = (req.process_id or "").strip()
    if eff_pid or (req.passport_hash or "").strip():
        proc, icc_bytes = _resolve_process_and_icc(eff_pid)
        file_icc_sha = str(
            (report.get("embedded_icc") or {}).get("sha256")
            or (report.get("pdf") or {}).get("output_intent_icc_sha256")
            or ""
        )
        gate = evaluate_passport_gate(
            proc,
            file_icc_sha256=file_icc_sha,
            passport_hash=req.passport_hash,
            expected_icc_sha256=req.expected_icc_sha256,
            require_file_icc=True,
        )
        report["passport_gate"] = gate
        if not gate["allowed"]:
            report["issues"].append({
                "level": "error",
                "message": f"工艺护照门禁拦截（{gate['reason']}）：{gate['message']}",
            })
            report["status"] = "error"
        else:
            report["issues"].append({"level": "info", "message": gate["message"]})

        # 若同时提供了专色清单或 PDF 内检出专色，则自动做工艺色域核验
        spots = req.spot_colors if req.spot_colors is not None else (
            (report.get("pdf") or {}).get("spot_colors") or []
        )
        if spots and proc is not None and icc_bytes:
            sc = check_spot_colors(proc, icc_bytes, spots, tolerance=req.spot_tolerance)
            report["spot_check"] = sc
            if sc["summary"]["out_of_gamut"] > 0:
                report["issues"].append({
                    "level": "warn",
                    "message": (
                        f"专色色域核验：{sc['summary']['out_of_gamut']} 个专色超出当前工艺色域"
                        f"（最大 ΔE₀₀ = {sc['summary']['max_delta_e']}），建议保留独立专色墨路"
                    ),
                })
                if report["status"] == "pass":
                    report["status"] = "warn"

    return PreflightReport(**report)


@router.post("/preflight/printguard", response_model=PreflightReport)
def run_preflight_printguard(
    payload: dict,
    process_id: str = Query("", description="可选目标工艺 id（覆盖报告内 process_passport.process_id）"),
    passport_hash: str = Query("", description="可选预期工艺护照指纹"),
    expected_icc_sha256: str = Query("", description="可选外部声明 ICC 指纹"),
    spot_tolerance: float = Query(2.0, gt=0.0, le=20.0, description="专色色域 ΔE₀₀ 容差"),
):
    """便捷消费 PrintGuard Pro 导出的原始 JSON 文件（可直接把 PrintGuard_Preflight_*.json 作为请求体 POST）。"""
    if isinstance(payload.get("printguard_report"), dict):
        pg_report = payload["printguard_report"]
        eff_pid = (process_id or str(payload.get("process_id") or "")).strip()
        eff_hash = (passport_hash or str(payload.get("passport_hash") or "")).strip()
        eff_icc = (expected_icc_sha256 or str(payload.get("expected_icc_sha256") or "")).strip()
        spots = payload.get("spot_colors")
        fname = str(payload.get("filename") or "")
    else:
        pg_report = payload
        embedded_pp = payload.get("process_passport") if isinstance(payload.get("process_passport"), dict) else {}
        eff_pid = (process_id or str(embedded_pp.get("process_id") or "")).strip()
        eff_hash = (passport_hash or str(embedded_pp.get("passport_hash") or "")).strip()
        eff_icc = (expected_icc_sha256 or str(embedded_pp.get("icc_sha256") or "")).strip()
        spots = payload.get("spot_colors")
        fname = ""

    proc, icc_bytes = _resolve_process_and_icc(eff_pid)
    try:
        report = consume_printguard_report(
            pg_report,
            filename=fname,
            process_doc=proc,
            icc_bytes=icc_bytes,
            passport_hash=eff_hash,
            expected_icc_sha256=eff_icc,
            spot_colors=spots,
            tolerance=spot_tolerance,
        )
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    return PreflightReport(**report)


@router.post("/spot-check", response_model=SpotCheckResponse)
def run_spot_check(req: SpotCheckRequest):
    """查询专色清单（如 Illustrator 桥接提取的专色）是否在目标工艺色域内及最优四色模拟 ΔE₀₀。"""
    proc = _processes.get(req.process_id)
    if not proc:
        raise HTTPException(404, "工艺不存在，请先在工艺库中创建")
    _ensure_active(proc)
    icc_bytes = read_icc_or_409(_processes, req.process_id)
    result = check_spot_colors(proc, icc_bytes, req.spots, tolerance=req.tolerance)
    return SpotCheckResponse(**result)


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
