"""V0.11 归档生命周期、只读门禁与永久清理测试。"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from app.services.calibration_history import CalibrationStore
from app.services.compensation import CompensationStore
from app.services.evaluations import EvaluationStore
from app.services.filedb import JsonStore
from app.services.measurements import MeasurementStore
from app.services.passport import PassportStore
from app.services.processes import DATA_DIR, PROFILE_DIR, ProcessStore
from app.services.recipe import RecipeStore
from app.services.signoffs import SignoffStore

pytestmark = pytest.mark.api


def _body(response, status: int = 200) -> dict:
    assert response.status_code == status, (response.status_code, response.text)
    data = response.json()
    assert isinstance(data, dict), data
    return data


def _process_body(response, status: int = 200) -> dict:
    data = _body(response, status)
    nested = data.get("process")
    return nested if isinstance(nested, dict) else data


def _archived_error(response) -> dict:
    assert response.status_code == 409, (response.status_code, response.text)
    detail = response.json().get("detail")
    assert isinstance(detail, dict), detail
    assert detail.get("code") == "PROCESS_ARCHIVED", detail
    return detail


def _archive(client, pid: str, reason: str = "测试归档", actor: str = "pytest") -> dict:
    return _process_body(
        client.post(
            f"/api/processes/{pid}/archive",
            json={"reason": reason, "actor": actor},
        )
    )


def _recipe_doc(proc: dict) -> dict:
    recipe = {"C": 10.0, "M": 20.0, "Y": 30.0, "K": 0.0}
    return RecipeStore().create(
        proc,
        {
            "target_lab": [50.0, 20.0, 5.0],
            "recipe": recipe,
            "predicted_lab": [50.0, 20.0, 5.0],
            "delta_e": 0.5,
            "iterations": [],
            "predictions": 1,
            "lut_points": 17 ** 4,
            "converged": True,
            "method": "cie2000",
            "tolerance": 2.0,
        },
        "归档配方",
    )


def _seed_dependencies(proc: dict) -> dict:
    pid = proc["id"]
    recipe = _recipe_doc(proc)
    comp = CompensationStore().accumulate(
        pid, proc["icc_sha256"], [0.01, -0.01, 0.0, 0.0]
    )
    calibration = CalibrationStore().save_round(
        pid,
        proc["icc_sha256"],
        f"sess_cal_{pid}",
        {
            "overall": "ok",
            "summary": {"max_de_expected": 1.0, "avg_de_expected": 0.5},
            "ink_suggestions": [{"channel": "C", "delta_pct": 0.1}],
        },
        [0.0, 0.0, 0.0, 0.0],
        [0.01, -0.01, 0.0, 0.0],
    )
    passport, _ = PassportStore().create(proc)
    session = MeasurementStore().create_session(
        "V0.11 归档测试仪", calibrated_at="2026-01-01T08:00:00", process_id=pid
    )
    evaluation = EvaluationStore().create_evaluation("book", "V0.11 归档评价", pid)
    signoff = SignoffStore().create(proc, evidence={"file_name": "archive-proof.pdf"})
    return {
        "recipe": recipe,
        "compensation": comp,
        "calibration": calibration,
        "passport": passport,
        "session": session,
        "evaluation": evaluation,
        "signoff": signoff,
    }


def _cleanup_side_records(pid: str) -> None:
    RecipeStore().remove_process(pid)
    CompensationStore().reset(pid)
    CalibrationStore().remove_process(pid)
    PassportStore().remove_process(pid)
    for session in MeasurementStore().list_sessions():
        if session.get("process_id") == pid:
            MeasurementStore().delete_session(session["id"])
    evaluations = JsonStore(DATA_DIR / "evaluations.json")
    for eid, doc in list(evaluations.all().items()):
        if doc.get("process_id") == pid:
            evaluations.delete(eid)
    signoffs = JsonStore(DATA_DIR / "signoffs.json")
    for sid, doc in list(signoffs.all().items()):
        if doc.get("process_id") == pid:
            signoffs.delete(sid)


def _dependency_count(payload: dict, key: str) -> int:
    node = payload.get(key)
    if node is None:
        counts = payload.get("counts") or {}
        node = counts.get(key)
    if isinstance(node, int):
        return node
    if isinstance(node, list):
        return len(node)
    if isinstance(node, dict):
        for field in ("count", "total", "n"):
            if isinstance(node.get(field), int):
                return node[field]
        return sum(value for value in node.values() if isinstance(value, int))
    raise AssertionError(f"依赖报告缺少 {key}: {payload}")


def test_delete_archives_and_list_filter(api_client, process_factory):
    proc = process_factory("V0.11 归档列表")
    pid = proc["id"]
    try:
        deleted = _body(api_client.delete(f"/api/processes/{pid}"))
        assert deleted.get("status") in ("archived", "deleted")
        archived = _body(api_client.get(f"/api/processes/{pid}"))
        assert archived["id"] == pid
        assert archived.get("archived_at"), archived
        assert archived.get("archive_reason") == "manual_archive"

        active = api_client.get("/api/processes").json()
        assert all(row["id"] != pid for row in active)
        hidden = api_client.get("/api/processes?include_archived=0").json()
        assert all(row["id"] != pid for row in hidden)
        shown = api_client.get("/api/processes?include_archived=1").json()
        assert any(row["id"] == pid and row.get("archived_at") for row in shown)

        restored = _process_body(api_client.post(f"/api/processes/{pid}/restore"))
        assert restored["id"] == pid
        assert not restored.get("archived_at")
        assert any(row["id"] == pid for row in api_client.get("/api/processes").json())
    finally:
        _cleanup_side_records(pid)


def test_explicit_archive_records_reason_and_actor(api_client, process_factory):
    proc = process_factory("V0.11 归档原因")
    pid = proc["id"]
    try:
        archived = _archive(api_client, pid, "版型停用", "色彩主管")
        assert archived.get("archived_at")
        assert archived.get("archive_reason") == "版型停用"
        assert archived.get("archived_by") == "色彩主管"

        listed = api_client.get("/api/processes?include_archived=1").json()
        row = next(item for item in listed if item["id"] == pid)
        assert row["archive_reason"] == "版型停用"
        assert row["archived_by"] == "色彩主管"
    finally:
        _cleanup_side_records(pid)


def test_archived_metadata_recipe_and_passport_writes_blocked(
    api_client, process_factory, cmyk_icc
):
    proc = process_factory("V0.11 只读派生")
    pid = proc["id"]
    recipe = _recipe_doc(proc)
    try:
        _archive(api_client, pid)

        _archived_error(
            api_client.patch(f"/api/processes/{pid}", json={"name": "不应写入"})
        )
        _archived_error(
            api_client.put(
                f"/api/processes/{pid}/icc",
                params={"filename": "blocked.icc"},
                content=cmyk_icc,
                headers={"Content-Type": "application/octet-stream"},
            )
        )
        _archived_error(
            api_client.post(
                "/api/recipes/predict",
                json={
                    "process_id": pid,
                    "targets": [{"label": "只读", "lab": [50.0, 20.0, 5.0]}],
                },
            )
        )
        _archived_error(
            api_client.post(
                "/api/recipes/verify",
                json={"recipe_id": recipe["id"], "measured_lab": [50.1, 20.0, 5.0]},
            )
        )
        _archived_error(api_client.post(f"/api/processes/{pid}/passport"))
    finally:
        _cleanup_side_records(pid)


def test_archived_qc_evaluation_and_signoff_writes_blocked(
    api_client, process_factory
):
    proc = process_factory("V0.11 只读证据")
    pid = proc["id"]
    session = MeasurementStore().create_session(
        "V0.11 只读测试仪", calibrated_at="2026-01-01T08:00:00", process_id=pid
    )
    evaluation = EvaluationStore().create_evaluation("book", "只读评价", pid)
    signoff = SignoffStore().create(proc, evidence={"file_name": "readonly-proof.pdf"})
    measurement = b"INSTRUMENTATION\t\"Probe\"\nNUMBER_OF_SETS\t1\nBEGIN_DATA\n1\tA\t1\nEND_DATA\n"
    try:
        _archive(api_client, pid)

        _archived_error(
            api_client.post(
                "/api/qc/measurements/import",
                params={
                    "filename": "blocked.txt",
                    "instrument": "Probe",
                    "calibrated_at": "2026-01-01T08:00:00",
                    "process_id": pid,
                },
                content=measurement,
                headers={"Content-Type": "application/octet-stream"},
            )
        )
        _archived_error(
            api_client.post(
                "/api/qc/evaluations",
                params={"template": "book", "title": "不应创建", "process_id": pid},
            )
        )
        _archived_error(
            api_client.post(
                "/api/qc/evaluations/entries",
                params={"evaluation_id": evaluation["id"], "evaluator": "甲"},
                json={"颜色无明显偏色": 5},
            )
        )
        _archived_error(
            api_client.post(
                "/api/signoffs",
                json={
                    "process_id": pid,
                    "title": "不应创建",
                    "evidence": {"file_name": "blocked.pdf"},
                },
            )
        )
        _archived_error(
            api_client.post(
                f"/api/signoffs/{signoff['id']}/submit",
                json={"actor": "设计"},
            )
        )
        assert MeasurementStore().get_session(session["id"]) is not None
    finally:
        _cleanup_side_records(pid)


def test_archived_file_operations_blocked(api_client, process_factory):
    proc = process_factory("V0.11 只读文件")
    pid = proc["id"]
    png = io.BytesIO()
    Image.new("RGB", (32, 32), (180, 130, 85)).save(png, format="PNG")
    pdf = io.BytesIO()
    Image.new("RGB", (64, 64), (120, 60, 160)).save(pdf, format="PDF")
    import base64

    png_b64 = base64.b64encode(png.getvalue()).decode()
    try:
        _archive(api_client, pid)
        _archived_error(
            api_client.post(
                "/api/files/convert",
                json={
                    "image_b64": png_b64,
                    "process_id": pid,
                    "intent": "relative_colorimetric",
                },
            )
        )
        _archived_error(
            api_client.post(
                "/api/files/pdfx",
                params={"process_id": pid, "filename": "blocked.pdf", "strict": "0"},
                content=pdf.getvalue(),
                headers={"Content-Type": "application/pdf"},
            )
        )
    finally:
        _cleanup_side_records(pid)


def test_archived_reads_gamut_and_passport_versions_available(
    api_client, process_factory
):
    proc = process_factory("V0.11 归档可读")
    pid = proc["id"]
    try:
        passport, _ = PassportStore().create(proc)
        _archive(api_client, pid)

        assert _body(api_client.get(f"/api/processes/{pid}"))["id"] == pid
        gamut = _body(api_client.get(f"/api/processes/{pid}/gamut?levels=3"))
        assert gamut["sampled"] == 3 ** 4
        versions = api_client.get(f"/api/processes/{pid}/passport/versions")
        assert versions.status_code == 200, versions.text
        assert any(row["version"] == passport["version"] for row in versions.json())
        latest = api_client.get(f"/api/processes/{pid}/passport")
        assert latest.status_code == 200, latest.text
        assert latest.json()["version"] == passport["version"]
        icc = api_client.get(f"/api/processes/{pid}/icc")
        assert icc.status_code == 200 and icc.content
    finally:
        _cleanup_side_records(pid)


def test_permanent_requires_archived_and_matching_confirmation(
    api_client, process_factory
):
    proc = process_factory("V0.11 永久门禁")
    pid = proc["id"]
    try:
        blocked = api_client.delete(
            f"/api/processes/{pid}/permanent", params={"confirm": pid}
        )
        assert blocked.status_code == 409, blocked.text

        _archive(api_client, pid)
        wrong = api_client.delete(
            f"/api/processes/{pid}/permanent", params={"confirm": "wrong-id"}
        )
        assert wrong.status_code == 422, wrong.text
        assert api_client.get(f"/api/processes/{pid}").status_code == 200
    finally:
        _cleanup_side_records(pid)


def test_dependencies_counts_and_blockers(api_client, process_factory):
    proc = process_factory("V0.11 依赖计数")
    pid = proc["id"]
    try:
        _seed_dependencies(proc)
        deps = _body(api_client.get(f"/api/processes/{pid}/dependencies"))
        for key in (
            "recipes",
            "compensation",
            "calibrations",
            "passports",
            "measurement_sessions",
            "evaluations",
            "signoffs",
        ):
            assert _dependency_count(deps, key) >= 1, (key, deps)
        assert "blockers" in deps
        assert isinstance(deps["blockers"], list)
    finally:
        _cleanup_side_records(pid)


def test_permanent_cleanup_counts_detaches_and_orphans(api_client, process_factory):
    proc = process_factory("V0.11 永久清理")
    pid = proc["id"]
    deps = _seed_dependencies(proc)
    icc_path = PROFILE_DIR / proc["icc_file"]
    assert icc_path.exists()
    try:
        _archive(api_client, pid)
        result = _body(
            api_client.delete(
                f"/api/processes/{pid}/permanent", params={"confirm": pid}
            )
        )
        assert result["status"] == "permanently_deleted"
        assert result["deleted"]["processes"] == 1
        assert result["deleted"]["icc"] == 1
        assert result["deleted"]["recipes"] == 1
        assert result["deleted"]["compensation"] == 1
        assert result["deleted"]["calibrations"] == 1
        assert result["deleted"]["passports"] == 1
        assert result["detached"]["measurement_sessions"] == 1
        assert result["detached"]["evaluations"] == 1
        assert result["orphaned"]["signoffs"] == 1
        assert api_client.get(f"/api/processes/{pid}").status_code == 404
        assert not icc_path.exists()
        assert RecipeStore().get(deps["recipe"]["id"]) is None
        assert CompensationStore().get(pid) is None
        assert CalibrationStore().list_for_process(pid) == []
        assert PassportStore().list_versions(pid) == []

        session = MeasurementStore().get_session(deps["session"]["id"])
        assert session is not None
        assert session.get("process_id") in ("", None)
        assert session.get("detached_from_process") == pid
        assert session.get("detached_at")

        evaluation = EvaluationStore().get(deps["evaluation"]["id"])
        assert evaluation is not None
        assert evaluation.get("process_id") in ("", None)
        assert evaluation.get("detached_from_process") == pid
        assert evaluation.get("detached_at")

        signoff = SignoffStore().get(deps["signoff"]["id"])
        assert signoff is not None
        assert signoff.get("orphaned_at")
        assert signoff.get("orphan_reason")
    finally:
        _cleanup_side_records(pid)


def test_archived_gate_and_manifest(api_client, process_factory):
    proc = process_factory("V0.11 归档门禁")
    pid = proc["id"]
    try:
        signoff = _body(
            api_client.post(
                "/api/signoffs",
                json={
                    "process_id": pid,
                    "title": "归档前放行",
                    "evidence": {"file_name": "gate-proof.pdf"},
                },
            )
        )
        _body(
            api_client.post(
                f"/api/signoffs/{signoff['id']}/submit", json={"actor": "设计"}
            )
        )
        _body(
            api_client.post(
                f"/api/signoffs/{signoff['id']}/approve", json={"actor": "客户"}
            )
        )
        assert _body(
            api_client.get(f"/api/signoffs/gate?process_id={pid}")
        )["allowed"] is True

        _archive(api_client, pid)
        gate = _body(api_client.get(f"/api/signoffs/gate?process_id={pid}"))
        assert gate["allowed"] is False
        assert gate["reason"] == "process_archived"
        manifest = api_client.get(
            f"/api/signoffs/release-manifest?process_id={pid}"
        )
        assert manifest.status_code == 409, manifest.text
        assert ProcessStore().get(pid) is not None
    finally:
        _cleanup_side_records(pid)
