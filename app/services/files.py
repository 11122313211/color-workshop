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


def preflight(data: bytes, filename: str = "") -> dict:
    """印前预检入口：PDF 走结构化检查（pypdf），图像走原有检查。"""
    if data[:5] == b"%PDF-":
        return _pdf_preflight(data, filename)
    return _image_preflight(data, filename)


def _image_preflight(image_bytes: bytes, filename: str = "") -> dict:
    """图像预检：格式/模式/尺寸/DPI/嵌入 ICC/主导色，给出行动建议。"""
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


def _pdf_preflight(data: bytes, filename: str = "") -> dict:
    """PDF 印前预检（pypdf）：页面/色彩空间/专色/输出意图/字体/透明度。

    图像有效 DPI 需要解析内容流 CTM 才能准确计算，MVP 阶段不给出 DPI 结论。
    PDF 仅预检，不做 ICC 转换（栅格化转换记入 V0.5）。
    """
    issues: list[dict] = []

    # 首页缩略图（pypdfium2 可选依赖，缺失时静默降级）
    thumbnail_b64 = None
    try:
        import pypdfium2 as pdfium

        doc = pdfium.PdfDocument(data)
        pil = doc[0].render(scale=1.0).to_pil().convert("RGB")
        pil.thumbnail((460, 460))
        tbuf = io.BytesIO()
        pil.save(tbuf, format="PNG")
        thumbnail_b64 = "data:image/png;base64," + base64.b64encode(tbuf.getvalue()).decode("ascii")
        doc.close()
    except Exception:  # noqa: BLE001 —— 缩略图失败不影响预检
        thumbnail_b64 = None

    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        issues.append({"level": "error", "message": "PDF 已加密，无法解析内部对象，请提供未加密文件"})
        return {
            "filename": filename,
            "format": "PDF",
            "mode": "PDF（加密）",
            "size": [0, 0],
            "pages": 0,
            "thumbnail_b64": thumbnail_b64,
            "issues": issues,
            "status": "error",
        }

    pages = len(reader.pages)
    pdf: dict = {
        "page_sizes_mm": [],
        "images": [],
        "spot_colors": [],
        "fonts": 0,
        "transparency": False,
        "has_output_intent": False,
    }

    def _colorspace_name(cs, page_res=None) -> str:
        cs_obj = cs.get_object() if hasattr(cs, "get_object") else cs
        if isinstance(cs_obj, list) and cs_obj:
            head = str(cs_obj[0])
            if head == "/ICCBased":
                try:
                    n = cs_obj[1].get_object().get("/N")
                    return f"ICCBased(N={n})"
                except Exception:  # noqa: BLE001
                    return "ICCBased"
            if head == "/Separation" and len(cs_obj) > 1:
                return f"Separation:{cs_obj[1]}"
            return head
        name = str(cs_obj)
        # 命名色彩空间 → 页面资源里查实际定义
        if page_res and name in page_res.get("/ColorSpace", {}):
            return _colorspace_name(page_res["/ColorSpace"][name], page_res)
        return name.lstrip("/")

    for pi, page in enumerate(reader.pages):
        mb = page.mediabox
        pdf["page_sizes_mm"].append(
            [round(float(mb.width) * 25.4 / 72, 1), round(float(mb.height) * 25.4 / 72, 1)]
        )
        res = page.get("/Resources")
        res = res.get_object() if res is not None else {}
        fonts = res.get("/Font") or {}
        pdf["fonts"] += len(fonts)
        # 专色：页面 /ColorSpace 资源里的 /Separation 定义
        for _k, v in (res.get("/ColorSpace") or {}).items():
            arr = v.get_object()
            if isinstance(arr, list) and arr and str(arr[0]) == "/Separation" and len(arr) > 1:
                spot = str(arr[1]).lstrip("/")
                if spot not in pdf["spot_colors"]:
                    pdf["spot_colors"].append(spot)
        # 图像 XObject
        for _name, ref in (res.get("/XObject") or {}).items():
            xo = ref.get_object()
            if xo.get("/Subtype") != "/Image":
                continue
            cs_name = _colorspace_name(xo.get("/ColorSpace"), res)
            if "/SMask" in xo or "/Mask" in xo:
                pdf["transparency"] = True
            pdf["images"].append(
                {
                    "page": pi + 1,
                    "px": [int(xo.get("/Width", 0)), int(xo.get("/Height", 0))],
                    "colorspace": cs_name,
                }
            )

    # 输出意图（PDF/X 常见）
    try:
        root = reader.trailer["/Root"]
        oi = root.get("/OutputIntents")
        pdf["has_output_intent"] = bool(oi and len(oi.get_object()))
        if pdf["has_output_intent"]:
            issues.append({
                "level": "info",
                "message": "已嵌入输出意图（Output Intent），色彩将以印刷条件为基准",
            })
    except Exception:  # noqa: BLE001
        pdf["has_output_intent"] = False
    if not pdf["has_output_intent"]:
        issues.append({
            "level": "warn",
            "message": "未嵌入输出意图（Output Intent），建议以目标工艺 ICC 导出 PDF/X 后再印",
        })

    rgb_like = [im for im in pdf["images"] if im["colorspace"].startswith(("DeviceRGB", "ICCBased(N=3"))]
    if rgb_like:
        issues.append({
            "level": "warn",
            "message": f"{len(rgb_like)} 个图像为 RGB 色彩空间，印刷建议转 CMYK（可用「印前转换」处理图像后重新组版）",
        })
    if pdf["spot_colors"]:
        issues.append({
            "level": "info",
            "message": "含专色：" + "、".join(pdf["spot_colors"][:5]) + ("等" if len(pdf["spot_colors"]) > 5 else ""),
        })
    if pdf["transparency"]:
        issues.append({
            "level": "info",
            "message": "含透明对象（SMask），RIP 输出时会自动拼合透明度",
        })
    sizes = pdf["page_sizes_mm"]
    if sizes and len({tuple(s) for s in sizes}) > 1:
        issues.append({
            "level": "warn",
            "message": f"各页面尺寸不一致（{sizes[0]} / {sizes[-1]} mm 等），请确认拼版方式",
        })

    status = "error" if any(i["level"] == "error" for i in issues) else (
        "warn" if any(i["level"] == "warn" for i in issues) else "pass"
    )
    first = sizes[0] if sizes else [0, 0]
    return {
        "filename": filename,
        "format": "PDF",
        "mode": "PDF",
        "size": [int(first[0]), int(first[1])],
        "pages": pages,
        "pdf": pdf,
        "thumbnail_b64": thumbnail_b64,
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
    if image_bytes[:5] == b"%PDF-":
        raise ValueError("PDF 暂不支持 ICC 转换（仅预检），请导出页面图像后再试")
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
