"""V0.11 工艺护照版本历史与签样引用测试。"""

from __future__ import annotations

import math

import pytest

from app.services.calibration_history import CalibrationStore
from app.services.compensation import CompensationStore
from app.services.filedb import JsonStore
from app.services.measurements import MeasurementStore
from app.services.passport import PassportStore
from app.services.processes import DATA_DIR
from app.services.recipe import RecipeStore
from app.services.signoffs import SignoffStore

pytestmark = pytest.mark.api


def _spectral_text() -> bytes:
    fields = "\t".join(
        ["SampleID", "SAMPLE_NAME"] + [f"nm{w}" for w in range(380, 731, 10)]
    )
    head = (
        'INSTRUMENTATION\t"Eye-One Pro"\n'
        'ILLUMINATION_NAME\t"D50"\n'
        'OBSERVER_ANGLE\t"2"\n'
        "NUMBER_OF_FIELDS\t38\n"
        "BEGIN_DATA_FORMAT\n" + fields + "\nEND_DATA_FORMAT\n"
        "NUMBER_OF_SETS\t2\nBEGIN_DATA\n"
    )

    def row(label: str, base: float) -> str:
        values = [
            round(
                min(1.0, base + 0.35 / (1 + math.exp(-(w - 520) / 40))),
                4,
            )
            for w in range(380, 731, 10)
        ]
        return f"1\t{label}\t" + "\t".join(str(value) for value in values)

    return (head + row("A1", 0.05) + "\n" + row("A2", 0.02) + "\nEND_DATA\n").encode(
        "utf-8"
    )


def _verified_session(api_client, pid: str) -> str:
    spectral = _spectral_text()
    first = api_client.post(
        "/api/qc/measurements/import",
        params={
            "filename": "v11-history.txt",
            "instrument": "History Probe",
            "condition": "M1",
            "calibrated_at": "2026-01-01T08:00:00",
            "repeat_mode": "single",
            "process_id": pid,
        },
        content=spectral,
        headers={"Content-Type": "application/octet-stream"},
    )
    assert first.status_code == 200, first.text
    sid = first.json()["session"]["id"]
    second = api_client.post(
        "/api/qc/measurements/import",
        params={
            "filename": "v11-history-r2.txt",
            "session_id": sid,
            "repeat_mode": "repeat",
            "process_id": pid,
        },
        content=spectral,
        headers={"Content-Type": "application/octet-stream"},
    )
    assert second.status_code == 200, second.text
    assert second.json()["session"]["stats"]["verified"] == 2
    return sid


def _add_calibration(proc: dict, sid: str) -> dict:
    return CalibrationStore().save_round(
        proc["id"],
        proc["icc_sha256"],
        sid,
        {
            "overall": "ok",
            "summary": {"max_de_expected": 1.0, "avg_de_expected": 0.5},
            "ink_suggestions": [{"channel": "C", "delta_pct": 0.1}],
        },
        [0.0, 0.0, 0.0, 0.0],
        [0.01, 0.0, 0.0, 0.0],
    )


def _valid_process(api_client, process_factory) -> tuple[dict, str]:
    proc = process_factory("V0.11 护照历史", measurement_condition="M1")
    sid = _verified_session(api_client, proc["id"])
    _add_calibration(proc, sid)
    created = api_client.post(f"/api/processes/{proc['id']}/passport")
    assert created.status_code == 200, created.text
    assert created.json()["status"] == "valid"
    return proc, sid


def _cleanup(pid: str) -> None:
    RecipeStore().remove_process(pid)
    CompensationStore().reset(pid)
    CalibrationStore().remove_process(pid)
    PassportStore().remove_process(pid)
    for session in MeasurementStore().list_sessions():
        if session.get("process_id") == pid:
            MeasurementStore().delete_session(session["id"])
    signoffs = JsonStore(DATA_DIR / "signoffs.json")
    for sid, doc in list(signoffs.all().items()):
        if doc.get("process_id") == pid:
            signoffs.delete(sid)


def _create_signoff(
    api_client,
    pid: str,
    title: str,
    passport: dict,
    *,
    approve: bool = True,
) -> dict:
    response = api_client.post(
        "/api/signoffs",
        json={
            "process_id": pid,
            "title": title,
            "evidence": {"file_name": f"{title}.pdf"},
            "passport_id": passport["id"],
            "passport_hash": passport["passport_hash"],
        },
    )
    assert response.status_code == 200, response.text
    doc = response.json()
    if approve:
        submit = api_client.post(
            f"/api/signoffs/{doc['id']}/submit", json={"actor": "设计组"}
        )
        assert submit.status_code == 200, submit.text
        approved = api_client.post(
            f"/api/signoffs/{doc['id']}/approve", json={"actor": "客户"}
        )
        assert approved.status_code == 200, approved.text
        doc = approved.json()
    return doc


def test_versions_mark_latest_and_current_signoff_references(
    api_client, process_factory
):
    proc, sid = _valid_process(api_client, process_factory)
    pid = proc["id"]
    try:
        first = api_client.get(f"/api/processes/{pid}/passport").json()
        signoff = _create_signoff(api_client, pid, "v1 签样", first)

        patched = api_client.patch(
            f"/api/processes/{pid}", json={"measurement_condition": "M2"}
        )
        assert patched.status_code == 200, patched.text
        second = api_client.post(f"/api/processes/{pid}/passport")
        assert second.status_code == 200, second.text
        assert second.json()["version"] == 2

        versions = api_client.get(f"/api/processes/{pid}/passport/versions")
        assert versions.status_code == 200, versions.text
        rows = versions.json()
        assert [row["version"] for row in rows] == [2, 1]
        latest, old = rows

        assert latest["is_latest"] is True
        assert old["is_latest"] is False
        assert old["referenced_count"] == 1
        assert len(old["referenced_by_signoffs"]) == 1
        ref = old["referenced_by_signoffs"][0]
        assert ref["id"] == signoff["id"]
        assert ref["state"] == "approved"
        assert ref["title"] == "v1 签样"
        assert latest["referenced_count"] == 0
        assert latest["referenced_by_signoffs"] == []
    finally:
        _cleanup(pid)
        if MeasurementStore().get_session(sid):
            MeasurementStore().delete_session(sid)


def test_versions_include_each_referencing_signoff_state(
    api_client, process_factory
):
    proc, sid = _valid_process(api_client, process_factory)
    pid = proc["id"]
    try:
        passport = api_client.get(f"/api/processes/{pid}/passport").json()
        draft = _create_signoff(
            api_client, pid, "草稿引用", passport, approve=False
        )
        approved = _create_signoff(api_client, pid, "批准引用", passport)
        rows = api_client.get(f"/api/processes/{pid}/passport/versions").json()
        row = next(item for item in rows if item["version"] == passport["version"])
        refs = {item["id"]: item for item in row["referenced_by_signoffs"]}
        assert row["referenced_count"] == 2
        assert refs[draft["id"]]["state"] == "draft"
        assert refs[approved["id"]]["state"] == "approved"
        assert refs[draft["id"]]["title"] == "草稿引用"
    finally:
        _cleanup(pid)
        if MeasurementStore().get_session(sid):
            MeasurementStore().delete_session(sid)


def test_signoff_store_reference_lookup_matches_process_and_hash(
    api_client, process_factory
):
    proc, sid = _valid_process(api_client, process_factory)
    pid = proc["id"]
    try:
        first = api_client.get(f"/api/processes/{pid}/passport").json()
        signoff = _create_signoff(api_client, pid, "引用查找", first)
        store = SignoffStore()
        assert hasattr(store, "references_for_passport")

        rows = store.references_for_passport(
            pid, first["id"], first["passport_hash"]
        )
        assert [row["id"] for row in rows] == [signoff["id"]]
        assert store.references_for_passport("other-process", first["id"], first["passport_hash"]) == []
        assert store.references_for_passport(pid, "pp_missing", "0" * 64) == []
    finally:
        _cleanup(pid)
        if MeasurementStore().get_session(sid):
            MeasurementStore().delete_session(sid)


def test_reference_lookup_accepts_legacy_id_or_hash_only(
    api_client, process_factory
):
    """旧签样可能只记录 passport_id 或 passport_hash，任一命中都算引用。"""
    proc, sid = _valid_process(api_client, process_factory)
    pid = proc["id"]
    store = SignoffStore()
    try:
        passport = api_client.get(f"/api/processes/{pid}/passport").json()
        only_hash = store.create(
            proc,
            title="仅 hash 旧签样",
            evidence={
                "file_name": "legacy-hash.pdf",
                "passport_hash": passport["passport_hash"],
            },
        )
        only_id = store.create(
            proc,
            title="仅 id 旧签样",
            evidence={
                "file_name": "legacy-id.pdf",
                "passport_id": passport["id"],
            },
        )
        by_hash = store.references_for_passport(
            pid, "", passport["passport_hash"]
        )
        assert [row["id"] for row in by_hash] == [only_hash["id"]]
        by_id = store.references_for_passport(
            pid, passport["id"], ""
        )
        assert [row["id"] for row in by_id] == [only_id["id"]]
        both = store.references_for_passport(
            pid, passport["id"], passport["passport_hash"]
        )
        assert {row["id"] for row in both} == {only_hash["id"], only_id["id"]}
    finally:
        for row in store.list(process_id=pid):
            store._store.delete(row["id"])
        _cleanup(pid)
        if MeasurementStore().get_session(sid):
            MeasurementStore().delete_session(sid)


def test_archived_process_passport_history_stays_readable(
    api_client, process_factory
):
    proc, sid = _valid_process(api_client, process_factory)
    pid = proc["id"]
    try:
        passport = api_client.get(f"/api/processes/{pid}/passport").json()
        archived = api_client.post(
            f"/api/processes/{pid}/archive",
            json={"reason": "历史只读", "actor": "pytest"},
        )
        assert archived.status_code == 200, archived.text

        versions = api_client.get(f"/api/processes/{pid}/passport/versions")
        assert versions.status_code == 200, versions.text
        assert versions.json()[0]["version"] == passport["version"]
        old = api_client.get(f"/api/processes/{pid}/passport/{passport['version']}")
        assert old.status_code == 200, old.text
        assert old.json()["passport_hash"] == passport["passport_hash"]
        assert old.json()["id"] == passport["id"]

        blocked = api_client.post(f"/api/processes/{pid}/passport")
        assert blocked.status_code == 409, blocked.text
        detail = blocked.json().get("detail")
        assert isinstance(detail, dict)
        assert detail.get("code") == "PROCESS_ARCHIVED"
    finally:
        _cleanup(pid)
        if MeasurementStore().get_session(sid):
            MeasurementStore().delete_session(sid)


def test_old_passport_version_is_immutable_after_new_version(
    api_client, process_factory
):
    proc, sid = _valid_process(api_client, process_factory)
    pid = proc["id"]
    try:
        first = api_client.get(f"/api/processes/{pid}/passport").json()
        before = api_client.get(f"/api/processes/{pid}/passport/{first['version']}")
        assert before.status_code == 200, before.text

        patched = api_client.patch(
            f"/api/processes/{pid}", json={"measurement_condition": "M2"}
        )
        assert patched.status_code == 200, patched.text
        second = api_client.post(f"/api/processes/{pid}/passport").json()
        assert second["version"] == first["version"] + 1

        after = api_client.get(f"/api/processes/{pid}/passport/{first['version']}")
        assert after.status_code == 200, after.text
        assert after.json()["id"] == before.json()["id"]
        assert after.json()["passport_hash"] == before.json()["passport_hash"]
        assert after.json()["generated_at"] == before.json()["generated_at"]
        assert after.json()["process"]["measurement_condition"] == "M1"
    finally:
        _cleanup(pid)
        if MeasurementStore().get_session(sid):
            MeasurementStore().delete_session(sid)
