"""测试样张生成器（V0.5 可信测量闭环 · M3，方法标准化的载体）。

Pillow CMYK 栅格样张（A4 / 300dpi），**设备值直出**（不做 ICC 转换——
样张测的是设备自身表现），每个测量目标**双点重复布局**（同标签左右两份），
直接服务 M1 复测一致性规则：导入后同标签两块互为复测对。

布局（左右两个对称半版，内容一致）：
- 实地/叠印条：C M Y K R(M+Y) G(C+Y) B(C+M) W(纸白)；
- 梯尺：C/M/Y/K 各 0→100 共 11 级（标签 c0..c100 / m0.. / y0.. / k0..，
  与 M2 的 parse_ink_label 直接兼容）；
- 灰平衡格：GB1(C50M40Y40) GB2(C60M50Y50) GB3(C25M20Y20)；
- 角线、页眉（工艺名 + 日期）与页脚说明。

输出 TIFF + PDF（Pillow 原生 CMYK PDF 编码，单页无需 pypdf 组页）。
"""

from __future__ import annotations

import base64
import io
from datetime import date

from PIL import Image, ImageDraw, ImageFont

DPI_DEFAULT = 300
PAGE_MM = (210.0, 297.0)

_INK_STEPS = list(range(0, 101, 10))
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

    # 梯尺：每色一行 11 级
    y += size + 34
    step_w = (w - 10 * 6) // 11
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

    # 输出：TIFF（CMYK）+ PDF（Pillow 原生 DeviceCMYK）+ PNG 预览
    tiff_buf = io.BytesIO()
    img.save(tiff_buf, format="TIFF", dpi=(dpi, dpi), compression="tiff_lzw")
    pdf_buf = io.BytesIO()
    img.save(pdf_buf, format="PDF", resolution=dpi)
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
    }
