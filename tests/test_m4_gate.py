"""M4 测试：ΔE 分量分解手算 + 补偿模型 verified 门禁。"""

from __future__ import annotations

import pytest

from app.algorithms.core import delta_e_components


# ---------------------------------------------------------------------------
# 分量手算
# ---------------------------------------------------------------------------


def test_components_handcheck():
    c = delta_e_components([50, 20, 5], [52, 18, 7])
    assert c["dL"] == pytest.approx(2.0)
    assert c["da"] == pytest.approx(-2.0)
    assert c["db"] == pytest.approx(2.0)
    # C1=√(20²+5²)=20.6155，C2=√(18²+7²)=19.3132
    assert c["dC"] == pytest.approx(19.3132 - 20.6155, abs=1e-3)
    # h1=atan2(5,20)=14.036°，h2=atan2(7,18)=21.251°
    assert c["dh"] == pytest.approx(7.214, abs=1e-2)
    assert c["dE76"] == pytest.approx(3.4641, abs=1e-3)


def test_components_hue_wrap():
    """色相角跨 ±180° 环绕：350°→10° 应给出 +20° 而非 -340°。"""
    c = delta_e_components([50, -9.85, -1.71], [50, 9.85, 1.71])  # h≈-170° → h≈+9.9°
    assert -180 < c["dh"] <= 180
    assert abs(c["dh"]) < 185
    c2 = delta_e_components([50, 9.85, -1.71], [50, -9.85, -1.71])
    assert -180 < c2["dh"] <= 180


def test_components_zero():
    c = delta_e_components([50, 10, 20], [50, 10, 20])
    assert c["dE76"] == 0 and c["dC"] == 0 and c["dh"] == 0


# ---------------------------------------------------------------------------
# 补偿门禁（API 层，关联测量会话）
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.api


def _predict_one(client, pid, lab=(50.0, 20.0, 5.0), label="门禁目标"):
    r = client.post(
        "/api/recipes/predict",
        json={"process_id": pid, "targets": [{"label": label, "lab": list(lab)}], "tolerance": 2.0},
    )
    assert r.status_code == 200, r.text
    records = client.get("/api/recipes", params={"process_id": pid}).json()
    doc = next(x for x in records if x.get("label") == label)
    return doc


def _verified_session(client):
    """构造一个含 2 条同标签 verified 记录的会话（复测一致）。"""
    spectral = _spectral_text()
    r = client.post(
        "/api/qc/measurements/import",
        params={"filename": "v1.txt", "instrument": "I", "calibrated_at": "t", "repeat_mode": "single"},
        content=spectral,
        headers={"Content-Type": "application/octet-stream"},
    )
    sid = r.json()["session"]["id"]
    client.post(
        "/api/qc/measurements/import",
        params={"filename": "v2.txt", "session_id": sid, "repeat_mode": "repeat"},
        content=spectral,
        headers={"Content-Type": "application/octet-stream"},
    )
    return sid


def _spectral_text() -> bytes:
    fields = "\t".join(["SampleID", "SAMPLE_NAME"] + [f"nm{w}" for w in range(380, 731, 10)])
    head = (
        'INSTRUMENTATION\t"Eye-One Pro"\n'
        'ILLUMINATION_NAME\t"D50"\n'
        'OBSERVER_ANGLE\t"2"\n'
        "NUMBER_OF_FIELDS\t38\n"
        "BEGIN_DATA_FORMAT\n" + fields + "\nEND_DATA_FORMAT\n"
        "NUMBER_OF_SETS\t2\nBEGIN_DATA\n"
    )
    # 平滑反射率两块（近纸白 + 中间调），两文件完全一致 → 复测 ΔE=0
    import math

    def row(label, base):
        vals = [round(min(1.0, base + 0.35 / (1 + math.exp(-(w - 520) / 40))), 4) for w in range(380, 731, 10)]
        return f"1\t{label}\t" + "\t".join(str(v) for v in vals)

    return (head + row("A1", 0.05) + "\n" + row("A2", 0.02) + "\nEND_DATA\n").encode("utf-8")


def _compensation_state(client, pid):
    r = client.get(f"/api/processes/{pid}")
    comp = r.json().get("compensation")
    if not comp:
        return None
    return comp


def test_compensation_gate_blocks_unverified_session(api_client, cmyk_icc):
    """关联会话无 verified → 修正照常、补偿不沉淀（n 不变 + skipped_reason）。"""
    from app.services.processes import ProcessStore
    from app.services.measurements import MeasurementStore

    proc = ProcessStore().create("门禁工艺", cmyk_icc)
    try:
        doc = _predict_one(api_client, proc["id"])
        pred = doc["predicted_lab"]
        # 单次测量会话（无复测声明 → 全 warning，无 verified）
        r = api_client.post(
            "/api/qc/measurements/import",
            params={"filename": "s.txt", "instrument": "I", "calibrated_at": "t", "repeat_mode": "single"},
            content=_spectral_text(),
            headers={"Content-Type": "application/octet-stream"},
        )
        sid = r.json()["session"]["id"]
        assert r.json()["session"]["stats"]["verified"] == 0

        v = api_client.post(
            "/api/recipes/verify",
            json={"recipe_id": doc["id"], "measured_lab": [pred[0] + 3.0, pred[1] - 2.0, pred[2] + 1.5],
                  "measurement_session_id": sid},
        )
        assert v.status_code == 200, v.text
        body = v.json()
        assert body["improved"] is True  # 修正照常工作
        assert body["compensation_status"]["applied"] is False
        assert "verified" in body["compensation_status"]["skipped_reason"]
        assert _compensation_state(api_client, proc["id"]) is None  # 补偿模型未沉淀
        assert body["components"]["dL"] == pytest.approx(3.0, abs=0.2)
    finally:
        ProcessStore().delete(proc["id"])


def test_compensation_gate_allows_verified_session(api_client, cmyk_icc):
    """关联会话含 verified → 补偿照常沉淀；不关联时行为与 V0.4 兼容。"""
    from app.services.processes import ProcessStore

    proc = ProcessStore().create("门禁工艺B", cmyk_icc)
    try:
        doc = _predict_one(api_client, proc["id"], label="门禁B")
        pred = doc["predicted_lab"]
        sid = _verified_session(api_client)

        v = api_client.post(
            "/api/recipes/verify",
            json={"recipe_id": doc["id"], "measured_lab": [pred[0] + 3.0, pred[1] - 2.0, pred[2] + 1.5],
                  "measurement_session_id": sid},
        )
        body = v.json()
        assert body["improved"] is True
        assert body["compensation_status"]["applied"] is True
        assert body["compensation_status"]["n"] >= 1
        comp = _compensation_state(api_client, proc["id"])
        assert comp is not None and comp["n"] == 1

        # 清掉补偿后验证「不关联」路径（V0.4 兼容：无条件沉淀）
        api_client.delete(f"/api/processes/{proc['id']}/compensation")
        v2 = api_client.post(
            "/api/recipes/verify",
            json={"recipe_id": doc["id"], "measured_lab": [pred[0] + 3.0, pred[1] - 2.0, pred[2] + 1.5]},
        )
        b2 = v2.json()
        assert b2["compensation_status"]["applied"] is True
        assert b2["compensation_status"]["n"] == 1
    finally:
        ProcessStore().delete(proc["id"])


def test_compensation_gate_unknown_session_404(api_client, cmyk_icc):
    from app.services.processes import ProcessStore

    proc = ProcessStore().create("门禁工艺C", cmyk_icc)
    try:
        doc = _predict_one(api_client, proc["id"], label="门禁C")
        r = api_client.post(
            "/api/recipes/verify",
            json={"recipe_id": doc["id"], "measured_lab": doc["predicted_lab"],
                  "measurement_session_id": "sess_missing"},
        )
        assert r.status_code == 404
    finally:
        ProcessStore().delete(proc["id"])
