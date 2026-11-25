"""P0-#2 跨项目打通测试：PrintGuard Pro 预检报告 → color-workshop 工艺护照门禁 + 专色色域查询。

覆盖：
1. PrintGuard 导出的机读 JSON 报告被 POST /api/files/preflight 与 POST /api/files/preflight/printguard 直接消费；
2. 文件预检（图像 / PDF/X-4 / PrintGuard JSON）校验工艺护照指纹：
   - ICC 与有效护照一致 → 放行（verified / allowed=True）；
   - ICC 与护照不一致 → 拦截放行（icc_passport_mismatch / rejected）；
   - 文件未嵌入 ICC → 拦截放行（file_icc_missing / rejected）；
   - 护照指纹不匹配或工艺替换 ICC 导致护照失效 → 拦截放行（passport_hash_mismatch / passport_stale）；
3. Illustrator 桥接提取的专色清单经 POST /api/files/spot-check 自动查询是否在当前工艺色域内与真实 ΔE₀₀。
"""

from __future__ import annotations

import base64
import hashlib
import io

import pytest
from PIL import Image

from app.services.processes import ProcessStore
from app.services.recipe import forward_query
from tests.test_render_check import _vector_cmyk_pdf
from tests.test_v10_passport import _add_calibration_round, _verified_session

pytestmark = pytest.mark.api


def _make_valid_passport(api_client, process_factory, name: str = "桥接有效护照工艺") -> tuple[dict, dict]:
    proc = process_factory(name)
    sid = _verified_session(api_client, proc["id"])
    _add_calibration_round(proc, sid)
    resp = api_client.post(f"/api/processes/{proc['id']}/passport")
    assert resp.status_code == 200, resp.text
    passport = resp.json()
    assert passport["status"] == "valid"
    return proc, passport


def _png_b64(icc: bytes | None = None) -> str:
    img = Image.new("RGB", (32, 32), (160, 90, 70))
    buf = io.BytesIO()
    kw = {"format": "PNG"}
    if icc:
        kw["icc_profile"] = icc
    img.save(buf, **kw)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _sample_printguard_report(
    *,
    clean: bool = True,
    process_id: str = "",
    passport_hash: str = "",
    icc_sha256: str = "",
    spot_colors: list | None = None,
) -> dict:
    report = {
        "application": "PrintGuard Pro Prepress Workstation",
        "standard": "ISO 12647-2 / Fogra39",
        "timestamp": "2026-11-25T12:00:00.000Z",
        "box_preset": "rtc",
        "preflight_audit": {
            "rgb_cleared": clean,
            "overprint_black_active": clean,
            "tac_limited_280": clean,
            "tac_peak_measured": 210 if clean else 355,
            "tac_limit_percent": 280 if clean else 340,
            "fonts_outlined": clean,
            "glue_tab_cleared": clean,
            "bleed_mm": 3.0 if clean else 0.0,
            "misregistration_tolerance": 0.15,
            "trapping_mode": "offset",
            "barcode_audit": {
                "symbology": "EAN-13",
                "data": "6901234567895",
                "bwr_reduction_mm": 0.025,
                "quiet_zone_compliant": clean,
            },
            "ciede2000_audit": {
                "formula": "CIEDE2000 (ISO/CIE 11664-6)",
                "standard_target": "ISO 12647-2 / Fogra39",
                "delta_e_peak": 1.12 if clean else 4.25,
                "tolerance_rating": "PASS" if clean else "FAIL",
                "samples_audited": 192,
            },
        },
        "human_explanations": {
            "schema_version": "1.0",
            "source": "rule_template",
            "summary_text": "全部达标" if clean else "存在阻断缺陷",
            "counts": {
                "total": 8,
                "pass": 8 if clean else 2,
                "warn": 0 if clean else 2,
                "fail": 0 if clean else 4,
                "skip": 0,
                "safe_auto_fix": 0 if clean else 2,
                "human_confirm_required": 0 if clean else 4,
            },
        },
    }
    if process_id or passport_hash or icc_sha256:
        report["process_passport"] = {
            "process_id": process_id,
            "passport_hash": passport_hash,
            "icc_sha256": icc_sha256,
        }
    if spot_colors is not None:
        report["spot_colors"] = spot_colors
    return report


# ---------------------------------------------------------------------------
# 1. PrintGuard 机读 JSON 报告直接被预检端点消费
# ---------------------------------------------------------------------------


def test_preflight_endpoint_consumes_printguard_json_pass_and_reject(
    api_client, process_factory
):
    """POST /api/files/preflight 与 /preflight/printguard 可直接消费 PrintGuard 机读 JSON 并执行门禁。"""
    proc, passport = _make_valid_passport(api_client, process_factory, "PG报告消费工艺")

    # A. 全合规 PrintGuard 报告 + 匹配工艺护照与 ICC → 放行
    clean_report = _sample_printguard_report(
        clean=True,
        process_id=proc["id"],
        passport_hash=passport["passport_hash"],
        icc_sha256=proc["icc_sha256"],
        spot_colors=["CutContour", "PANTONE 485 C"],
    )
    r = api_client.post(
        "/api/files/preflight",
        json={"printguard_report": clean_report},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["format"] == "PrintGuard-JSON"
    assert body["passport_gate"]["allowed"] is True
    assert body["passport_gate"]["verdict"] == "verified"
    assert body["passport_gate"]["reason"] == "passport_verified"
    assert body["printguard_summary"]["release_allowed"] is True
    assert body["spot_check"] is not None
    assert body["spot_check"]["summary"]["total"] == 2

    # B. 直接把原始 PrintGuard 报告（含阻断项 + 错误 ICC 指纹）POST 给便捷端点 → 拦截放行
    dirty_report = _sample_printguard_report(
        clean=False,
        process_id=proc["id"],
        passport_hash=passport["passport_hash"],
        icc_sha256="0" * 64,
    )
    r2 = api_client.post("/api/files/preflight/printguard", json=dirty_report)
    assert r2.status_code == 200, r2.text
    body2 = r2.json()
    assert body2["status"] == "error"
    assert body2["passport_gate"]["allowed"] is False
    assert body2["passport_gate"]["reason"] == "icc_passport_mismatch"
    assert body2["printguard_summary"]["release_allowed"] is False
    assert body2["printguard_summary"]["release_verdict"] == "rejected"
    assert len(body2["printguard_summary"]["blockers"]) >= 4


# ---------------------------------------------------------------------------
# 2. 文件预检校验工艺护照指纹：ICC 与护照不一致时拦截放行
# ---------------------------------------------------------------------------


def test_file_preflight_passport_fingerprint_gate_for_image_and_pdfx(
    api_client, process_factory, cmyk_icc, rgb_icc
):
    """图像与 PDF/X-4 预检时核验工艺护照指纹：ICC 一致放行，不一致/缺失/护照失效一律拦截。"""
    proc, passport = _make_valid_passport(api_client, process_factory, "文件护照门禁工艺")

    # 2.1 嵌入一致 CMYK ICC 的图像 → 护照门禁放行
    r_ok = api_client.post(
        "/api/files/preflight",
        json={
            "image_b64": _png_b64(cmyk_icc),
            "filename": "match.png",
            "process_id": proc["id"],
            "passport_hash": passport["passport_hash"],
        },
    )
    assert r_ok.status_code == 200, r_ok.text
    ok_body = r_ok.json()
    assert ok_body["embedded_icc"]["sha256"] == hashlib.sha256(cmyk_icc).hexdigest()
    assert ok_body["passport_gate"]["allowed"] is True
    assert ok_body["passport_gate"]["verdict"] == "verified"

    # 2.2 嵌入不一致 ICC（rgb_icc）的图像 → 拦截放行（icc_passport_mismatch）
    r_mismatch = api_client.post(
        "/api/files/preflight",
        json={
            "image_b64": _png_b64(rgb_icc),
            "filename": "wrong_icc.png",
            "process_id": proc["id"],
            "passport_hash": passport["passport_hash"],
        },
    )
    assert r_mismatch.status_code == 200, r_mismatch.text
    mm_body = r_mismatch.json()
    assert mm_body["status"] == "error"
    assert mm_body["passport_gate"]["allowed"] is False
    assert mm_body["passport_gate"]["verdict"] == "rejected"
    assert mm_body["passport_gate"]["reason"] == "icc_passport_mismatch"

    # 2.3 未嵌入 ICC 的图像 → 拦截放行（file_icc_missing）
    r_no_icc = api_client.post(
        "/api/files/preflight",
        json={
            "image_b64": _png_b64(None),
            "filename": "no_icc.png",
            "process_id": proc["id"],
        },
    )
    assert r_no_icc.status_code == 200
    assert r_no_icc.json()["passport_gate"]["allowed"] is False
    assert r_no_icc.json()["passport_gate"]["reason"] == "file_icc_missing"

    # 2.4 PDF/X-4 封装产物（内嵌目标工艺 OutputIntent ICC）回读预检 → 护照门禁放行
    src_pdf = _vector_cmyk_pdf([b"0.7 0.2 0.1 0 k 60 60 200 200 re f"])
    pkg = api_client.post(
        "/api/files/pdfx",
        params={"process_id": proc["id"], "filename": "box.pdf", "strict": "1"},
        content=src_pdf,
        headers={"Content-Type": "application/pdf"},
    ).json()
    pkg_b64 = pkg["file_b64"].split(",", 1)[1]

    r_pdf_ok = api_client.post(
        "/api/files/preflight",
        json={
            "image_b64": pkg_b64,
            "filename": "box-PDFX4.pdf",
            "process_id": proc["id"],
            "passport_hash": passport["passport_hash"],
        },
    )
    assert r_pdf_ok.status_code == 200, r_pdf_ok.text
    pdf_ok = r_pdf_ok.json()
    assert pdf_ok["pdf"]["output_intent_icc_sha256"] == hashlib.sha256(cmyk_icc).hexdigest()
    assert pdf_ok["pdf"]["output_intent_icc_sha256"].startswith(proc["icc_sha256"])
    assert pdf_ok["passport_gate"]["allowed"] is True
    assert pdf_ok["passport_gate"]["verdict"] == "verified"

    # 2.5 工艺替换 ICC 后原护照变 stale → 再次预检拦截放行
    changed_icc = bytearray(cmyk_icc)
    changed_icc[24:36] = b"\x00" * 12
    ProcessStore().replace_icc(proc["id"], bytes(changed_icc), "new.icc")

    r_stale = api_client.post(
        "/api/files/preflight",
        json={
            "image_b64": pkg_b64,
            "filename": "box-PDFX4.pdf",
            "process_id": proc["id"],
            "passport_hash": passport["passport_hash"],
        },
    )
    assert r_stale.status_code == 200
    stale_gate = r_stale.json()["passport_gate"]
    assert stale_gate["allowed"] is False
    assert stale_gate["reason"] == "passport_stale"
    assert r_stale.json()["status"] == "error"


# ---------------------------------------------------------------------------
# 3. Illustrator 桥接提取的专色清单 → 自动查询工艺色域内/外与 ΔE₀₀
# ---------------------------------------------------------------------------


def test_spot_color_gamut_and_delta_e_check(api_client, process_factory, cmyk_icc):
    """专色色域核验：技术刀线豁免、色域内准确放行、超色域准确报警并给出 ΔE₀₀ 与推荐分色值。"""
    proc = process_factory("专色核验工艺")

    # 用同一工艺 ICC 正查一个真实可印中间色，保证该 Lab 必然在工艺色域内
    in_gamut_lab = forward_query(cmyk_icc, "CMYK", [45.0, 35.0, 20.0, 10.0])["lab"]

    resp = api_client.post(
        "/api/files/spot-check",
        json={
            "process_id": proc["id"],
            "tolerance": 2.0,
            "spots": [
                "CutContour",
                {"name": "BrandInGamut", "lab": in_gamut_lab},
                {"name": "ExtremeNeonGreen", "lab": [78.0, -110.0, 95.0]},
                "PANTONE 485 C",
                "CustomUnlabeledSpot",
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["process_id"] == proc["id"]
    assert body["summary"]["total"] == 5
    by_name = {it["name"]: it for it in body["items"]}

    # 技术刀模专色不误报
    assert by_name["CutContour"]["category"] == "technical"
    assert by_name["CutContour"]["in_gamut"] is None
    assert by_name["CutContour"]["verdict"] == "verified"

    # 色域内实测色：in_gamut=True 且 ΔE₀₀ ≤ 2.0
    assert by_name["BrandInGamut"]["in_gamut"] is True
    assert by_name["BrandInGamut"]["delta_e"] <= 2.0
    assert by_name["BrandInGamut"]["verdict"] == "verified"
    assert by_name["BrandInGamut"]["recommended_recipe"] is not None

    # 超色域极端荧光绿：in_gamut=False 且 ΔE₀₀ > 2.0，verdict=rejected
    assert by_name["ExtremeNeonGreen"]["in_gamut"] is False
    assert by_name["ExtremeNeonGreen"]["delta_e"] > 2.0
    assert by_name["ExtremeNeonGreen"]["risk"] == "out_of_gamut"
    assert by_name["ExtremeNeonGreen"]["verdict"] == "rejected"

    # 仅传名称的常用 Pantone：命中参考近似表并输出真实 ΔE₀₀ 与推荐 CMYK
    assert by_name["PANTONE 485 C"]["source"] == "reference_table"
    assert isinstance(by_name["PANTONE 485 C"]["delta_e"], float)
    assert by_name["PANTONE 485 C"]["recommended_recipe"] is not None

    # 未知自定义专色无色值：诚实标注 unknown（不伪造 ΔE）
    assert by_name["CustomUnlabeledSpot"]["risk"] == "unknown"
    assert by_name["CustomUnlabeledSpot"]["delta_e"] is None
    assert by_name["CustomUnlabeledSpot"]["verdict"] == "warning"

    assert body["verdict"] == "rejected"
