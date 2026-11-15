"""测试样张生成器（V0.5 可信测量闭环 · M3，V0.6 目视诊断扩展）。

Pillow CMYK 栅格样张（A4 / 300dpi），**设备值直出**（不做 ICC 转换——
样张测的是设备自身表现），每个测量目标**双点重复布局**（同标签左右两份），
直接服务 M1 复测一致性规则：导入后同标签两块互为复测对。

布局（左右两个对称半版，内容一致）：
- 实地/叠印条：C M Y K R(M+Y) G(C+Y) B(C+M) W(纸白)；
- 梯尺：C/M/Y/K 各 12 级（0→100 步 10 + 75——75% 梯级供相对反差 K 值计算，
  标签 c0..c100 / m0.. / y0.. / k0..，与 density.parse_ink_label 直接兼容）；
- 灰平衡格：GB1(C50M40Y40) GB2(C60M50Y50) GB3(C25M20Y20)；
- 角线、页眉（工艺名 + 日期）与页脚说明。

第二页「目视诊断区」（V0.6，方法源自 ISO300 类测试版与 GATF 判读口诀）：
- 星标 ×2（约 35:1 形变放大：中心均匀黑化=网点增大、方向性楔形=滑版/条杠、
  偏向非对称=重影、中心白点扩大=网点收缩）；
- 正向/反白细线组（0.01/0.05/0.4/0.6pt，横向+纵向，极细网点还原考核）；
- 套准标记（圆/三角撞色叠印 + 十字圆环，量化套印误差）。
目视目标不进 labels/pairs（不做测量配对）。

输出 TIFF + PDF（Pillow 原生 CMYK 多页编码，无需 pypdf 组页），预览为第一页。
"""

from __future__ import annotations

import base64
import io
import math
from datetime import date

from PIL import Image, ImageDraw, ImageFont

DPI_DEFAULT = 300
PAGE_MM = (210.0, 297.0)

# 0→100 步 10 + 75：75% 是相对反差 K=(Ds−D75)/Ds 的关键控制点
_INK_STEPS = sorted(set(list(range(0, 101, 10)) + [75]))
# CMYK 元组顺序 (C, M, Y, K)；R=M100Y100，G=C100Y100，B=C100M100
_SOLIDS = [
    ("C", (255, 0, 0, 0)),
    ("M", (0, 255, 0, 0)),
    ("Y", (0, 0, 255, 0)),
    ("K", (0, 0, 0, 255)),
    ("R", (0, 255, 255, 0)),   # M100Y100
    ("G", (255, 0, 255, 0)),   # C100Y100
    ("B", (255, 255, 0, 0)),   # C100M100
    ("W", (0, 0, 0, 0)),       # 纸白
]
_GRAYS = [
    ("GB1", (128, 102, 102, 0)),   # C50M40Y40
    ("GB2", (153, 128, 128, 0)),   # C60M50Y50
    ("GB3", (64, 51, 51, 0)),      # C25M20Y20
]
_K255 = 255

# ---------------------------------------------------------------------------
# V0.7 校准版：ISO 12647-7 控制条精神的编号色块集（28 patch，双点重复）
# ---------------------------------------------------------------------------

#: 校准梯级：100/75/50/25/10（ISO 12647-7:2016 控制条布局）
_CAL_TINT_STEPS = (100, 75, 50, 25, 10)
#: 叠印实地（R=M100Y100 G=C100Y100 B=C100M100，与验证样张 _SOLIDS 一致）与灰平衡格
CAL_PATCHES: list[dict] = []
_i = 0
for _ch, _name in enumerate("CMYK"):
    for _t in _CAL_TINT_STEPS:
        _i += 1
        _vals = [0, 0, 0, 0]
        _vals[_ch] = _t
        CAL_PATCHES.append({"index": f"P{_i:02d}", "label": f"{_name}{_t}", "device": _vals})
for _label, _vals in (
    ("R", (0, 100, 100, 0)), ("G", (100, 0, 100, 0)),
    ("B", (100, 100, 0, 0)), ("CMY", (100, 100, 100, 0)),
    ("W", (0, 0, 0, 0)),
    ("GB1", (50, 40, 40, 0)), ("GB2", (60, 50, 50, 0)), ("GB3", (25, 20, 20, 0)),
):
    _i += 1
    CAL_PATCHES.append({"index": f"P{_i:02d}", "label": _label, "device": list(_vals)})

#: 编号→标签 映射（测量导入时 P 编号与标签等价配对，大小写不敏感）
CAL_INDEX_MAP = {p["index"].upper(): p["label"] for p in CAL_PATCHES}


def _cmyk(value: int, channel: int) -> tuple[int, int, int, int]:
    """单通道网点值（0-100% → 0-255）。channel: 0=C 1=M 2=Y 3=K。"""
    v = round(value / 100.0 * _K255)
    vals = [0, 0, 0, 0]
    vals[channel] = v
    return tuple(vals)


def _load_font(size: int):
    for name in ("arial.ttf", "segoeui.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _draw_half(draw: ImageDraw.ImageDraw, x0: int, y0: int, w: int, font: ImageFont.FreeTypeFont,
               small: ImageFont.FreeTypeFont) -> list[tuple[str, int, int]]:
    """绘制一个半版的全部目标。返回 [(label, patch 中心 x, y)]（页面坐标，供 pairs 报告）。"""
    positions: list[tuple[str, int, int]] = []
    x = x0
    y = y0

    # 实地/叠印条
    size = (w - 7 * 8) // 8
    for label, color in _SOLIDS:
        draw.rectangle([x, y, x + size, y + size], fill=color, outline=(0, 0, 0, 255))
        draw.text((x + size / 2, y + size + 4), label, font=small, fill=(0, 0, 0, 255), anchor="ma")
        positions.append((label, x + size // 2, y + size // 2))
        x += size + 8

    # 梯尺：每色一行 12 级（含 75%）
    y += size + 34
    step_w = (w - (len(_INK_STEPS) - 1) * 6) // len(_INK_STEPS)
    for ch, name in enumerate("CMYK"):
        row_y = y + ch * 92
        for i, v in enumerate(_INK_STEPS):
            xx = x0 + i * (step_w + 6)
            draw.rectangle([xx, row_y, xx + step_w, row_y + 56],
                           fill=_cmyk(v, ch), outline=(0, 0, 0, 255))
            label = f"{name.lower()}{v}"
            draw.text((xx + step_w / 2, row_y + 60), label, font=small,
                      fill=(0, 0, 0, 255), anchor="ma")
            if v >= 10:  # 0 级为纸白，不作为配对复测目标
                positions.append((label, xx + step_w // 2, row_y + 28))
    y += 4 * 92 + 8

    # 灰平衡格
    for i, (label, color) in enumerate(_GRAYS):
        xx = x0 + i * (size + 8)
        draw.rectangle([xx, y, xx + size, y + size], fill=color, outline=(0, 0, 0, 255))
        draw.text((xx + size / 2, y + size + 4), label, font=small, fill=(0, 0, 0, 255), anchor="ma")
        positions.append((label, xx + size // 2, y + size // 2))
    return positions


# ---------------------------------------------------------------------------
# 第二页：目视诊断区（星标 / 细线 / 套准，不参与测量配对）
# ---------------------------------------------------------------------------

# 星标：36 细楔形（黑:白 ≈ 36%），约 35:1 形变放大
_STAR_WEDGES = 36
_FINE_WIDTHS_PT = (0.01, 0.05, 0.4, 0.6)


def _draw_star_target(draw: ImageDraw.ImageDraw, cx: int, cy: int, r: int, dpi: int) -> None:
    """GATF 星标：楔形自中心放射，中心小白点（网点收缩时白点扩大）。"""
    for i in range(_STAR_WEDGES):
        a0 = i * (2 * math.pi / _STAR_WEDGES)
        a1 = a0 + math.radians(3.6)
        pts = [(cx, cy),
               (cx + r * math.cos(a0), cy + r * math.sin(a0)),
               (cx + r * math.cos(a1), cy + r * math.sin(a1))]
        draw.polygon(pts, fill=(0, 0, 0, 255))
    rc = round(1.5 / 25.4 * dpi)
    draw.ellipse([cx - rc, cy - rc, cx + rc, cy + rc], fill=(0, 0, 0, 0))


def _draw_fine_lines(draw: ImageDraw.ImageDraw, x0: int, y: int, w: int, dpi: int,
                     font: ImageFont.FreeTypeFont, reverse: bool = False) -> None:
    """细线组：0.01/0.05/0.4/0.6pt 横+纵线条；reverse=True 时黑底反白。"""
    line_color = (0, 0, 0, 0) if reverse else (0, 0, 0, 255)   # 反白=白线
    bg = (0, 0, 0, 255) if reverse else (0, 0, 0, 0)
    n = len(_FINE_WIDTHS_PT)
    gw = (w - (n - 1) * 10) // n
    gh = round(24 / 25.4 * dpi)
    for i, wpt in enumerate(_FINE_WIDTHS_PT):
        xx = x0 + i * (gw + 10)
        draw.rectangle([xx, y, xx + gw, y + gh], fill=bg, outline=(0, 0, 0, 255))
        px = max(1, round(wpt / 72.0 * dpi))
        cy = y + gh // 2
        draw.line([(xx + 4, cy), (xx + gw - 4, cy)], fill=line_color, width=px)
        cxl = xx + gw // 2
        draw.line([(cxl, y + 4), (cxl, y + gh - 4)], fill=line_color, width=px)
        draw.text((xx + gw / 2, y + gh + 3), f"{wpt}pt{' 反白' if reverse else ''}",
                  font=font, fill=(0, 0, 0, 255), anchor="ma")


def _draw_registrations(draw: ImageDraw.ImageDraw, x0: int, y: int, dpi: int,
                        font: ImageFont.FreeTypeFont) -> None:
    """套准标记：圆/三角撞色叠印（错位 1.5mm）+ 十字圆环。"""
    mm = lambda v: round(v / 25.4 * dpi)  # noqa: E731
    # 撞色对（色序 KCMY 相邻色组），圆=先印色，三角=后印色
    combos = [(("C", (255, 0, 0, 0)), ("M", (0, 255, 0, 0))),
              (("M", (0, 255, 0, 0)), ("Y", (0, 0, 255, 0))),
              (("Y", (0, 0, 255, 0)), ("K", (0, 0, 0, 255))),
              (("K", (0, 0, 0, 255)), ("C", (255, 0, 0, 0)))]
    r = mm(6)
    step = mm(26)
    for i, (c1, c2) in enumerate(combos):
        cx = x0 + i * step + r
        cy = y + r
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=c1[1], width=mm(0.5))
        off = mm(1.5)  # 设计错位：套准正确时错位恰好 1.5mm，超过即套印误差
        pts = [(cx + off, cy - r), (cx + off - r * 0.9, cy + r * 0.75),
               (cx + off + r * 0.9, cy + r * 0.75)]
        draw.polygon(pts, outline=c2[1], width=mm(0.5))
        draw.text((cx, cy + r + 3), f"{c1[0]}+{c2[0]}", font=font,
                  fill=(0, 0, 0, 255), anchor="ma")
    # 十字圆环套准标（经典标记）
    cx = x0 + 4 * step + r + mm(4)
    cy = y + r
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(0, 0, 0, 255), width=mm(0.5))
    draw.line([(cx - r - mm(3), cy), (cx + r + mm(3), cy)], fill=(0, 0, 0, 255), width=mm(0.5))
    draw.line([(cx, cy - r - mm(3)), (cx, cy + r + mm(3))], fill=(0, 0, 0, 255), width=mm(0.5))
    draw.text((cx, cy + r + 3), "十", font=font, fill=(0, 0, 0, 255), anchor="ma")


def _build_visual_page(w: int, h: int, margin: int, dpi: int, process_name: str,
                       font_title, font_meta, font_foot):
    """生成第二页（目视诊断区）的 (Image, ImageDraw)。"""
    img = Image.new("CMYK", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    title = "目视诊断区 · 星标 / 细线 / 套准"
    draw.text((margin, margin), title, font=font_title, fill=(0, 0, 0, 255))
    if process_name:
        draw.text((w - margin, margin), process_name, font=font_meta,
                  fill=(0, 0, 0, 255), anchor="ra")

    mm = lambda v: round(v / 25.4 * dpi)  # noqa: E731
    y = margin + mm(14)

    # 星标 ×2（左右两处，便于同张对比）
    r_star = mm(30)
    for cx in (w // 2 - mm(45), w // 2 + mm(45)):
        _draw_star_target(draw, cx, y + r_star, r_star, dpi)
    draw.text((w // 2, y + 2 * r_star + 6),
              "星标：中心白点完整=正常 · 均匀黑化=网点增大 · 方向性楔形=滑版/条杠 · 偏向非对称=重影 · 白点扩大=网点收缩",
              font=font_foot, fill=(0, 0, 0, 255), anchor="ma")
    y += 2 * r_star + mm(12)

    # 正向细线组
    draw.text((margin, y), "正向细线（横向+纵向）", font=font_meta, fill=(0, 0, 0, 255))
    y += mm(7)
    _draw_fine_lines(draw, margin, y, w - margin * 2, dpi, font_meta, reverse=False)
    y += mm(24 + 10)

    # 反白细线组
    draw.text((margin, y), "反白细线（黑底白线，考核反白不堵塞）", font=font_meta, fill=(0, 0, 0, 255))
    y += mm(7)
    _draw_fine_lines(draw, margin, y, w - margin * 2, dpi, font_meta, reverse=True)
    y += mm(24 + 10)

    # 套准标记
    draw.text((margin, y), "套准标记（圆/三角撞色，设计错位 1.5mm）", font=font_meta, fill=(0, 0, 0, 255))
    y += mm(7)
    _draw_registrations(draw, margin, y, dpi, font_meta)

    # 页脚
    foot = ("目视目标不参与测量配对 · 星标判读借 35:1 形变放大 · "
            "细线断线/反白堵塞即极细网点还原不足")
    draw.text((w / 2, h - margin), foot, font=font_foot, fill=(0, 0, 0, 255), anchor="ms")
    return img, draw


def generate_testform(process_name: str = "", dpi: int = DPI_DEFAULT) -> dict:
    """生成 A4 CMYK 验证样张。返回 {tiff_b64, pdf_b64, preview_b64, labels, pairs, dpi, page_mm}。"""
    w = round(PAGE_MM[0] / 25.4 * dpi)   # 2480 @300dpi
    h = round(PAGE_MM[1] / 25.4 * dpi)   # 3508
    margin = round(12 / 25.4 * dpi)
    img = Image.new("CMYK", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    font_title = _load_font(round(dpi * 0.13))   # ~11pt
    font_meta = _load_font(round(dpi * 0.09))
    font_foot = _load_font(round(dpi * 0.075))

    # 页眉
    title = f"色彩工坊 验证样张 · {process_name}" if process_name else "色彩工坊 验证样张"
    draw.text((margin, margin), title, font=font_title, fill=(0, 0, 0, 255))
    draw.text((w - margin, margin), date.today().isoformat(), font=font_meta,
              fill=(0, 0, 0, 255), anchor="ra")

    # 双点重复：左右两个半版，同标签成对
    half_w = (w - margin * 2 - round(10 / 25.4 * dpi)) // 2
    y_content = margin + round(8 / 25.4 * dpi)
    left = _draw_half(draw, margin, y_content, half_w, font_meta, font_foot)
    right_x0 = margin + half_w + round(10 / 25.4 * dpi)
    right = _draw_half(draw, right_x0, y_content, half_w, font_meta, font_foot)

    # 角线（裁切标记）
    mark = round(6 / 25.4 * dpi)
    for cx, cy, dx, dy in ((margin, margin, 1, 1), (w - margin, margin, -1, 1),
                           (margin, h - margin, 1, -1), (w - margin, h - margin, -1, -1)):
        draw.line([(cx, cy), (cx + dx * mark, cy)], fill=(0, 0, 0, 255), width=2)
        draw.line([(cx, cy), (cx, cy + dy * mark)], fill=(0, 0, 0, 255), width=2)

    # 页脚说明
    foot = ("设备值直出（未做 ICC 转换）· 同标签左右双点互为复测对 · "
            "测量时背衬使用同一批未印刷样张")
    draw.text((w / 2, h - margin), foot, font=font_foot, fill=(0, 0, 0, 255), anchor="ms")

    # 第二页：目视诊断区（星标/细线/套准，不参与测量配对）
    page2, _ = _build_visual_page(w, h, margin, dpi, process_name, font_title, font_meta, font_foot)

    # 输出：TIFF（CMYK 多页）+ PDF（Pillow 原生 DeviceCMYK 多页）+ PNG 预览（第一页）
    tiff_buf = io.BytesIO()
    img.save(tiff_buf, format="TIFF", dpi=(dpi, dpi), compression="tiff_lzw",
             save_all=True, append_images=[page2])
    pdf_buf = io.BytesIO()
    img.save(pdf_buf, format="PDF", resolution=dpi, save_all=True, append_images=[page2])
    preview = img.convert("RGB").resize((w // 5, h // 5))
    preview_buf = io.BytesIO()
    preview.save(preview_buf, format="PNG")

    # 配对报告：同标签左右坐标（mm）
    left_map = {label: (px, py) for label, px, py in left}
    pairs = []
    for label, px, py in right:
        if label in left_map:
            lx, ly = left_map[label]
            to_mm = lambda v: round(v / dpi * 25.4, 1)  # noqa: E731
            pairs.append({
                "label": label,
                "pos_a_mm": [to_mm(lx), to_mm(ly)],
                "pos_b_mm": [to_mm(px), to_mm(py)],
            })

    labels = sorted({label for label, _, _ in left})
    return {
        "tiff_b64": base64.b64encode(tiff_buf.getvalue()).decode(),
        "pdf_b64": base64.b64encode(pdf_buf.getvalue()).decode(),
        "preview_b64": "data:image/png;base64," + base64.b64encode(preview_buf.getvalue()).decode(),
        "labels": labels,
        "pairs": pairs,
        "pairs_count": len(pairs),
        "dpi": dpi,
        "page_mm": list(PAGE_MM),
        "pages": 2,
        "steps": _INK_STEPS,
    }


# ---------------------------------------------------------------------------
# V0.7 校准版：编号色块矩阵（单页，双点重复，配合色差仪逐块测量）
# ---------------------------------------------------------------------------

#: 版面几何（mm）：色块 13mm、列距 17.5mm、行距 21mm——色块间留色差仪孔位余量
_CAL_PATCH_MM = 13.0
_CAL_PITCH_X_MM = 17.5
_CAL_PITCH_Y_MM = 21.0
_CAL_COLS = 5


def _draw_calibration_half(draw: ImageDraw.ImageDraw, x0: int, y0: int, dpi: int,
                           font: ImageFont.FreeTypeFont) -> list[tuple[str, int, int]]:
    """绘制一个半版的 28 个编号色块。返回 [(标签, 中心 x, y)]（页面像素坐标）。"""
    px = lambda v: round(v / 25.4 * dpi)  # noqa: E731
    positions: list[tuple[str, int, int]] = []
    for i, p in enumerate(CAL_PATCHES):
        col = i % _CAL_COLS
        row = i // _CAL_COLS
        x = x0 + px(col * _CAL_PITCH_X_MM)
        y = y0 + px(row * _CAL_PITCH_Y_MM)
        side = px(_CAL_PATCH_MM)
        color = tuple(round(v / 100.0 * 255) for v in p["device"])
        draw.rectangle([x, y, x + side, y + side], fill=color, outline=(0, 0, 0, 255))
        draw.text((x + side / 2, y + side + px(1.2)), f"{p['index']} {p['label']}",
                  font=font, fill=(0, 0, 0, 255), anchor="ma")
        positions.append((p["label"], x + side // 2, y + side // 2))
    return positions


def generate_calibration_form(process_name: str = "", dpi: int = DPI_DEFAULT) -> dict:
    """生成 A4 CMYK 校准版（单页编号色块矩阵，双点重复）。

    返回 {tiff_b64, pdf_b64, preview_b64, patches, pairs, pairs_count, dpi, page_mm, pages}。
    patches 为 [{index,label,device(0-100 CMYK)}]——参考值对照与测量配对的唯一依据。
    """
    w = round(PAGE_MM[0] / 25.4 * dpi)
    h = round(PAGE_MM[1] / 25.4 * dpi)
    margin = round(12 / 25.4 * dpi)
    img = Image.new("CMYK", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    font_title = _load_font(round(dpi * 0.13))
    font_meta = _load_font(round(dpi * 0.09))
    font_foot = _load_font(round(dpi * 0.075))

    title = f"色彩工坊 校准版 · {process_name}" if process_name else "色彩工坊 校准版"
    draw.text((margin, margin), title, font=font_title, fill=(0, 0, 0, 255))
    draw.text((w - margin, margin), date.today().isoformat(), font=font_meta,
              fill=(0, 0, 0, 255), anchor="ra")

    # 双点重复：左右两个镜像半版
    gap = round(10 / 25.4 * dpi)
    half_w = (w - margin * 2 - gap) // 2
    y0 = margin + round(12 / 25.4 * dpi)
    left = _draw_calibration_half(draw, margin, y0, dpi, font_foot)
    right = _draw_calibration_half(draw, margin + half_w + gap, y0, dpi, font_foot)

    # 角线（裁切标记）
    mark = round(6 / 25.4 * dpi)
    for cx, cy, dx, dy in ((margin, margin, 1, 1), (w - margin, margin, -1, 1),
                           (margin, h - margin, 1, -1), (w - margin, h - margin, -1, -1)):
        draw.line([(cx, cy), (cx + dx * mark, cy)], fill=(0, 0, 0, 255), width=2)
        draw.line([(cx, cy), (cx, cy + dy * mark)], fill=(0, 0, 0, 255), width=2)

    # 测量指引（页脚两行）
    foot = ("测量指引：① 仪器校准后先测纸白（P25 W）→ ② 按 P 编号逐块测量（同编号左右两块各测一次更佳）→ "
            "③ 导出含 CIE Lab 的文本，标签保持 P 编号或色块标签")
    draw.text((w / 2, h - margin - round(5 / 25.4 * dpi)), foot, font=font_foot,
              fill=(0, 0, 0, 255), anchor="ms")
    draw.text((w / 2, h - margin), "背衬使用同一批未印刷样张 · 设备值直出（未做 ICC 转换）",
              font=font_foot, fill=(0, 0, 0, 255), anchor="ms")

    tiff_buf = io.BytesIO()
    img.save(tiff_buf, format="TIFF", dpi=(dpi, dpi), compression="tiff_lzw")
    pdf_buf = io.BytesIO()
    img.save(pdf_buf, format="PDF", resolution=dpi)
    preview = img.convert("RGB").resize((w // 5, h // 5))
    preview_buf = io.BytesIO()
    preview.save(preview_buf, format="PNG")

    left_map = {label: (px, py) for label, px, py in left}
    to_mm = lambda v: round(v / dpi * 25.4, 1)  # noqa: E731
    pairs = []
    for label, px, py in right:
        if label in left_map:
            lx, ly = left_map[label]
            pairs.append({"label": label,
                          "pos_a_mm": [to_mm(lx), to_mm(ly)],
                          "pos_b_mm": [to_mm(px), to_mm(py)]})
    return {
        "tiff_b64": base64.b64encode(tiff_buf.getvalue()).decode(),
        "pdf_b64": base64.b64encode(pdf_buf.getvalue()).decode(),
        "preview_b64": "data:image/png;base64," + base64.b64encode(preview_buf.getvalue()).decode(),
        "patches": [{**p} for p in CAL_PATCHES],
        "pairs": pairs,
        "pairs_count": len(pairs),
        "dpi": dpi,
        "page_mm": list(PAGE_MM),
        "pages": 1,
    }
