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
