"""API 层测试：印前预检与转换。"""

import base64
import io

import pytest
from PIL import Image

pytestmark = pytest.mark.api


def _png_b64(icc: bytes | None = None) -> str:
    img = Image.new("RGB", (32, 32), (170, 120, 90))
    buf = io.BytesIO()
    kw = {"format": "PNG"}
    if icc:
        kw["icc_profile"] = icc
    img.save(buf, **kw)
    return base64.b64encode(buf.getvalue()).decode()


def test_preflight_no_icc_warn(api_client):
    r = api_client.post(
        "/api/files/preflight",
        json={"image_b64": _png_b64(), "filename": "noicc.png"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "warn"
    assert data["has_embedded_icc"] is False


def test_preflight_with_cmyk_icc_pass(api_client, cmyk_icc):
    r = api_client.post(
        "/api/files/preflight",
        json={"image_b64": _png_b64(cmyk_icc), "filename": "withicc.png"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "pass"
    assert data["embedded_icc"]["device_space"] == "CMYK"


def test_convert_to_cmyk_tiff(api_client, cmyk_icc):
    proc = api_client.post(
        "/api/processes",
        params={"name": "转换工艺"},
        content=cmyk_icc,
        headers={"Content-Type": "application/octet-stream"},
    ).json()
    r = api_client.post(
        "/api/files/convert",
        json={"image_b64": _png_b64(), "process_id": proc["id"]},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["output_mode"] == "CMYK"
    assert data["file_format"] == "TIFF"
    raw = io.BytesIO(base64.b64decode(data["file_b64"].split(",", 1)[1]))
    assert Image.open(raw).mode == "CMYK"


def test_convert_unknown_process_4xx(api_client):
    r = api_client.post(
        "/api/files/convert",
        json={"image_b64": _png_b64(), "process_id": "nope"},
    )
    assert r.status_code in (400, 404)


# ---------- V0.4 PDF 预检 ----------

def _pdf_bytes(mode: str = "RGB", size=(620, 877)) -> bytes:
    """Pillow 运行时生成测试 PDF（RGB → DeviceRGB 图像；CMYK → DeviceCMYK 图像）。"""
    img = Image.new(mode, size, (180, 40, 40) if mode == "RGB" else (10, 90, 90, 5))
    buf = io.BytesIO()
    img.save(buf, format="PDF", resolution=150.0)
    return buf.getvalue()


def _encrypted_pdf(src: bytes) -> bytes:
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(io.BytesIO(src))
    writer = PdfWriter()
    writer.append(reader)
    writer.encrypt("secret")
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def test_pdf_preflight_rgb_warns(api_client):
    """RGB 图像 PDF：无输出意图 warn + RGB 图像 warn + 缩略图。"""
    r = api_client.post(
        "/api/files/preflight",
        json={"image_b64": base64.b64encode(_pdf_bytes("RGB")).decode(), "filename": "a.pdf"},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["format"] == "PDF"
    assert data["pages"] == 1
    assert data["status"] == "warn"
    assert data["pdf"]["has_output_intent"] is False
    assert data["pdf"]["images"] and data["pdf"]["images"][0]["colorspace"].startswith("DeviceRGB")
    assert any("输出意图" in i["message"] for i in data["issues"])
    assert any("RGB" in i["message"] for i in data["issues"])
    assert data["thumbnail_b64"] and data["thumbnail_b64"].startswith("data:image/png")
    print("[PASS] PDF 预检（RGB 告警 + 缩略图）")


def test_pdf_preflight_cmyk_no_rgb_warn(api_client):
    """CMYK 图像 PDF：不应触发 RGB 告警（输出意图缺失仍 warn）。"""
    r = api_client.post(
        "/api/files/preflight",
        json={"image_b64": base64.b64encode(_pdf_bytes("CMYK")).decode(), "filename": "c.pdf"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["pdf"]["images"][0]["colorspace"].startswith("DeviceCMYK")
    assert not any("RGB 色彩空间" in i["message"] for i in data["issues"])
    assert any("输出意图" in i["message"] for i in data["issues"])
    print("[PASS] PDF 预检（CMYK 无 RGB 告警）")


def test_pdf_preflight_encrypted_error(api_client):
    """加密 PDF：status=error。"""
    r = api_client.post(
        "/api/files/preflight",
        json={"image_b64": base64.b64encode(_encrypted_pdf(_pdf_bytes("RGB"))).decode(), "filename": "e.pdf"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "error"
    assert any(i["level"] == "error" for i in data["issues"])
    print("[PASS] 加密 PDF 返回 error")


def test_pdf_preflight_corrupt_400(api_client):
    r = api_client.post(
        "/api/files/preflight",
        json={"image_b64": base64.b64encode(b"%PDF-1.4 broken").decode(), "filename": "x.pdf"},
    )
    assert r.status_code == 400


def test_convert_rejects_pdf(api_client, cmyk_icc):
    """PDF 仅预检不支持转换：明确 400 报错。"""
    proc = api_client.post(
        "/api/processes",
        params={"name": "PDF转换工艺"},
        content=cmyk_icc,
        headers={"Content-Type": "application/octet-stream"},
    ).json()
    r = api_client.post(
        "/api/files/convert",
        json={"image_b64": base64.b64encode(_pdf_bytes("RGB")).decode(), "process_id": proc["id"]},
    )
    assert r.status_code == 400
    assert "PDF" in r.json()["detail"]
    print("[PASS] PDF 转换明确拒绝（400）")


# ---------- V0.9 PDF 预检深水区：字体 / 出血 / 有效 DPI / 开本 ----------

def _synthetic_pdf(*, trim: bool = True, bleed_mm: float = 3.0,
                   embedded_font: bool = False, image_px=None,
                   draw_pt: float = 100.0,
                   image_colorspace: str = "/DeviceCMYK") -> bytes:
    """构造可控 PDF：A4 成品 + 指定出血、字体嵌入状态、图像绘制尺寸。"""
    from pypdf import PdfWriter
    from pypdf.generic import (
        ArrayObject, DecodedStreamObject, DictionaryObject, FloatObject,
        NameObject, NumberObject,
    )

    mm = 72.0 / 25.4
    a4_w, a4_h = 210.0 * mm, 297.0 * mm
    pad = bleed_mm * mm
    media = [-pad, -pad, a4_w + pad, a4_h + pad]

    writer = PdfWriter()
    page = writer.add_blank_page(width=media[2] - media[0], height=media[3] - media[1])
    page[NameObject("/MediaBox")] = ArrayObject([FloatObject(v) for v in media])
    page[NameObject("/BleedBox")] = ArrayObject([FloatObject(v) for v in media])
    if trim:
        page[NameObject("/TrimBox")] = ArrayObject([
            FloatObject(0), FloatObject(0), FloatObject(a4_w), FloatObject(a4_h),
        ])

    if embedded_font:
        font_file = DecodedStreamObject()
        font_file.set_data(b"\x00" * 32)
        desc = DictionaryObject({
            NameObject("/Type"): NameObject("/FontDescriptor"),
            NameObject("/FontName"): NameObject("/TestEmbedded"),
            NameObject("/Flags"): NumberObject(4),
            NameObject("/FontBBox"): ArrayObject([NumberObject(0)] * 4),
            NameObject("/ItalicAngle"): NumberObject(0),
            NameObject("/Ascent"): NumberObject(800),
            NameObject("/Descent"): NumberObject(-200),
            NameObject("/CapHeight"): NumberObject(700),
            NameObject("/StemV"): NumberObject(80),
            NameObject("/FontFile2"): writer._add_object(font_file),
        })
        font = DictionaryObject({
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/TrueType"),
            NameObject("/BaseFont"): NameObject("/TestEmbedded"),
            NameObject("/FontDescriptor"): writer._add_object(desc),
        })
    else:
        font = DictionaryObject({
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        })

    resources = DictionaryObject({
        NameObject("/Font"): DictionaryObject({
            NameObject("/F1"): writer._add_object(font),
        }),
    })
    ops = b"BT /F1 12 Tf 20 20 Td (Test) Tj ET"

    if image_px:
        iw, ih = image_px
        img = DecodedStreamObject()
        img[NameObject("/Type")] = NameObject("/XObject")
        img[NameObject("/Subtype")] = NameObject("/Image")
        img[NameObject("/Width")] = NumberObject(iw)
        img[NameObject("/Height")] = NumberObject(ih)
        img[NameObject("/ColorSpace")] = NameObject(image_colorspace)
        img[NameObject("/BitsPerComponent")] = NumberObject(8)
        channels = 3 if image_colorspace == "/DeviceRGB" else 4
        # DeviceRGB 全 0 为黑色；DeviceCMYK 全 0 为 0% 墨量纯白，需填入可见油墨像素避免整页白板
        pixel = b"\x00\x00\x00" if channels == 3 else b"\x00\x80\x80\x19"
        img.set_data(pixel * (iw * ih))
        resources[NameObject("/XObject")] = DictionaryObject({
            NameObject("/Im0"): writer._add_object(img),
        })
        ops += f" q {draw_pt} 0 0 {draw_pt} 0 0 cm /Im0 Do Q".encode()

    page[NameObject("/Resources")] = resources
    content = DecodedStreamObject()
    content.set_data(ops)
    page[NameObject("/Contents")] = writer._add_object(content)

    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _post_pdf(api_client, data: bytes):
    return api_client.post(
        "/api/files/preflight",
        json={"image_b64": base64.b64encode(data).decode(), "filename": "synthetic.pdf"},
    )


def test_pdf_preflight_font_not_embedded_warns(api_client):
    """未嵌入的 Type1 字体 → 明确列出字体名并告警。"""
    data = _post_pdf(api_client, _synthetic_pdf(embedded_font=False)).json()
    fe = data["pdf"]["font_embedding"]
    assert fe["total"] == 1 and fe["embedded"] == 0
    assert fe["not_embedded"][0]["name"] == "Helvetica"
    assert fe["not_embedded"][0]["subtype"] == "Type1"
    assert any("字体未嵌入" in i["message"] for i in data["issues"])


def test_pdf_preflight_font_embedded_passes(api_client):
    """FontDescriptor + FontFile2 → 判定嵌入，不再告警。"""
    data = _post_pdf(api_client, _synthetic_pdf(embedded_font=True)).json()
    fe = data["pdf"]["font_embedding"]
    assert fe["total"] == 1 and fe["embedded"] == 1
    assert fe["not_embedded"] == []
    assert not any("字体未嵌入" in i["message"] for i in data["issues"])


def test_pdf_preflight_bleed_and_format_ok(api_client):
    """A4 成品 + 3mm 出血 → 出血达标、开本匹配。"""
    data = _post_pdf(api_client, _synthetic_pdf(bleed_mm=3.0)).json()
    pdf = data["pdf"]
    assert pdf["bleed"]["ok"] is True
    assert pdf["bleed"]["min_mm"] == 3.0
    assert pdf["page_format"]["name"] == "A4"
    assert pdf["page_boxes"][0]["bleed_sides_mm"] == [3.0, 3.0, 3.0, 3.0]
    assert not any("出血不足" in i["message"] for i in data["issues"])


def test_pdf_preflight_bleed_insufficient_warns(api_client):
    """TrimBox 与 MediaBox 重合（0 出血）→ 告警。"""
    data = _post_pdf(api_client, _synthetic_pdf(bleed_mm=0.0)).json()
    assert data["pdf"]["bleed"]["ok"] is False
    assert data["pdf"]["bleed"]["min_mm"] == 0.0
    assert any("出血不足" in i["message"] for i in data["issues"])


def test_pdf_preflight_trim_missing_warns(api_client):
    """未设 TrimBox → 无法判定成品尺寸，给出设定指引。"""
    data = _post_pdf(api_client, _synthetic_pdf(trim=False)).json()
    assert any("未设置 TrimBox" in i["message"] for i in data["issues"])
    assert data["pdf"]["bleed"]["checked"] is True  # 有 MediaBox，仍可计算


def test_pdf_preflight_effective_dpi_low(api_client):
    """50px 图像绘制到 100pt → 36 DPI，按最差绘制尺寸告警。"""
    data = _post_pdf(api_client, _synthetic_pdf(image_px=(50, 50), draw_pt=100.0)).json()
    assert data["pdf"]["effective_dpi"]["min"] == 36.0
    assert data["pdf"]["effective_dpi"]["low_count"] == 1
    assert data["pdf"]["images"][0]["effective_dpi"] == 36.0
    assert any("低于 300 DPI" in i["message"] for i in data["issues"])


def test_pdf_preflight_effective_dpi_ok(api_client):
    """500px 图像绘制到 100pt → 360 DPI，不告警。"""
    data = _post_pdf(api_client, _synthetic_pdf(image_px=(500, 500), draw_pt=100.0)).json()
    assert data["pdf"]["effective_dpi"]["min"] == 360.0
    assert data["pdf"]["effective_dpi"]["low_count"] == 0
    assert not any("低于 300 DPI" in i["message"] for i in data["issues"])
