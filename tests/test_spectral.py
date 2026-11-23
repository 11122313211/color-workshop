"""V0.10 光谱/同色异谱：积分一致性、风险边界、API 会话匹配。"""

from __future__ import annotations

import math

import pytest

from app.services.cgats import integrate_lab
from app.services.spectral import (
    DEFAULT_CONDITIONS,
    DISCLAIMER,
    metamerism,
    risk_for_delta_e,
)


#: 数值合成同色异谱对：D50 三刺激几乎相等，D65 有明显差异
SPECTRAL_A = {
    380: 0.2996, 390: 0.4153, 400: 0.4275, 410: 0.2976, 420: 0.3501,
    430: 0.3460, 440: 0.4908, 450: 0.3582, 460: 0.3290, 470: 0.3317,
    480: 0.3230, 490: 0.3336, 500: 0.3112, 510: 0.3763, 520: 0.2769,
    530: 0.4216, 540: 0.3881, 550: 0.5162, 560: 0.4398, 570: 0.4441,
    580: 0.5800, 590: 0.4738, 600: 0.4346, 610: 0.3948, 620: 0.5796,
    630: 0.4013, 640: 0.3972, 650: 0.4024, 660: 0.4348, 670: 0.4975,
    680: 0.4760, 690: 0.5369, 700: 0.4153, 710: 0.4324, 720: 0.5104,
    730: 0.4261,
}
SPECTRAL_B = {
    380: 0.4027, 390: 0.2878, 400: 0.2766, 410: 0.4078, 420: 0.3571,
    430: 0.3634, 440: 0.2216, 450: 0.3581, 460: 0.3923, 470: 0.3960,
    480: 0.4127, 490: 0.4121, 500: 0.4468, 510: 0.3962, 520: 0.5124,
    530: 0.3867, 540: 0.4406, 550: 0.3338, 560: 0.4315, 570: 0.4476,
    580: 0.3306, 590: 0.4537, 600: 0.5074, 610: 0.5594, 620: 0.3846,
    630: 0.5710, 640: 0.5815, 650: 0.5813, 660: 0.5528, 670: 0.4931,
    680: 0.5169, 690: 0.4578, 700: 0.5807, 710: 0.5646, 720: 0.4872,
    730: 0.5722,
}


def test_metamerism_integration_matches_cgats():
    result = metamerism(SPECTRAL_A, SPECTRAL_B, conditions=["D50", "D65"])
    by_condition = {row["condition"]: row for row in result["conditions"]}
    expected_d50 = integrate_lab(SPECTRAL_A, "D50", "2")
    assert by_condition["D50"]["lab_a"] == pytest.approx(expected_d50, abs=1e-3)
    assert set(by_condition) == {"D50", "D65"}
    assert result["disclaimer"] == DISCLAIMER
    assert result["worst_condition"] in by_condition


def test_synthetic_metameric_pair_is_condition_dependent():
    result = metamerism(SPECTRAL_A, SPECTRAL_B)
    by_condition = {row["condition"]: row for row in result["conditions"]}
    assert by_condition["D50"]["delta_e"] <= 0.5
    assert by_condition["D65"]["delta_e"] >= 2.0
    assert result["max_delta_e"] >= by_condition["D65"]["delta_e"]
    assert result["risk"] in ("medium", "high")


def test_risk_boundaries_are_explicit():
    assert risk_for_delta_e(1.0) == "low"
    assert risk_for_delta_e(1.0001) == "medium"
    assert risk_for_delta_e(2.0) == "medium"
    assert risk_for_delta_e(2.0001) == "high"
    assert risk_for_delta_e(None) == "none"


def test_oba_and_grid_warnings_are_reported():
    a = {380: 1.2, 390: 1.2, 400: 1.2, 410: 1.2}
    b = {380: 1.6, 390: 1.6, 400: 1.6, 410: 1.6, 420: 1.6}
    result = metamerism(a, b, conditions=DEFAULT_CONDITIONS)
    joined = " ".join(result["warnings"])
    assert "OBA" in joined
    assert "波长网格不同" in joined
    assert "1.5" in joined


def test_invalid_spectral_is_rejected():
    with pytest.raises(ValueError, match="少于 4"):
        metamerism({380: 0.1, 390: 0.2, 400: 0.3}, SPECTRAL_A)
    with pytest.raises(ValueError, match="不支持"):
        metamerism(SPECTRAL_A, SPECTRAL_B, conditions=["D93"])
    with pytest.raises(ValueError, match="至少选择"):
        metamerism(SPECTRAL_A, SPECTRAL_B, conditions=[])
    with pytest.raises(ValueError, match="380–730"):
        metamerism({360: 0.1, 380: 0.1, 390: 0.2, 400: 0.3}, SPECTRAL_A)
    extreme = {wavelength: 1e308 for wavelength in SPECTRAL_A}
    with pytest.raises(ValueError, match="非有限"):
        metamerism(extreme, SPECTRAL_A)


# ---------------------------------------------------------------------------
# API：两会话按 patch label 匹配
# ---------------------------------------------------------------------------

pytestmark_api = pytest.mark.api


def _cgats(spectra: list[tuple[str, dict[int, float]]]) -> bytes:
    wavelengths = list(range(380, 731, 10))
    fields = "\t".join(["SampleID", "SAMPLE_NAME"] + [f"nm{w}" for w in wavelengths])
    head = (
        'INSTRUMENTATION\t"Eye-One Pro"\n'
        'ILLUMINATION_NAME\t"D50"\n'
        'OBSERVER_ANGLE\t"2"\n'
        f"NUMBER_OF_FIELDS\t{len(wavelengths) + 2}\n"
        "BEGIN_DATA_FORMAT\n" + fields + "\nEND_DATA_FORMAT\n"
        f"NUMBER_OF_SETS\t{len(spectra)}\nBEGIN_DATA\n"
    )
    rows = []
    for label, spectral in spectra:
        values = [spectral.get(w, 0.0) for w in wavelengths]
        rows.append(f"1\t{label}\t" + "\t".join(f"{v:.4f}" for v in values))
    return (head + "\n".join(rows) + "\nEND_DATA\n").encode("utf-8")


def _session(api_client, spectra: list[tuple[str, dict[int, float]]], process_id: str = "") -> str:
    params = {
        "filename": "v10-spectral.txt",
        "instrument": "Spectral Probe",
        "calibrated_at": "2026-01-01T08:00:00",
        "condition": "M1",
        "repeat_mode": "single",
    }
    if process_id:
        params["process_id"] = process_id
    response = api_client.post(
        "/api/qc/measurements/import",
        params=params,
        content=_cgats(spectra),
        headers={"Content-Type": "application/octet-stream"},
    )
    assert response.status_code == 200, response.text
    return response.json()["session"]["id"]


@pytest.mark.api
def test_spectral_api_metamerism_and_session_compare(api_client):
    response = api_client.post(
        "/api/spectral/metamerism",
        json={
            "spectral_a": {str(k): v for k, v in SPECTRAL_A.items()},
            "spectral_b": {str(k): v for k, v in SPECTRAL_B.items()},
            "conditions": ["D50", "D65", "FL11"],
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["max_delta_e"] >= 2.0

    sid_a = _session(api_client, [("L1", SPECTRAL_A), ("L2", SPECTRAL_A)])
    sid_b = _session(api_client, [("L1", SPECTRAL_B), ("L2", SPECTRAL_B)])
    try:
        summary = api_client.get(f"/api/spectral/sessions/{sid_a}/summary")
        assert summary.status_code == 200, summary.text
        assert summary.json()["spectral_patches"] == 2
        assert summary.json()["comparable_sessions"] >= 1

        compare = api_client.post(
            "/api/spectral/compare",
            json={"session_a": sid_a, "session_b": sid_b, "top_n": 2},
        )
        assert compare.status_code == 200, compare.text
        body = compare.json()
        assert body["matched"] == 2
        assert len(body["top"]) == 2
        assert body["top"][0]["label"] in ("L1", "L2")
        assert body["top"][0]["spectral_a"] and body["top"][0]["spectral_b"]
        assert body["max_delta_e"] >= 2.0
    finally:
        api_client.delete("/api/qc/measurements", params={"session_id": sid_a})
        api_client.delete("/api/qc/measurements", params={"session_id": sid_b})


@pytest.mark.api
def test_spectral_api_errors(api_client):
    assert api_client.post(
        "/api/spectral/metamerism",
        json={"spectral_a": {"380": 0.1}, "spectral_b": {"380": 0.2}},
    ).status_code == 422
    assert api_client.post(
        "/api/spectral/compare",
        json={"session_a": "sess_missing_a", "session_b": "sess_missing_b"},
    ).status_code == 404
    assert api_client.post(
        "/api/spectral/metamerism",
        json={
            "spectral_a": {str(k): v for k, v in SPECTRAL_A.items()},
            "spectral_b": {str(k): v for k, v in SPECTRAL_B.items()},
            "conditions": [],
        },
    ).status_code == 422
    assert api_client.post(
        "/api/spectral/metamerism",
        json={
            "spectral_a": {"360": 0.1, "380": 0.1, "390": 0.2, "400": 0.3},
            "spectral_b": {str(k): v for k, v in SPECTRAL_B.items()},
        },
    ).status_code == 422
    assert api_client.post(
        "/api/spectral/metamerism",
        json={
            "spectral_a": {str(k): 1e308 for k in SPECTRAL_A},
            "spectral_b": {str(k): v for k, v in SPECTRAL_B.items()},
        },
    ).status_code == 422
    assert api_client.get("/api/spectral/sessions/sess_missing/summary").status_code == 404
