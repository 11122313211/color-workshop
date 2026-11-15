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
