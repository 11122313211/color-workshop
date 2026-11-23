"""V0.11 完整性审计、dry-run 与修复语义测试。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from app.services.calibration_history import CalibrationStore
from app.services.compensation import CompensationStore
from app.services.evaluations import EvaluationStore
from app.services.measurements import MeasurementStore
from app.services.passport import PassportStore
from app.services.processes import DATA_DIR, PROFILE_DIR
from app.services.recipe import RecipeStore
from app.services.signoffs import SignoffStore

pytestmark = pytest.mark.api


STORE_FILES = (
    "recipes.json",
    "compensations.json",
    "calibrations.json",
    "passports.json",
    "measurements.json",
    "evaluations.json",
    "signoffs.json",
)


def _body(response, status: int = 200) -> dict:
    assert response.status_code == status, (response.status_code, response.text)
    return response.json()


def _fake_process(pid: str) -> dict:
    return {
        "id": pid,
        "name": f"孤儿工艺 {pid}",
        "paper": "测试纸",
        "ink": "四色",
        "measurement_condition": "",
        "icc_file": f"{pid}.icc",
        "icc_original_name": f"{pid}.icc",
        "icc_size": 0,
        "icc_sha256": "0" * 16,
        "icc_meta": {"device_space": "CMYK", "channels": 4},
        "created_at": "2026-01-01T00:00:00",
    }


def _json_snapshot(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _snapshot() -> dict:
    return {
        "stores": {name: _json_snapshot(DATA_DIR / name) for name in STORE_FILES},
        "icc_files": sorted(path.name for path in PROFILE_DIR.glob("*") if path.is_file()),
        "orphaned_icc": (
            sorted(path.name for path in (DATA_DIR / "orphaned_icc").glob("*"))
            if (DATA_DIR / "orphaned_icc").exists()
            else []
        ),
    }


@pytest.fixture()
def integrity_seed(api_client, process_factory, cmyk_icc):
    referenced = process_factory("V0.11 完整性缺失 ICC")
    referenced_icc = PROFILE_DIR / referenced["icc_file"]
    referenced_icc.unlink()

    orphan_pid = f"orphan_{uuid.uuid4().hex[:10]}"
    fake = _fake_process(orphan_pid)
    recipe = RecipeStore().create(
        fake,
        {
            "target_lab": [50.0, 0.0, 0.0],
            "recipe": {"C": 0.0, "M": 0.0, "Y": 0.0, "K": 50.0},
            "predicted_lab": [50.0, 0.0, 0.0],
            "delta_e": 1.0,
            "iterations": [],
            "predictions": 1,
            "lut_points": 1,
            "converged": True,
        },
        "孤儿配方",
    )
    CompensationStore().accumulate(
        orphan_pid, fake["icc_sha256"], [0.01, 0.0, 0.0, 0.0]
    )
    calibration = CalibrationStore().save_round(
        orphan_pid,
        fake["icc_sha256"],
        f"sess_cal_{uuid.uuid4().hex[:8]}",
        {
            "overall": "ok",
            "summary": {"max_de_expected": 1.0, "avg_de_expected": 0.5},
            "ink_suggestions": [],
        },
        [0.0, 0.0, 0.0, 0.0],
        [0.01, 0.0, 0.0, 0.0],
    )
    passport, _ = PassportStore().create(fake)
    session = MeasurementStore().create_session(
        "V0.11 孤儿测量仪",
        calibrated_at="2026-01-01T08:00:00",
        process_id=orphan_pid,
    )
    evaluation = EvaluationStore().create_evaluation(
        "book", "V0.11 孤儿评价", orphan_pid
    )
    signoff = SignoffStore().create(fake, evidence={"file_name": "orphan-proof.pdf"})

    unused = PROFILE_DIR / f"orphan_{uuid.uuid4().hex[:10]}.icc"
    unused.write_bytes(cmyk_icc)

    yield {
        "referenced": referenced,
        "referenced_icc": referenced_icc.name,
        "orphan_pid": orphan_pid,
        "recipe": recipe,
        "calibration": calibration,
        "passport": passport,
        "session": session,
        "evaluation": evaluation,
        "signoff": signoff,
        "unused_icc": unused,
    }

    RecipeStore().remove_process(orphan_pid)
    CompensationStore().reset(orphan_pid)
    CalibrationStore().remove_process(orphan_pid)
    PassportStore().remove_process(orphan_pid)
    if MeasurementStore().get_session(session["id"]):
        MeasurementStore().delete_session(session["id"])
    if EvaluationStore().get(evaluation["id"]):
        EvaluationStore().delete_evaluation(evaluation["id"])
    signoff_store = SignoffStore()
    if signoff_store.get(signoff["id"]):
        signoff_store._store.delete(signoff["id"])
    unused.unlink(missing_ok=True)
    orphaned = DATA_DIR / "orphaned_icc" / unused.name
    orphaned.unlink(missing_ok=True)


def test_integrity_report_identifies_every_orphan_kind(api_client, integrity_seed):
    report = _body(api_client.get("/api/integrity/report"))
    text = json.dumps(report, ensure_ascii=False)
    details = report["details"]

    assert integrity_seed["orphan_pid"] in text
    assert integrity_seed["recipe"]["id"] in text
    assert integrity_seed["calibration"]["id"] in text
    assert integrity_seed["session"]["id"] in text
    assert integrity_seed["evaluation"]["id"] in text
    assert integrity_seed["signoff"]["id"] in text
    assert integrity_seed["referenced_icc"] in text
    assert integrity_seed["unused_icc"].name in text
    assert any(
        item["process_id"] == integrity_seed["orphan_pid"]
        for item in details["orphan_passports"]
    )
    assert any(
        item["process_id"] == integrity_seed["orphan_pid"]
        for item in details["orphan_compensations"]
    )
    assert "summary" in report or "counts" in report


def test_integrity_dry_run_does_not_write(api_client, integrity_seed):
    _body(api_client.get("/api/integrity/report"))
    before = _snapshot()
    preview = _body(api_client.post("/api/integrity/repair", json={"dry_run": True}))
    after = _snapshot()

    assert before == after
    assert preview.get("dry_run") is True
    assert preview["planned"]["deleted"]["recipes"] >= 1
    assert preview["planned"]["deleted"]["passports"] >= 1
    assert preview["planned"]["detached"]["measurement_sessions"] >= 1
    assert preview["planned"]["orphaned"]["signoffs"] >= 1
    assert preview["planned"]["quarantined"]["icc_files"] >= 1
    assert integrity_seed["recipe"]["id"] in json.dumps(preview, ensure_ascii=False)


def test_integrity_apply_cleans_detaches_orphans_and_quarantines_icc(
    api_client, integrity_seed
):
    result = _body(api_client.post("/api/integrity/repair", json={"dry_run": False}))
    assert result.get("dry_run") is False

    assert RecipeStore().get(integrity_seed["recipe"]["id"]) is None
    assert CompensationStore().get(integrity_seed["orphan_pid"]) is None
    assert CalibrationStore().list_for_process(integrity_seed["orphan_pid"]) == []
    assert PassportStore().list_versions(integrity_seed["orphan_pid"]) == []

    session = MeasurementStore().get_session(integrity_seed["session"]["id"])
    assert session is not None
    assert session.get("process_id") in ("", None)
    assert session.get("detached_from_process") == integrity_seed["orphan_pid"]
    assert session.get("detached_at")

    evaluation = EvaluationStore().get(integrity_seed["evaluation"]["id"])
    assert evaluation is not None
    assert evaluation.get("process_id") in ("", None)
    assert evaluation.get("detached_from_process") == integrity_seed["orphan_pid"]
    assert evaluation.get("detached_at")

    signoff = SignoffStore().get(integrity_seed["signoff"]["id"])
    assert signoff is not None
    assert signoff.get("orphaned_at")
    assert signoff.get("orphan_reason")

    unused = integrity_seed["unused_icc"]
    assert not unused.exists()
    quarantined = DATA_DIR / "orphaned_icc" / unused.name
    assert quarantined.exists()


def test_integrity_apply_is_idempotent(api_client, integrity_seed):
    first = _body(api_client.post("/api/integrity/repair", json={"dry_run": False}))
    assert first
    before = _snapshot()
    second = _body(api_client.post("/api/integrity/repair", json={"dry_run": False}))
    after = _snapshot()

    assert before == after
    assert second["counts"]["deleted"] == {
        "recipes": 0,
        "compensations": 0,
        "calibrations": 0,
        "passports": 0,
    }
    assert second["counts"]["detached"] == {
        "measurement_sessions": 0,
        "evaluations": 0,
    }
    assert second["counts"]["orphaned"] == {"signoffs": 0}
    assert second["counts"]["quarantined"] == {"icc_files": 0}
    report = _body(api_client.get("/api/integrity/report"))
    text = json.dumps(report, ensure_ascii=False)
    assert integrity_seed["recipe"]["id"] not in text
    assert integrity_seed["session"]["id"] not in text
    assert integrity_seed["signoff"]["id"] not in text
    assert integrity_seed["unused_icc"].name not in text


def test_integrity_cli_dry_run_is_executable(integrity_seed):
    script = Path(__file__).resolve().parent.parent / "scripts" / "audit_data.py"
    assert script.exists(), "V0.11 必须提供 scripts/audit_data.py"
    env = {
        **os.environ,
        "COLORFORGE_DATA_DIR": str(DATA_DIR),
        "PYTHONIOENCODING": "utf-8",
    }
    proc = subprocess.run(
        [sys.executable, str(script), "--dry-run"],
        cwd=script.parent.parent,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=60,
    )
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    output = (proc.stdout or "") + (proc.stderr or "")
    assert "dry" in output.lower() or "孤儿" in output or "orphan" in output.lower()
    assert integrity_seed["recipe"]["id"] in output


def test_integrity_report_survives_clean_data(api_client):
    report = _body(api_client.get("/api/integrity/report"))
    assert isinstance(report, dict)
    text = json.dumps(report, ensure_ascii=False)
    assert "generated_at" in text or "scan" in text.lower()
