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


#: 出血量建议下限（mm）——印前通用 3mm
BLEED_MIN_MM = 3.0

#: 常见成品开本（mm，宽×高）；判定容差 ±2mm，横竖版均可
PAGE_FORMATS = {
    "A3": (297.0, 420.0),
    "A4": (210.0, 297.0),
    "A5": (148.0, 210.0),
    "A6": (105.0, 148.0),
    "B4": (250.0, 353.0),
    "B5": (176.0, 250.0),
    "16开": (185.0, 260.0),
    "大16开": (210.0, 285.0),
    "32开": (130.0, 185.0),
    "大32开": (145.0, 210.0),
}

_PT_TO_MM = 25.4 / 72.0


def _deref(obj):
    """解引用 PDF 间接对象；None 原样返回。"""
    if obj is None:
        return None
    try:
        return obj.get_object()
    except AttributeError:
        return obj


def _page_resources(page) -> dict:
    """页面资源（沿 /Parent 继承链查找，部分生成器把 Resources 放在 Pages 节点）。"""
    node = page
    for _ in range(16):
        res = _deref(node.get("/Resources"))
        if res is not None:
            return res
        node = _deref(node.get("/Parent"))
        if node is None:
            break
    return {}


def _font_is_embedded(font: dict) -> tuple[bool, str]:
    """字体是否真正内嵌：Type0 查 DescendantFonts→CIDFont→FontDescriptor→FontFile*。"""
    subtype = str(font.get("/Subtype", "")).lstrip("/")
    if subtype == "Type3":
        procs = _deref(font.get("/CharProcs"))
        return (bool(procs), "Type3 字形程序内嵌" if procs else "Type3 无字形程序")

    descriptors = []
    if subtype == "Type0":
        for cf_ref in (_deref(font.get("/DescendantFonts")) or []):
            cf = _deref(cf_ref)
            if cf is not None:
                descriptors.append(_deref(cf.get("/FontDescriptor")))
    else:
        descriptors.append(_deref(font.get("/FontDescriptor")))

    if not descriptors or all(d is None for d in descriptors):
        return False, "无 FontDescriptor（标准字体，未嵌入）"
    for desc in descriptors:
        if desc is None:
            continue
        for key in ("/FontFile", "/FontFile2", "/FontFile3"):
            if desc.get(key) is not None:
                return True, "字体文件内嵌"
    return False, "FontDescriptor 无 FontFile*（未嵌入）"


def _font_report(resources: dict) -> list[dict]:
    """页面字体清单与嵌入状态（同一页面内按资源名列出）。"""
    fonts = _deref(resources.get("/Font")) or {}
    out: list[dict] = []
    for res_name, ref in fonts.items():
        font = _deref(ref)
        if font is None:
            continue
        embedded, reason = _font_is_embedded(font)
        out.append({
            "resource": str(res_name).lstrip("/"),
            "name": str(font.get("/BaseFont", res_name)).lstrip("/"),
            "subtype": str(font.get("/Subtype", "")).lstrip("/"),
            "embedded": embedded,
            "reason": reason,
        })
    return out


def _box_rect(page, key: str):
    """页面盒（pt）：[x0, y0, x1, y1]；缺失或非法返回 None。"""
    box = _deref(page.get(key))
    if box is None:
        return None
    try:
        vals = [float(v) for v in box]
    except (TypeError, ValueError):
        return None
    if len(vals) != 4:
        return None
    x0, x1 = sorted((vals[0], vals[2]))
    y0, y1 = sorted((vals[1], vals[3]))
    return (x0, y0, x1, y1)


def _rect_size_mm(rect) -> list[float]:
    return [round(abs(rect[2] - rect[0]) * _PT_TO_MM, 1),
            round(abs(rect[3] - rect[1]) * _PT_TO_MM, 1)]


def _page_boxes(page) -> dict | None:
    """成品/出血/介质盒与四边出血量（mm）。TrimBox 缺失时按 CropBox 近似成品尺寸。"""
    media = _box_rect(page, "/MediaBox")
    if media is None:
        return None
    crop = _box_rect(page, "/CropBox") or media
    trim = _box_rect(page, "/TrimBox")
    bleed = _box_rect(page, "/BleedBox")
    trim_ref = trim or crop
    # 无 BleedBox 时用 MediaBox 作为可出血边界（介质即物理纸张）
    bleed_ref = bleed or media
    bleed_mm = [
        round(max(0.0, (trim_ref[0] - bleed_ref[0]) * _PT_TO_MM), 1),
        round(max(0.0, (trim_ref[1] - bleed_ref[1]) * _PT_TO_MM), 1),
        round(max(0.0, (bleed_ref[2] - trim_ref[2]) * _PT_TO_MM), 1),
        round(max(0.0, (bleed_ref[3] - trim_ref[3]) * _PT_TO_MM), 1),
    ]
    return {
        "media_mm": _rect_size_mm(media),
        "crop_mm": _rect_size_mm(crop),
        "trim_mm": _rect_size_mm(trim) if trim else None,
        "bleed_mm": _rect_size_mm(bleed) if bleed else None,
        "has_trim": trim is not None,
        "has_bleed": bleed is not None,
        "bleed_source": "BleedBox" if bleed else "MediaBox",
        "bleed_sides_mm": bleed_mm,
        "min_bleed_mm": min(bleed_mm),
    }


def _match_page_format(size_mm: list[float], tol: float = 2.0) -> dict:
    """按常见开本匹配成品尺寸（横竖版均可）。"""
    w, h = size_mm
    for name, (fw, fh) in PAGE_FORMATS.items():
        if (abs(w - fw) <= tol and abs(h - fh) <= tol) or (
                abs(w - fh) <= tol and abs(h - fw) <= tol):
            return {"name": name, "match": True, "orientation": "portrait" if h >= w else "landscape"}
    return {"name": None, "match": False, "orientation": "portrait" if h >= w else "landscape"}


def _matrix_mul(m1, m2):
    """PDF 变换矩阵乘法（行向量约定：结果 = m1 × m2）。"""
    a1, b1, c1, d1, e1, f1 = m1
    a2, b2, c2, d2, e2, f2 = m2
    return (
        a1 * a2 + b1 * c2,
        a1 * b2 + b1 * d2,
        c1 * a2 + d1 * c2,
        c1 * b2 + d1 * d2,
        e1 * a2 + f1 * c2 + e2,
        e1 * b2 + f1 * d2 + f2,
    )


def _image_draw_dpi(reader, page, max_depth: int = 4) -> list[dict]:
    """解析内容流 CTM，计算每个图像 XObject 的实际绘制分辨率。

    图像单位正方形经 CTM 映射后的边长为 pt；有效 DPI = px × 72 / pt。
    同一图像多次绘制取最低值（预检按最差情况判定）。Form XObject 递归解析。
    """
    results: list[dict] = []
    try:
        contents = page.get_contents()
        if contents is None:
            return results
        from pypdf.generic import ContentStream
    except Exception:  # noqa: BLE001 —— 解析能力缺失不阻断预检
        return results

    def walk(stream, resources, ctm, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            # page.get_contents() 已返回 ContentStream；Form XObject 需现场解析
            if isinstance(stream, ContentStream):
                operations = stream.operations
            else:
                operations = ContentStream(stream, reader).operations
        except Exception:  # noqa: BLE001
            return
        stack: list[tuple] = []
        for operands, op in operations:
            try:
                if op == b"q":
                    stack.append(ctm)
                elif op == b"Q":
                    if stack:
                        ctm = stack.pop()
                elif op == b"cm" and len(operands) == 6:
                    ctm = _matrix_mul(tuple(float(x) for x in operands), ctm)
                elif op == b"Do" and operands:
                    name = str(operands[0]).lstrip("/")
                    xobjects = _deref(resources.get("/XObject")) or {}
                    xo = _deref(xobjects.get("/" + name))
                    if xo is None:
                        continue
                    subtype = str(xo.get("/Subtype", "")).lstrip("/")
                    if subtype == "Image":
                        w = int(xo.get("/Width", 0) or 0)
                        h = int(xo.get("/Height", 0) or 0)
                        sx = (ctm[0] ** 2 + ctm[1] ** 2) ** 0.5
                        sy = (ctm[2] ** 2 + ctm[3] ** 2) ** 0.5
                        if w and h and sx > 1e-6 and sy > 1e-6:
                            dpi = min(w * 72.0 / sx, h * 72.0 / sy)
                            results.append({
                                "name": name,
                                "px": [w, h],
                                "dpi": round(dpi, 1),
                            })
                    elif subtype == "Form":
                        matrix = _deref(xo.get("/Matrix"))
                        sub_ctm = ctm
                        if matrix is not None and len(matrix) == 6:
                            sub_ctm = _matrix_mul(tuple(float(x) for x in matrix), ctm)
                        sub_res = _deref(xo.get("/Resources")) or resources
                        walk(xo, sub_res, sub_ctm, depth + 1)
            except Exception:  # noqa: BLE001 —— 单个操作符异常跳过
                continue

    walk(contents, _page_resources(page), (1.0, 0.0, 0.0, 1.0, 0.0, 0.0), 0)
    return results


def _pdf_preflight(data: bytes, filename: str = "") -> dict:
    """PDF 印前预检（pypdf）：页面/色彩空间/专色/输出意图/字体/透明度/出血/有效 DPI。

    字体嵌入按 FontDescriptor→FontFile* 判定；出血按 TrimBox/BleedBox 计算；
    图像有效 DPI 解析内容流 CTM（含 Form XObject 递归）。
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
        "font_embedding": {"total": 0, "embedded": 0, "not_embedded": [], "type3": [], "items": []},
        "page_boxes": [],
        "bleed": {"checked": True, "ok": True, "min_mm": None},
        "effective_dpi": {"checked": True, "min": None, "low_count": 0, "items": []},
        "page_format": None,
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
        res = _page_resources(page)
        font_items = _font_report(res)
        pdf["fonts"] += len(font_items)
        for item in font_items:
            pdf["font_embedding"]["items"].append({**item, "page": pi + 1})
        boxes = _page_boxes(page)
        if boxes is not None:
            boxes["page"] = pi + 1
            pdf["page_boxes"].append(boxes)
        for draw in _image_draw_dpi(reader, page):
            draw["page"] = pi + 1
            pdf["effective_dpi"]["items"].append(draw)
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
                    "resource": str(_name).lstrip("/"),
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

    # 字体真嵌入（V0.9）：未嵌入字体在 RIP 端会被替换，排版会跑版
    # 跨页按 (字体名, 子类型, 是否嵌入) 去重，pages 记录出现页
    font_items_all = pdf["font_embedding"].pop("items", [])
    uniq_fonts: dict[tuple, dict] = {}
    for item in font_items_all:
        key = (item["name"], item["subtype"], item["embedded"])
        if key in uniq_fonts:
            uniq_fonts[key]["pages"].append(item["page"])
        else:
            uniq_fonts[key] = {**item, "pages": [item["page"]]}
    fonts_unique = list(uniq_fonts.values())
    fe = pdf["font_embedding"]
    fe.update({
        "total": len(fonts_unique),
        "embedded": sum(1 for f in fonts_unique if f["embedded"]),
        "not_embedded": [f for f in fonts_unique if not f["embedded"]],
        "type3": [f for f in fonts_unique if f["subtype"] == "Type3"],
    })
    if fe["not_embedded"]:
        names = sorted({f["name"] for f in fe["not_embedded"]})
        shown = "、".join(names[:3]) + ("等" if len(names) > 3 else "")
        issues.append({
            "level": "warn",
            "message": f"{len(fe['not_embedded'])} 个字体未嵌入（{shown}），RIP 可能替换字体导致排版与标点变化",
        })
    if fe["type3"]:
        issues.append({
            "level": "info",
            "message": f"{len(fe['type3'])} 个 Type3 字体（字形为绘图指令），高精度输出前建议转曲或替换",
        })

    # 出血（V0.9）：TrimBox 为成品线，BleedBox（缺省 MediaBox）为可出血边界
    boxes = pdf["page_boxes"]
    if not boxes:
        pdf["bleed"]["checked"] = False
    else:
        if not all(b["has_trim"] for b in boxes):
            issues.append({
                "level": "warn",
                "message": "未设置 TrimBox，无法判定成品尺寸与出血；建议在排版软件中设定成品线与 3mm 出血",
            })
        trimmed = [b for b in boxes if b["has_trim"]]
        if trimmed:
            worst = min(trimmed, key=lambda b: b["min_bleed_mm"])
            pdf["bleed"].update({
                "ok": worst["min_bleed_mm"] >= BLEED_MIN_MM,
                "min_mm": worst["min_bleed_mm"],
                "page": worst["page"],
                "source": worst["bleed_source"],
            })
            if worst["min_bleed_mm"] < BLEED_MIN_MM:
                sides = "/".join(f"{v:.1f}" for v in worst["bleed_sides_mm"])
                issues.append({
                    "level": "warn",
                    "message": (f"出血不足：第 {worst['page']} 页最小 {worst['min_bleed_mm']:.1f} mm"
                                f"（左/下/右/上 = {sides} mm，建议 ≥{BLEED_MIN_MM:.0f} mm）"),
                })

    # 标准开本（V0.9）：按成品尺寸匹配 A/B 系列与 16/32 开
    if boxes:
        first_box = boxes[0]
        size_mm = first_box["trim_mm"] or first_box["crop_mm"]
        fmt = _match_page_format(size_mm)
        fmt["size_mm"] = size_mm
        pdf["page_format"] = fmt
        if not fmt["match"] and len({tuple(b["crop_mm"]) for b in boxes}) == 1:
            issues.append({
                "level": "info",
                "message": f"成品尺寸 {size_mm[0]}×{size_mm[1]} mm 未匹配常见开本（A/B 系列、16/32 开），请确认是否为异形开本",
            })

    # 图像有效 DPI（V0.9）：内容流 CTM 解析，按最差绘制尺寸判定
    dpi_items = pdf["effective_dpi"]["items"]
    if dpi_items:
        by_key: dict[tuple, float] = {}
        for item in dpi_items:
            key = (item["page"], item["name"])
            if key not in by_key or item["dpi"] < by_key[key]:
                by_key[key] = item["dpi"]
        for im in pdf["images"]:
            im["effective_dpi"] = by_key.get((im["page"], im.get("resource", "")))
        pdf["effective_dpi"]["min"] = min(by_key.values())
        low = [item for item in dpi_items if item["dpi"] < 300]
        pdf["effective_dpi"]["low_count"] = len(low)
        if low:
            worst = min(low, key=lambda i: i["dpi"])
            issues.append({
                "level": "warn",
                "message": (f"{len(low)} 处图像有效分辨率低于 300 DPI（最低 {worst['dpi']} DPI，"
                            f"第 {worst['page']} 页 {worst['px'][0]}×{worst['px'][1]} px），印刷可能发虚"),
            })
    elif pdf["images"]:
        # 有位图资源但内容流未解析出绘制尺寸（内联图像/异常内容流）
        pdf["effective_dpi"]["checked"] = False

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
