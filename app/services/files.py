"""印前文件处理：预检（Preflight）+ 经工艺 ICC 的色彩转换（含软打样预览）。

预检在客户文件接入时自动运行，给出可执行的检查结论；
转换走标准 ICC CMM（ImageCms），输出 CMYK 成品文件与 RGB 软打样预览。
"""

from __future__ import annotations

import base64
import io

import numpy as np
from PIL import Image, ImageCms

from .processes import ProcessStore

_INTENT_MAP = {
    "perceptual": ImageCms.Intent.PERCEPTUAL,
    "saturation": ImageCms.Intent.SATURATION,
    "relative_colorimetric": ImageCms.Intent.RELATIVE_COLORIMETRIC,
    "absolute_colorimetric": ImageCms.Intent.ABSOLUTE_COLORIMETRIC,
}


def preflight(image_bytes: bytes, filename: str = "") -> dict:
    """印前预检：格式/模式/尺寸/DPI/嵌入 ICC/主导色，给出行动建议。"""
    img = Image.open(io.BytesIO(image_bytes))
    icc_profile_bytes = img.info.get("icc_profile")

    issues: list[dict] = []

    if icc_profile_bytes:
        try:
            embedded = ProcessStore.parse_meta(icc_profile_bytes)
        except Exception:  # noqa: BLE001 —— 损坏的嵌入 profile 不阻断预检
            embedded = {}
            issues.append({
                "level": "warn",
                "message": "文件嵌入了 ICC profile 但无法解析，建议重新导出文件",
            })
        if embedded:
            issues.append({
                "level": "info",
                "message": f"已嵌入 ICC：{embedded.get('description', '未知')}",
            })
    else:
        embedded = {}
        issues.append({
            "level": "warn",
            "message": "文件未嵌入 ICC profile，转换时将按 sRGB 假设源色彩空间",
        })

    dpi = img.info.get("dpi")
    dpi_val = round(float(dpi[0]), 1) if dpi else None
    if dpi_val and dpi_val < 300:
        issues.append({
            "level": "warn",
            "message": f"图像分辨率 {dpi_val} DPI 低于印刷建议值 300 DPI",
        })

    if img.mode not in ("RGB", "RGBA"):
        issues.append({
            "level": "info",
            "message": f"图像模式为 {img.mode}，转换前将统一为 RGB",
        })

    # 主导色（quantize 调色板）
    small = img.convert("RGB")
    small.thumbnail((96, 96))
    try:
        quant = small.quantize(colors=5)
        palette = quant.getpalette() or []
        dominant = []
        counts = sorted(quant.colors.items(), key=lambda kv: -kv[1]) if hasattr(quant, "colors") else []
        for idx, _cnt in counts[:5]:
            r, g, b = palette[idx * 3 : idx * 3 + 3]
            dominant.append(f"#{r:02x}{g:02x}{b:02x}")
    except Exception:  # noqa: BLE001 —— 主导色提取失败不阻断
        dominant = []

    status = "warn" if any(i["level"] == "warn" for i in issues) else "pass"
    return {
        "filename": filename,
        "format": img.format,
        "mode": img.mode,
        "size": list(img.size),
        "dpi": dpi_val,
        "has_embedded_icc": bool(icc_profile_bytes),
        "embedded_icc": embedded,
        "dominant_colors": dominant,
        "issues": issues,
        "status": status,
    }


def convert_image(
    image_bytes: bytes,
    process_icc_bytes: bytes,
    device_space: str,
    intent: str = "relative_colorimetric",
) -> dict:
    """经工艺 ICC 转换图像（源 sRGB 或嵌入 ICC → 工艺设备空间）。

    返回:
        preview_b64: 软打样预览（转换结果经逆向变换回 RGB 显示）
        file_b64: 成品文件（CMYK 工艺输出 TIFF，RGB 工艺输出 PNG）
        file_format / output_mode
    """
    img = Image.open(io.BytesIO(image_bytes))
    embedded = img.info.get("icc_profile")

    if embedded:
        src_prof = ImageCms.getOpenProfile(io.BytesIO(embedded))
        src_name = "嵌入 ICC"
    else:
        src_prof = ImageCms.createProfile("sRGB")
        src_name = "sRGB（假设）"

    dst_prof = ImageCms.getOpenProfile(io.BytesIO(process_icc_bytes))
    intent_flag = _INTENT_MAP.get(intent, ImageCms.Intent.RELATIVE_COLORIMETRIC)

    out_mode = "CMYK" if device_space == "CMYK" else "RGB"
    xform = ImageCms.buildTransform(
        src_prof, dst_prof, "RGB", out_mode,
        renderingIntent=intent_flag,
    )
    rgb_img = img.convert("RGB")
    converted = ImageCms.applyTransform(rgb_img, xform)

    # 成品文件
    out_format = "TIFF" if out_mode == "CMYK" else "PNG"
    buf = io.BytesIO()
    converted.save(buf, format=out_format)
    file_b64 = base64.b64encode(buf.getvalue()).decode("ascii")

    # 软打样预览：转换结果经逆向变换回 sRGB 展示
    rev = ImageCms.buildTransform(
        dst_prof, ImageCms.createProfile("sRGB"), out_mode, "RGB",
        renderingIntent=ImageCms.Intent.RELATIVE_COLORIMETRIC,
    )
    proof = ImageCms.applyTransform(converted, rev)
    pbuf = io.BytesIO()
    proof.save(pbuf, format="PNG")
    preview_b64 = base64.b64encode(pbuf.getvalue()).decode("ascii")

    return {
        "preview_b64": f"data:image/png;base64,{preview_b64}",
        "file_b64": f"data:image/tiff;base64,{file_b64}" if out_mode == "CMYK" else f"data:image/png;base64,{file_b64}",
        "file_format": out_format,
        "output_mode": out_mode,
        "source_profile": src_name,
    }
