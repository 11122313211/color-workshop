"""M3 测试样张生成器测试：结构、双点配对、CMYK 值与 PDF/TIFF 可读性。"""

from __future__ import annotations

import base64
import io

import pytest
from PIL import Image

from app.services.testform import _INK_STEPS, generate_testform


@pytest.fixture(scope="module")
def form():
    return generate_testform(process_name="测试工艺", dpi=300)


def test_structure_and_pairs(form):
    assert form["dpi"] == 300
    assert form["page_mm"] == [210.0, 297.0]
    assert form["pairs_count"] > 30  # 实地 7（W 除外）+ 梯尺 4×10 + 灰平衡 3
    labels = form["labels"]
    for must in ("C", "M", "Y", "K", "R", "G", "B", "W", "GB1", "c60", "m100", "y10", "k10"):
        assert must in labels, must
    # 双点：每个标签成对且坐标不同
    for pair in form["pairs"][:5]:
        assert pair["pos_a_mm"] != pair["pos_b_mm"]
        assert len(pair["pos_a_mm"]) == 2


def test_pdf_and_tiff_readable(form):
    pdf_bytes = base64.b64decode(form["pdf_b64"])
    assert pdf_bytes[:5] == b"%PDF-"
    img = Image.open(io.BytesIO(base64.b64decode(form["tiff_b64"])))
    assert img.mode == "CMYK"
    w, h = img.size
    assert w == 2480 and h == 3508  # A4 @300dpi
    preview = Image.open(io.BytesIO(base64.b64decode(form["preview_b64"].split(",", 1)[1])))
    assert preview.mode == "RGB"


def test_patch_values_direct_device(form):
    """设备值直出：C 实地像素 = (255,0,0,0)；c100 梯尺末级 = C 满版；W = 纸白。"""
    img = Image.open(io.BytesIO(base64.b64decode(form["tiff_b64"])))
    # 用配对坐标反查：c100 的 pos_a
    pair = next(p for p in form["pairs"] if p["label"] == "c100")
    x, y = pair["pos_a_mm"]
    px = int(x / 25.4 * 300)
    py = int(y / 25.4 * 300)
    c, m, yy, k = img.getpixel((px, py))
    assert c > 240 and m == 0 and yy == 0 and k == 0
    # W 纸白
    pair_w = next(p for p in form["pairs"] if p["label"] == "W")
    wx, wy = pair_w["pos_a_mm"]
    c2, m2, y2, k2 = img.getpixel((int(wx / 25.4 * 300), int(wy / 25.4 * 300)))
    assert (c2, m2, y2, k2) == (0, 0, 0, 0)
    # K 实地
    pair_k = next(p for p in form["pairs"] if p["label"] == "K")
    kx, ky = pair_k["pos_a_mm"]
    c3, m3, y3, k3 = img.getpixel((int(kx / 25.4 * 300), int(ky / 25.4 * 300)))
    assert k3 == 255 and c3 == 0


def test_tvi_labels_compatible_with_m2(form):
    """梯尺标签可直接被 M2 parse_ink_label 识别并配对 TVI（K 梯尺不参与 TVI）。"""
    from app.services.density import parse_ink_label

    for label in ("c60", "m30", "y40"):
        channel, nominal = parse_ink_label(label)
        assert nominal is not None and channel in "CMY"
    # K 梯尺标签不属于 TVI 目标（ink_metrics 需 C/M/Y 三滤色，会被聚合器自然跳过）
    assert parse_ink_label("k100")[1] is None


def test_dpi_scaling():
    f600 = generate_testform(process_name="", dpi=600)
    img = Image.open(io.BytesIO(base64.b64decode(f600["tiff_b64"])))
    assert img.size == (4961, 7016)
    assert f600["pairs_count"] > 30
