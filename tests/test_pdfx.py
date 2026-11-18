"""V0.9 P0-2 PDF/X-4 结构封装测试。

覆盖：OutputIntent（工艺 ICC + /GTS_PDFX）/ XMP GTS_PDFXVersion / TrimBox+BleedBox
写入与保留；严格模式阻断未嵌入字体与 RGB 图像；非严格模式如实降级；缺 TrimBox
补全告警；非 PDF / 未知工艺的错误码。
"""

from __future__ import annotations

import base64
import io

import pytest
from pypdf import PdfReader

from tests.test_api_files import _synthetic_pdf

pytestmark = pytest.mark.api


def _post(api_client, process_id: str, pdf: bytes, **params):
    return api_client.post(
        "/api/files/pdfx",
        params={"process_id": process_id, "filename": "cover.pdf", **params},
        content=pdf,
        headers={"Content-Type": "application/pdf"},
    )


def _decode(result: dict) -> bytes:
    assert result["file_b64"].startswith("data:application/pdf;base64,")
    return base64.b64decode(result["file_b64"].split(",", 1)[1])


def test_pdfx_packages_structure(api_client, process_factory, cmyk_icc):
    """干净的 CMYK PDF → OutputIntent + XMP + 盒子齐全。"""
    proc = process_factory("PDFX 工艺")
    pdf = _synthetic_pdf(embedded_font=True, image_px=(500, 500))
    r = _post(api_client, proc["id"], pdf, strict="1")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["mode"] == "PDF/X-4"
    assert data["status"] == "pass" and data["compliant"] is True
    assert data["filename"] == "cover-PDFX4.pdf"
    assert data["output_intent"]["channels"] == 4
    assert data["pages"] == 1

    raw = _decode(data)
    reader = PdfReader(io.BytesIO(raw))
    root = reader.trailer["/Root"]
    oi = root["/OutputIntents"][0].get_object()
    assert str(oi["/S"]) == "/GTS_PDFX"
    profile = oi["/DestOutputProfile"].get_object()
    assert int(profile["/N"]) == 4
    assert bytes(profile.get_data()) == cmyk_icc
    assert str(oi["/OutputConditionIdentifier"])  # 非空

    meta = root["/Metadata"].get_object()
    xmp = meta.get_data().decode("utf-8")
    assert "GTS_PDFXVersion" in xmp and "PDF/X-4" in xmp

    page = reader.pages[0]
    assert "/TrimBox" in page and "/BleedBox" in page
    assert abs(float(page.trimbox.width) - 210 * 72 / 25.4) < 0.1
    assert abs(float(page.bleedbox.width) - 216 * 72 / 25.4) < 0.1

    # 回读预检：输出意图已被识别
    again = api_client.post("/api/files/preflight", json={
        "image_b64": base64.b64encode(raw).decode(), "filename": "out.pdf",
    }).json()
    assert again["pdf"]["has_output_intent"] is True


def test_pdfx_strict_rejects_unembedded_font(api_client, process_factory):
    """严格模式：字体未嵌入 → 422，且说明原因。"""
    proc = process_factory("PDFX 字体工艺")
    pdf = _synthetic_pdf(embedded_font=False)
    r = _post(api_client, proc["id"], pdf, strict="1")
    assert r.status_code == 422
    assert "字体未嵌入" in r.json()["detail"]


def test_pdfx_lenient_warns_but_packages(api_client, process_factory):
    """非严格：仍生成结构文件，但 status=warn / compliant=false。"""
    proc = process_factory("PDFX 宽松工艺")
    pdf = _synthetic_pdf(embedded_font=False)
    r = _post(api_client, proc["id"], pdf, strict="0")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "warn"
    assert data["compliant"] is False
    assert any("字体未嵌入" in c["message"] for c in data["checks"])

    reader = PdfReader(io.BytesIO(_decode(data)))
    assert "/OutputIntents" in reader.trailer["/Root"]


def test_pdfx_strict_rejects_rgb_image(api_client, process_factory):
    """严格模式：DeviceRGB 图像 → 422（CMYK 输出意图下需先转换）。"""
    proc = process_factory("PDFX RGB 工艺")
    pdf = _synthetic_pdf(embedded_font=True, image_px=(500, 500),
                         image_colorspace="/DeviceRGB")
    r = _post(api_client, proc["id"], pdf, strict="1")
    assert r.status_code == 422
    assert "RGB" in r.json()["detail"]


def test_pdfx_rgb_output_intent_allows_device_rgb(api_client, process_factory, rgb_icc):
    """RGB 输出意图下 DeviceRGB 图像不是阻断项（仍提示非 CMYK 交付口径）。"""
    proc = process_factory("PDFX RGB 工艺", icc=rgb_icc)
    pdf = _synthetic_pdf(embedded_font=True, image_px=(200, 200),
                         image_colorspace="/DeviceRGB")
    r = _post(api_client, proc["id"], pdf, strict="1")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["compliant"] is True
    assert data["output_intent"]["channels"] == 3
    assert any("通道工艺" in c["message"] for c in data["checks"])


def test_pdfx_missing_trimbox_is_derived_with_warning(api_client, process_factory):
    """缺 TrimBox → 按 MediaBox 补成品线并告警。"""
    proc = process_factory("PDFX 盒子工艺")
    pdf = _synthetic_pdf(trim=False, embedded_font=True)
    r = _post(api_client, proc["id"], pdf, strict="1")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "warn"
    assert any("TrimBox" in c["message"] for c in data["checks"])
    page = PdfReader(io.BytesIO(_decode(data))).pages[0]
    assert "/TrimBox" in page
    assert "/BleedBox" in page


def test_pdfx_rejects_non_pdf_and_unknown_process(api_client, process_factory):
    proc = process_factory("PDFX 守卫工艺")
    r = _post(api_client, proc["id"], b"\x89PNG\r\n\x1a\n", strict="1")
    assert r.status_code == 422
    assert "PDF" in r.json()["detail"]

    r = _post(api_client, "nope", _synthetic_pdf(embedded_font=True), strict="1")
    assert r.status_code == 404

    r = api_client.post("/api/files/pdfx", params={"process_id": proc["id"]},
                        content=b"", headers={"Content-Type": "application/pdf"})
    assert r.status_code == 400