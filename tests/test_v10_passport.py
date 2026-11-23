"""V0.10 工艺护照、配方语义、签样护照门禁与迁移测试。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import math
import uuid
from pathlib import Path

import pytest

from app.services.calibration_history import CalibrationStore
from app.services.compensation import CompensationStore
from app.services.filedb import JsonStore
from app.services.processes import DATA_DIR, PROFILE_DIR, ProcessStore


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

    def row(label: str, base: float) -> str:
        vals = [
            round(min(1.0, base + 0.35 / (1 + math.exp(-(w - 520) / 40))), 4)
            for w in range(380, 731, 10)
        ]
        return f"1\t{label}\t" + "\t".join(str(v) for v in vals)

    return (head + row("A1", 0.05) + "\n" + row("A2", 0.02) + "\nEND_DATA\n").encode("utf-8")


def _verified_session(api_client, process_id: str) -> str:
    spectral = _spectral_text()
    response = api_client.post(
        "/api/qc/measurements/import",
        params={
            "filename": "v10-passport.txt",
            "instrument": "Passport Probe",
            "condition": "M1",
            "calibrated_at": "2026-01-01T08:00:00",
            "repeat_mode": "single",
            "process_id": process_id,
        },
        content=spectral,
        headers={"Content-Type": "application/octet-stream"},
    )
    assert response.status_code == 200, response.text
    sid = response.json()["session"]["id"]
    response = api_client.post(
        "/api/qc/measurements/import",
        params={
            "filename": "v10-passport-r2.txt",
            "session_id": sid,
            "repeat_mode": "repeat",
            "process_id": process_id,
        },
        content=spectral,
        headers={"Content-Type": "application/octet-stream"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["session"]["stats"]["verified"] == 2
    return sid


def _add_calibration_round(process_doc: dict, sid: str) -> dict:
    report = {
        "overall": "ok",
        "summary": {"max_de_expected": 1.2, "avg_de_expected": 0.6},
        "ink_suggestions": [
            {"channel": "C", "delta_pct": 0.2},
            {"channel": "M", "delta_pct": -0.1},
        ],
    }
    return CalibrationStore().save_round(
        process_doc["id"],
        process_doc["icc_sha256"],
        sid,
        report,
        [0.0, 0.0, 0.0, 0.0],
        [0.01, -0.005, 0.0, 0.0],
    )


@pytest.mark.api
def test_passport_idempotent_versions_and_exports(api_client, process_factory):
    proc = process_factory("护照幂等工艺")
    pid = proc["id"]

    first = api_client.post(f"/api/processes/{pid}/passport")
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["created"] is True
    assert body["version"] == 1
    assert body["status"] == "incomplete"
    assert len(body["passport_hash"]) == 64
    assert any(w["code"] == "no_verified_measurement" for w in body["warnings"])

    second = api_client.post(f"/api/processes/{pid}/passport")
    assert second.status_code == 200, second.text
    assert second.json()["created"] is False
    assert second.json()["version"] == 1
    assert second.json()["passport_hash"] == body["passport_hash"]

    patched = api_client.patch(
        f"/api/processes/{pid}",
        json={"measurement_condition": "M1"},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["passport_summary"]["version"] == 1

    third = api_client.post(f"/api/processes/{pid}/passport")
    assert third.status_code == 200, third.text
    assert third.json()["created"] is True
    assert third.json()["version"] == 2
    assert third.json()["passport_hash"] != body["passport_hash"]

    versions = api_client.get(f"/api/processes/{pid}/passport/versions")
    assert versions.status_code == 200, versions.text
    assert [row["version"] for row in versions.json()] == [2, 1]

    detail = api_client.get(f"/api/processes/{pid}")
    assert detail.json()["passport_summary"]["version"] == 2
    assert api_client.get(f"/api/processes/{pid}/passport/99").status_code == 404

    md = api_client.get(f"/api/processes/{pid}/passport/2/export?format=md")
    assert md.status_code == 200
    assert "工艺护照" in md.text and "同色异谱" in md.text
    payload = api_client.get(f"/api/processes/{pid}/passport/2/export?format=json")
    assert payload.status_code == 200
    assert json.loads(payload.text)["passport_hash"] == third.json()["passport_hash"]
    assert api_client.get(
        f"/api/processes/{pid}/passport/2/export?format=pdf"
    ).status_code == 422


@pytest.mark.api
def test_passport_valid_stale_then_invalid(api_client, process_factory, cmyk_icc):
    proc = process_factory("护照状态工艺", measurement_condition="M1")
    pid = proc["id"]
    sid = _verified_session(api_client, pid)
    _add_calibration_round(proc, sid)
    try:
        valid = api_client.post(f"/api/processes/{pid}/passport").json()
        assert valid["status"] == "valid", valid
        assert valid["evidence"]["measurements"]["verified_records"] == 2
        assert valid["evidence"]["calibration"]["rounds"] == 1

        CompensationStore().accumulate(pid, proc["icc_sha256"], [0.01, -0.01, 0.0, 0.0])
        stale = api_client.get(f"/api/processes/{pid}/passport").json()
        assert stale["status"] == "stale"
        assert any(r["code"] == "compensation_changed" for r in stale["status_reasons"])

        refreshed = api_client.post(f"/api/processes/{pid}/passport").json()
        assert refreshed["status"] == "valid"
        assert refreshed["version"] == 2

        changed = bytearray(cmyk_icc)
        changed[24:36] = b"\x00" * 12
        ProcessStore().replace_icc(pid, bytes(changed), "changed.icc")
        changed_status = api_client.get(f"/api/processes/{pid}/passport").json()
        assert changed_status["status"] == "stale"
        assert any(r["code"] == "icc_changed" for r in changed_status["status_reasons"])

        third = api_client.post(f"/api/processes/{pid}/passport").json()
        assert third["version"] == 3
        assert third["status"] == "incomplete"

        current = ProcessStore().get(pid)
        (Path(PROFILE_DIR) / current["icc_file"]).unlink()
        invalid = api_client.get(f"/api/processes/{pid}/passport").json()
        assert invalid["status"] == "invalid"
        assert any(r["code"] == "icc_unreadable" for r in invalid["status_reasons"])
    finally:
        api_client.delete("/api/qc/measurements", params={"session_id": sid})
        ProcessStore().delete(pid)


@pytest.mark.api
def test_signoff_passport_gate_and_legacy_path(api_client, process_factory):
    proc = process_factory("护照签样工艺", measurement_condition="M1")
    pid = proc["id"]
    sid = _verified_session(api_client, pid)
    _add_calibration_round(proc, sid)
    try:
        passport = api_client.post(f"/api/processes/{pid}/passport").json()
        created = api_client.post("/api/signoffs", json={
            "process_id": pid,
            "title": "护照签样",
            "created_by": "客户",
            "evidence": {"file_name": "proof-v10.pdf"},
            "passport_id": passport["id"],
            "passport_hash": passport["passport_hash"],
        })
        assert created.status_code == 200, created.text
        signoff = created.json()
        assert signoff["evidence"]["passport_id"] == passport["id"]
        assert signoff["snapshot"]["passport_hash"] == passport["passport_hash"]

        assert api_client.post(
            f"/api/signoffs/{signoff['id']}/submit", json={"actor": "设计"}
        ).status_code == 200
        assert api_client.post(
            f"/api/signoffs/{signoff['id']}/approve", json={"actor": "客户"}
        ).status_code == 200
        gate = api_client.get(f"/api/signoffs/gate?process_id={pid}").json()
        assert gate["allowed"] is True
        assert gate["passport_hash"] == passport["passport_hash"]

        manifest = api_client.get(f"/api/signoffs/release-manifest?process_id={pid}")
        assert manifest.status_code == 200
        assert manifest.json()["passport"]["version"] == 1

        CompensationStore().accumulate(pid, proc["icc_sha256"], [0.02, 0.0, 0.0, 0.0])
        stale_gate = api_client.get(f"/api/signoffs/gate?process_id={pid}").json()
        assert stale_gate["allowed"] is False
        assert stale_gate["reason"] == "passport_stale"
        assert stale_gate["passport_status"] == "stale"
        assert api_client.get(
            f"/api/signoffs/release-manifest?process_id={pid}"
        ).status_code == 409

        new_passport = api_client.post(f"/api/processes/{pid}/passport").json()
        assert new_passport["passport_hash"] != passport["passport_hash"]
        changed_gate = api_client.get(f"/api/signoffs/gate?process_id={pid}").json()
        assert changed_gate["allowed"] is False
        assert changed_gate["reason"] == "passport_changed"
        assert api_client.get(
            f"/api/signoffs/release-manifest?process_id={pid}"
        ).status_code == 409
    finally:
        api_client.delete("/api/qc/measurements", params={"session_id": sid})
        ProcessStore().delete(pid)

    legacy_proc = process_factory("旧签样兼容工艺")
    try:
        legacy = api_client.post("/api/signoffs", json={
            "process_id": legacy_proc["id"],
            "evidence": {"file_name": "legacy.pdf"},
        })
        assert legacy.status_code == 200, legacy.text
        sid_l = legacy.json()["id"]
        assert api_client.post(
            f"/api/signoffs/{sid_l}/submit", json={"actor": "设计"}
        ).status_code == 200
        assert api_client.post(
            f"/api/signoffs/{sid_l}/approve", json={"actor": "客户"}
        ).status_code == 200
        legacy_gate = api_client.get(
            f"/api/signoffs/gate?process_id={legacy_proc['id']}"
        ).json()
        assert legacy_gate["allowed"] is True
        assert legacy_gate["passport_hash"] is None
    finally:
        ProcessStore().delete(legacy_proc["id"])


@pytest.mark.api
def test_signoff_gate_blocks_invalid_passport(api_client, process_factory):
    proc = process_factory("无效护照门禁工艺", measurement_condition="M1")
    pid = proc["id"]
    sid = _verified_session(api_client, pid)
    _add_calibration_round(proc, sid)
    try:
        passport = api_client.post(f"/api/processes/{pid}/passport").json()
        assert passport["status"] == "valid"
        signoff = api_client.post("/api/signoffs", json={
            "process_id": pid,
            "evidence": {"file_name": "invalid-proof.pdf"},
            "passport_id": passport["id"],
            "passport_hash": passport["passport_hash"],
        }).json()
        api_client.post(f"/api/signoffs/{signoff['id']}/submit", json={"actor": "设计"})
        api_client.post(f"/api/signoffs/{signoff['id']}/approve", json={"actor": "客户"})

        current = ProcessStore().get(pid)
        (Path(PROFILE_DIR) / current["icc_file"]).unlink()
        gate = api_client.get(f"/api/signoffs/gate?process_id={pid}").json()
        assert gate["allowed"] is False
        assert gate["reason"] == "passport_invalid"
        assert gate["passport_status"] == "invalid"
        assert api_client.get(
            f"/api/signoffs/release-manifest?process_id={pid}"
        ).status_code == 409
    finally:
        api_client.delete("/api/qc/measurements", params={"session_id": sid})
        ProcessStore().delete(pid)


@pytest.mark.api
def test_signoff_rejects_non_valid_passport(api_client, process_factory):
    proc = process_factory("非有效护照签样工艺")
    try:
        passport = api_client.post(f"/api/processes/{proc['id']}/passport").json()
        assert passport["status"] == "incomplete"
        rejected = api_client.post("/api/signoffs", json={
            "process_id": proc["id"],
            "title": "不完整护照签样",
            "evidence": {"file_name": "incomplete-proof.pdf"},
            "passport_id": passport["id"],
            "passport_hash": passport["passport_hash"],
        })
        assert rejected.status_code == 422
        assert "valid" in rejected.json()["detail"]
    finally:
        ProcessStore().delete(proc["id"])


@pytest.mark.api
def test_recipe_semantics_and_export(api_client, process_factory):
    proc = process_factory("配方语义工艺")
    try:
        predicted = api_client.post("/api/recipes/predict", json={
            "process_id": proc["id"],
            "targets": [{"label": "语义红", "lab": [50.0, 20.0, 5.0]}],
            "tolerance": 2.0,
        })
        assert predicted.status_code == 200, predicted.text
        result = predicted.json()["results"][0]
        assert result["recipe_kind"] == "device_values"
        assert result["recipe_meta"]["unit"] == "percent"
        assert "分色值" in result["recipe_meta"]["note"]
        rid = result["recipe_id"]
        assert rid

        exported = api_client.get(f"/api/recipes/{rid}/export?format=md")
        assert exported.status_code == 200
        assert "分色值（设备值）" in exported.text
        assert "不是基础油墨调墨配方" in exported.text
        as_json = api_client.get(f"/api/recipes/{rid}/export?format=json")
        assert as_json.status_code == 200
        assert json.loads(as_json.text)["recipe_kind"] == "device_values"
        assert api_client.get(f"/api/recipes/{rid}/export?format=pdf").status_code == 422

        legacy_id = f"legacy_{uuid.uuid4().hex[:8]}"
        legacy_doc = {
            "id": legacy_id,
            "label": "旧配方",
            "process_id": proc["id"],
            "process_name": proc["name"],
            "device_space": "CMYK",
            "target_lab": [50, 20, 5],
            "recipe": {"C": 1, "M": 2, "Y": 3, "K": 4},
            "predicted_lab": [50, 20, 5],
            "delta_e": 0.1,
            "method": "cie2000",
            "tolerance": 2.0,
            "converged": True,
            "iterations": [0.1],
            "predictions": 1,
            "lut_points": 0,
            "verifications": [],
            "created_at": "2025-01-01T00:00:00",
        }
        JsonStore(DATA_DIR / "recipes.json").put(legacy_id, legacy_doc)
        old = api_client.get(f"/api/recipes/{legacy_id}")
        assert old.status_code == 200
        assert old.json()["recipe_kind"] == "device_values"
        assert "recipe_meta" in old.json()
        api_client.delete(f"/api/recipes/{legacy_id}")
    finally:
        ProcessStore().delete(proc["id"])


@pytest.mark.api
def test_process_measurement_condition_validation(api_client, process_factory):
    proc = process_factory("测量条件工艺", measurement_condition="M2")
    pid = proc["id"]
    assert proc["measurement_condition"] == "M2"
    assert api_client.get(f"/api/processes/{pid}").json()["measurement_condition"] == "M2"

    updated = api_client.patch(
        f"/api/processes/{pid}",
        json={"measurement_condition": "M0"},
    )
    assert updated.status_code == 200
    assert updated.json()["measurement_condition"] == "M0"
    assert api_client.patch(
        f"/api/processes/{pid}",
        json={"measurement_condition": "M9"},
    ).status_code == 422

    old = process_factory("未登记条件工艺")
    assert old["measurement_condition"] == ""


def test_passport_hash_excludes_generated_metadata(process_factory):
    from app.services.passport import _hash_payload, _snapshot_payload

    assert _hash_payload({"n": 1}) == _hash_payload({"n": 1.0})
    proc = process_factory("哈希稳定工艺", measurement_condition="M1")
    try:
        snapshot = _snapshot_payload(proc)
        original = snapshot["passport_hash"]
        snapshot["generated_at"] = "2099-01-01T00:00:00"
        snapshot["version"] = 999
        snapshot["passport_hash"] = "ignored"
        assert _hash_payload(snapshot) == original
        snapshot["warnings"].append({"code": "x", "level": "info", "message": "changed"})
        assert _hash_payload(snapshot) != original
    finally:
        ProcessStore().delete(proc["id"])


def test_passport_versions_capped_at_50(monkeypatch, process_factory):
    from app.services import passport as passport_service

    proc = process_factory("护照上限工艺")
    seq = {"value": 0}

    def fake_payload(process_doc):
        seq["value"] += 1
        payload = {
            "process": {
                "id": process_doc["id"],
                "name": process_doc["name"],
                "icc_sha256": process_doc["icc_sha256"],
                "icc_meta": process_doc["icc_meta"],
            },
            "evidence": {
                "compensation": {"fingerprint": ""},
                "calibration": {"fingerprint": passport_service.calibration_fingerprint([])},
                "measurements": {"verified_records": 0},
                "spectral": {"available": False},
            },
            "warnings": [],
            "sequence": seq["value"],
            "generated_at": "2026-01-01T00:00:00",
        }
        payload["passport_hash"] = passport_service._hash_payload(payload)
        return payload

    monkeypatch.setattr(passport_service, "_snapshot_payload", fake_payload)
    store = passport_service.PassportStore(Path(DATA_DIR) / f"passports-{uuid.uuid4().hex[:8]}.json")
    try:
        for _ in range(55):
            store.create(proc)
        versions = store.list_versions(proc["id"])
        assert len(versions) == 50
        assert versions[0]["version"] == 55
        assert versions[-1]["version"] == 6
    finally:
        ProcessStore().delete(proc["id"])


def test_passport_concurrent_create_serializes_versions(monkeypatch, process_factory):
    from app.services import passport as passport_service

    proc = process_factory("护照并发工艺")
    seq = {"value": 0}

    def fake_payload(process_doc):
        seq["value"] += 1
        payload = {
            "process": {
                "id": process_doc["id"],
                "name": process_doc["name"],
                "icc_sha256": process_doc["icc_sha256"],
                "icc_meta": process_doc["icc_meta"],
            },
            "evidence": {
                "compensation": {"fingerprint": ""},
                "calibration": {"fingerprint": passport_service.calibration_fingerprint([])},
                "measurements": {"verified_records": 0},
                "spectral": {"available": False},
            },
            "warnings": [],
            "sequence": seq["value"],
            "generated_at": "2026-01-01T00:00:00",
        }
        payload["passport_hash"] = passport_service._hash_payload(payload)
        return payload

    monkeypatch.setattr(passport_service, "_snapshot_payload", fake_payload)
    path = Path(DATA_DIR) / f"passports-thread-{uuid.uuid4().hex[:8]}.json"
    store = passport_service.PassportStore(path)
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: store.create(proc), range(8)))
        assert all(created for _, created in results)
        versions = store.list_versions(proc["id"])
        assert [row["version"] for row in reversed(versions)] == list(range(1, 9))
        assert len({row["passport_hash"] for row in versions}) == 8
    finally:
        ProcessStore().delete(proc["id"])
        path.unlink(missing_ok=True)


def test_passport_store_ignores_malformed_versions(process_factory):
    from app.services.passport import PassportStore

    proc = process_factory("护照损坏数据工艺")
    path = Path(DATA_DIR) / f"passports-corrupt-{uuid.uuid4().hex[:8]}.json"
    JsonStore(path).put(proc["id"], {
        "process_id": proc["id"],
        "versions": [
            None,
            {"version": "bad", "passport_hash": "x"},
            {"version": 2, "passport_hash": "good"},
            {"version": 0, "passport_hash": "zero"},
        ],
    })
    try:
        store = PassportStore(path)
        assert store.latest_snapshot(proc["id"])["version"] == 2
        assert [row["version"] for row in store.list_versions(proc["id"])] == [2]
    finally:
        ProcessStore().delete(proc["id"])
        path.unlink(missing_ok=True)


def test_migration_is_idempotent_and_non_destructive():
    from scripts.migrate_v10 import migrate

    root = Path(DATA_DIR) / f"migration-{uuid.uuid4().hex[:8]}"
    root.mkdir(parents=True)
    JsonStore(root / "processes.json").put("p1", {"id": "p1", "name": "旧工艺"})
    JsonStore(root / "recipes.json").put("r1", {"id": "r1", "label": "旧配方"})
    JsonStore(root / "signoffs.json").put(
        "s1",
        {"id": "s1", "snapshot": {"icc_sha256": "abc"}, "state": "approved"},
    )

    first = migrate(root)
    assert first["processes_updated"] == 1
    assert first["recipes_updated"] == 1
    assert first["signoffs_updated"] == 1
    assert first["passports_initialized"] is True

    process = JsonStore(root / "processes.json").get("p1")
    recipe = JsonStore(root / "recipes.json").get("r1")
    signoff = JsonStore(root / "signoffs.json").get("s1")
    assert process["measurement_condition"] == ""
    assert recipe["recipe_kind"] == "device_values"
    assert recipe["recipe_meta"]["unit"] == "percent"
    assert signoff["snapshot"]["passport_hash"] == ""
    assert (root / "passports.json").read_text(encoding="utf-8").strip() == "{}"

    second = migrate(root)
    assert second["processes_updated"] == 0
    assert second["recipes_updated"] == 0
    assert second["signoffs_updated"] == 0
    assert second["passports_initialized"] is False
    assert JsonStore(root / "processes.json").get("p1")["name"] == "旧工艺"
