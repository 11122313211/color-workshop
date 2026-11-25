"""工艺护照：工艺色彩证据的版本化、不可变交付物（V0.10）。

护照汇总 ICC、补偿模型、校准轮次、可信测量、配方统计与光谱风险。
快照内容不可变，状态在读取时依据当前工艺动态判定：

- ``invalid``：ICC 文件缺失或不可读；
- ``stale``：当前 ICC / 补偿指纹 / 校准指纹与快照不一致；
- ``incomplete``：无 verified 测量，或 CMYK 工艺无校准轮次；
- ``valid``：其余情况。

护照 hash 为规范化 JSON 的 sha256，排除 generated_at/version/passport_hash，
浮点统一 4 位小数；内容不变时重复 POST 返回旧版本，不新增版本。
"""

from __future__ import annotations

import hashlib
import json
import math
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from .calibration_history import CalibrationStore
from .compensation import CompensationStore
from .filedb import JsonStore
from .measurements import MeasurementStore
from .processes import DATA_DIR, PROFILE_DIR, ProcessStore
from .recipe import RecipeStore
from .spectral import DISCLAIMER, process_spectral_overview

MAX_VERSIONS = 50
HASH_EXCLUDED_KEYS = {"generated_at", "version", "passport_hash", "status", "status_reasons", "id"}
_STORE_WRITE_LOCK = threading.RLock()
STATUS_LABELS = {
    "valid": "有效",
    "incomplete": "证据不完整",
    "stale": "已失效",
    "invalid": "无效",
}
CMYK = "CMYK"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _canonical_value(value: Any) -> Any:
    """JSON 规范化：浮点固定 4 位小数，键排序由 dumps(sort_keys=True) 负责。"""
    if isinstance(value, dict):
        return {str(key): _canonical_value(val) for key, val in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        rounded = round(float(value), 4)
        if rounded == 0:
            return 0
        return int(rounded) if float(rounded).is_integer() else rounded
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return str(value)
    if not math.isfinite(numeric):
        return None
    rounded = round(numeric, 4)
    if rounded == 0:
        return 0
    return int(rounded) if float(rounded).is_integer() else rounded


def _hash_payload(payload: dict) -> str:
    data = {
        key: _canonical_value(value)
        for key, value in payload.items()
        if key not in HASH_EXCLUDED_KEYS
    }
    text = json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def compensation_fingerprint(doc: dict | None) -> str:
    if not doc:
        return ""
    payload = {
        "icc_sha256": doc.get("icc_sha256", ""),
        "n": int(doc.get("n", 0) or 0),
        "bias_device": [round(float(v), 4) for v in (doc.get("bias_device") or [])],
    }
    return hashlib.sha256(
        json.dumps(_canonical_value(payload), sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def calibration_fingerprint(rounds: list[dict]) -> str:
    payload = [
        {
            "id": row.get("id", ""),
            "seq": int(row.get("seq", 0) or 0),
            "session_id": row.get("session_id", ""),
            "icc_sha256": row.get("icc_sha256", ""),
            "overall": row.get("overall", "n/a"),
            "summary": row.get("summary") or {},
            "bias_after": row.get("bias_after") or [],
        }
        for row in sorted(
            rounds,
            key=lambda row: (row.get("created_at", ""), int(row.get("seq", 0) or 0)),
        )
    ]
    return hashlib.sha256(
        json.dumps(_canonical_value(payload), sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _warn(
    warnings: list[dict],
    code: str,
    message: str,
    level: str = "warning",
) -> None:
    if any(item.get("code") == code and item.get("message") == message for item in warnings):
        return
    warnings.append({"code": code, "level": level, "message": message})


def _icc_state(process_doc: dict) -> dict:
    """当前 ICC 可读性与指纹；护照 invalid 判定的唯一现场来源。"""
    path = Path(PROFILE_DIR) / str(process_doc.get("icc_file", ""))
    state = {
        "available": False,
        "readable": False,
        "sha256": process_doc.get("icc_sha256", ""),
        "size": process_doc.get("icc_size", 0),
        "device_space": (process_doc.get("icc_meta") or {}).get("device_space", ""),
        "channels": (process_doc.get("icc_meta") or {}).get("channels", 0),
        "description": (process_doc.get("icc_meta") or {}).get("description", ""),
        "error": "",
    }
    try:
        data = path.read_bytes()
    except OSError:
        state["error"] = "ICC 文件缺失或不可读"
        return state
    state["available"] = True
    state["readable"] = True
    state["size"] = len(data)
    state["sha256"] = hashlib.sha256(data).hexdigest()[:16]
    try:
        meta = ProcessStore.parse_meta(data)
        state.update(
            {
                "device_space": meta.get("device_space", ""),
                "channels": meta.get("channels", 0),
                "description": meta.get("description", ""),
            }
        )
    except Exception:  # noqa: BLE001 —— 有字节但 ICC 结构损坏同样 invalid
        state["readable"] = False
        state["error"] = "ICC 文件结构无效或不可解析"
    return state


def _measurement_evidence(process_id: str) -> dict:
    sessions = [
        session
        for session in MeasurementStore().list_sessions()
        if session.get("process_id") == process_id
    ]
    verified_sessions = 0
    verified_records = 0
    record_files: list[dict] = []
    for session in sessions:
        session_verified = 0
        for record in session.get("records", {}).values():
            if record.get("reliability") == "verified":
                session_verified += 1
                record_files.append(
                    {
                        "session_id": session.get("id", ""),
                        "record_id": record.get("id", ""),
                        "filename": record.get("filename", ""),
                        "imported_at": record.get("imported_at", ""),
                    }
                )
        if session_verified:
            verified_sessions += 1
        verified_records += session_verified
    return {
        "sessions": len(sessions),
        "verified_sessions": verified_sessions,
        "verified_records": verified_records,
        "conditions": sorted({str(s.get("condition", "")) for s in sessions if s.get("condition")}),
        "session_ids": sorted(str(s.get("id", "")) for s in sessions),
        "verified_sources": sorted(record_files, key=lambda row: (row["session_id"], row["record_id"])),
    }


def _recipe_evidence(process_id: str) -> dict:
    rows = RecipeStore().list(process_id)
    if not rows:
        return {
            "total": 0,
            "converged": 0,
            "pass_rate": None,
            "mean_de": None,
            "latest_at": "",
        }
    return {
        "total": len(rows),
        "converged": sum(1 for row in rows if row.get("converged")),
        "pass_rate": round(sum(1 for row in rows if row.get("converged")) / len(rows), 4),
        "mean_de": round(sum(float(row.get("delta_e", 0)) for row in rows) / len(rows), 4),
        "latest_at": rows[0].get("created_at", ""),
    }


def _snapshot_payload(process_doc: dict) -> dict:
    pid = process_doc.get("id", "")
    icc = _icc_state(process_doc)
    comp_doc = CompensationStore().get(pid)
    cal_rounds = CalibrationStore().list_for_process(
        pid,
        process_doc.get("icc_sha256"),
    )
    measurements = _measurement_evidence(pid)
    recipes = _recipe_evidence(pid)
    sessions = MeasurementStore().list_sessions()
    spectral = process_spectral_overview(pid, sessions)
    process_meta = process_doc.get("icc_meta") or {}
    warnings: list[dict] = []

    condition = process_doc.get("measurement_condition", "")
    if not condition:
        _warn(
            warnings,
            "measurement_condition_missing",
            "工艺未登记 M0/M1/M2 测量条件；护照不比较条件，仅记录缺失警告",
            "warning",
        )
    elif measurements["conditions"]:
        mismatched = [value for value in measurements["conditions"] if value != condition]
        if mismatched:
            _warn(
                warnings,
                "measurement_condition_mismatch",
                f"工艺登记 {condition}，但会话条件包含 {', '.join(mismatched)}",
                "warning",
            )

    if not icc["readable"]:
        _warn(warnings, "icc_unreadable", icc["error"] or "ICC 不可读", "error")
    if measurements["verified_records"] == 0:
        _warn(
            warnings,
            "no_verified_measurement",
            "无 verified 测量记录，护照不能视为完整证据",
            "warning",
        )
    if process_meta.get("device_space") == CMYK and not cal_rounds:
        _warn(
            warnings,
            "no_calibration_round",
            "CMYK 工艺尚无校准轮次，护照不能视为完整证据",
            "warning",
        )
    if not comp_doc:
        _warn(
            warnings,
            "no_compensation",
            "工艺尚无实测补偿模型；护照记录为空状态",
            "info",
        )
    if recipes["total"] == 0:
        _warn(warnings, "no_recipe_history", "工艺尚无配方历史", "info")
    if not spectral["available"]:
        _warn(warnings, "no_spectral_data", "无光谱测量数据，护照不伪造光谱段", "warning")
    elif spectral["comparable_sessions"] == 0:
        _warn(
            warnings,
            "no_comparable_spectral_sessions",
            "有光谱数据但没有可比较的同工艺会话对",
            "info",
        )
    for message in spectral.get("warnings") or []:
        _warn(warnings, "spectral_warning", message, "warning")

    payload = {
        "process": {
            "id": pid,
            "name": process_doc.get("name", ""),
            "paper": process_doc.get("paper", ""),
            "ink": process_doc.get("ink", ""),
            "notes": process_doc.get("notes", ""),
            "measurement_condition": condition,
            "icc_sha256": process_doc.get("icc_sha256", ""),
            "icc_meta": {
                "device_space": process_meta.get("device_space", ""),
                "channels": process_meta.get("channels", 0),
                "description": process_meta.get("description", ""),
            },
        },
        "evidence": {
            "icc": icc,
            "compensation": {
                "available": bool(comp_doc),
                "matches_current_icc": bool(
                    comp_doc and comp_doc.get("icc_sha256") == process_doc.get("icc_sha256")
                ),
                "n": int((comp_doc or {}).get("n", 0) or 0),
                "bias_device": [round(float(v), 4) for v in ((comp_doc or {}).get("bias_device") or [])],
                "updated_at": (comp_doc or {}).get("updated_at", ""),
                "fingerprint": compensation_fingerprint(comp_doc),
            },
            "calibration": {
                "available": bool(cal_rounds),
                "rounds": len(cal_rounds),
                "latest_round_id": cal_rounds[0].get("id", "") if cal_rounds else "",
                "latest_at": cal_rounds[0].get("created_at", "") if cal_rounds else "",
                "fingerprint": calibration_fingerprint(cal_rounds),
            },
            "measurements": measurements,
            "recipes": recipes,
            "spectral": spectral,
        },
        "warnings": warnings,
    }
    payload["generated_at"] = _now()
    payload["passport_hash"] = _hash_payload(payload)
    return payload


def _current_calibration_rounds(process_doc: dict) -> list[dict]:
    return CalibrationStore().list_for_process(
        process_doc.get("id", ""),
        process_doc.get("icc_sha256"),
    )


def build_eval_context(process_doc: dict | None) -> dict | None:
    """单次预计算工艺当前 ICC/补偿/校准指纹，供多版本护照状态判定复用（消除 N+1 读盘解析）。"""
    if not process_doc:
        return None
    pid = process_doc.get("id", "")
    icc = _icc_state(process_doc)
    comp_fp = compensation_fingerprint(CompensationStore().get(pid))
    cal_fp = calibration_fingerprint(_current_calibration_rounds(process_doc))
    return {"icc": icc, "comp_fp": comp_fp, "cal_fp": cal_fp}


def evaluate_status(snapshot: dict, process_doc: dict | None, *, context: dict | None = None) -> dict:
    """动态判定护照状态；快照本身永不写入 status。"""
    reasons: list[dict] = []
    if not process_doc:
        _warn(reasons, "process_missing", "工艺不存在", "error")
        return {"status": "invalid", "status_label": STATUS_LABELS["invalid"], "status_reasons": reasons}

    icc = context["icc"] if context is not None else _icc_state(process_doc)
    if not icc["readable"]:
        _warn(reasons, "icc_unreadable", icc["error"] or "ICC 不可读", "error")
        return {"status": "invalid", "status_label": STATUS_LABELS["invalid"], "status_reasons": reasons}

    evidence = snapshot.get("evidence") or {}
    snapshot_icc = (snapshot.get("process") or {}).get("icc_sha256", "")
    if snapshot_icc != icc["sha256"]:
        _warn(
            reasons,
            "icc_changed",
            "当前 ICC 指纹与护照快照不一致",
            "error",
        )

    current_comp_fp = (
        context["comp_fp"]
        if context is not None
        else compensation_fingerprint(CompensationStore().get(process_doc.get("id", "")))
    )
    snapshot_comp_fp = (evidence.get("compensation") or {}).get("fingerprint", "")
    if current_comp_fp != snapshot_comp_fp:
        _warn(
            reasons,
            "compensation_changed",
            "当前补偿模型指纹与护照快照不一致",
            "error",
        )

    current_cal_fp = (
        context["cal_fp"]
        if context is not None
        else calibration_fingerprint(_current_calibration_rounds(process_doc))
    )
    snapshot_cal_fp = (evidence.get("calibration") or {}).get("fingerprint", "")
    if current_cal_fp != snapshot_cal_fp:
        _warn(
            reasons,
            "calibration_changed",
            "当前校准轮次指纹与护照快照不一致",
            "error",
        )

    if reasons:
        return {"status": "stale", "status_label": STATUS_LABELS["stale"], "status_reasons": reasons}

    measurements = evidence.get("measurements") or {}
    calibration = evidence.get("calibration") or {}
    device_space = (snapshot.get("process") or {}).get("icc_meta", {}).get("device_space", "")
    if int(measurements.get("verified_records", 0) or 0) <= 0:
        _warn(
            reasons,
            "no_verified_measurement",
            "快照中没有 verified 测量记录",
            "warning",
        )
    if device_space == CMYK and int(calibration.get("rounds", 0) or 0) <= 0:
        _warn(
            reasons,
            "no_calibration_round",
            "CMYK 工艺快照中没有校准轮次",
            "warning",
        )
    if reasons:
        return {
            "status": "incomplete",
            "status_label": STATUS_LABELS["incomplete"],
            "status_reasons": reasons,
        }
    return {"status": "valid", "status_label": STATUS_LABELS["valid"], "status_reasons": []}


def expose_snapshot(snapshot: dict, process_doc: dict | None, *, context: dict | None = None) -> dict:
    status = evaluate_status(snapshot, process_doc, context=context)
    pid = snapshot.get("process", {}).get("id", "")
    version = int(snapshot.get("version", 0) or 0)
    return {
        "id": f"pp_{pid}_v{version}",
        "process_id": pid,
        "version": version,
        "passport_hash": snapshot.get("passport_hash", ""),
        "generated_at": snapshot.get("generated_at", ""),
        "process": snapshot.get("process") or {},
        "evidence": snapshot.get("evidence") or {},
        "warnings": snapshot.get("warnings") or [],
        **status,
    }


class PassportStore:
    """护照版本库（data/passports.json），每工艺最多保留 50 版。"""

    def __init__(self, path=None) -> None:
        self._store = JsonStore(path or DATA_DIR / "passports.json")

    def _bucket(self, process_id: str) -> dict:
        bucket = self._store.get(process_id)
        if not isinstance(bucket, dict):
            return {"process_id": process_id, "versions": []}
        raw_versions = bucket.get("versions")
        versions: list[dict] = []
        for row in raw_versions if isinstance(raw_versions, list) else []:
            if not isinstance(row, dict):
                continue
            try:
                version = int(row.get("version", 0) or 0)
            except (TypeError, ValueError):
                continue
            passport_hash = row.get("passport_hash")
            if version <= 0 or not isinstance(passport_hash, str) or not passport_hash:
                continue
            versions.append({**row, "version": version})
        versions.sort(key=lambda row: row["version"])
        return {"process_id": process_id, "versions": versions}

    def _save(self, bucket: dict) -> None:
        bucket["versions"] = sorted(
            bucket.get("versions", []),
            key=lambda row: int(row.get("version", 0) or 0),
        )[-MAX_VERSIONS:]
        self._store.put(bucket["process_id"], bucket)

    def latest_snapshot(self, process_id: str) -> dict | None:
        versions = self._bucket(process_id).get("versions", [])
        return versions[-1] if versions else None

    def get_version(self, process_id: str, version: int) -> dict | None:
        for row in self._bucket(process_id).get("versions", []):
            if int(row.get("version", 0) or 0) == int(version):
                return row
        return None

    def get_by_hash(self, process_id: str, passport_hash: str) -> dict | None:
        for row in reversed(self._bucket(process_id).get("versions", [])):
            if row.get("passport_hash") == passport_hash:
                return row
        return None

    def get_by_id(self, process_id: str, passport_id: str) -> dict | None:
        for row in reversed(self._bucket(process_id).get("versions", [])):
            if row.get("id") == passport_id:
                return row
        return None

    def list_versions(self, process_id: str) -> list[dict]:
        return list(reversed(self._bucket(process_id).get("versions", [])))

    def create(self, process_doc: dict) -> tuple[dict, bool]:
        """生成或返回内容相同的现有版本；返回 (exposed, created)。"""
        with _STORE_WRITE_LOCK:
            pid = process_doc.get("id", "")
            bucket = self._bucket(pid)
            payload = _snapshot_payload(process_doc)
            latest = bucket["versions"][-1] if bucket["versions"] else None
            if latest and latest.get("passport_hash") == payload["passport_hash"]:
                return expose_snapshot(latest, process_doc), False

            payload["version"] = int((latest or {}).get("version", 0) or 0) + 1
            payload["id"] = f"pp_{pid}_v{payload['version']}"
            bucket["versions"].append(payload)
            self._save(bucket)
            return expose_snapshot(payload, process_doc), True

    def expose(self, snapshot: dict, process_doc: dict | None, *, context: dict | None = None) -> dict:
        return expose_snapshot(snapshot, process_doc, context=context)

    def summary_for_process(self, process_doc: dict, *, context: dict | None = None) -> dict | None:
        latest = self.latest_snapshot(process_doc.get("id", ""))
        if not latest:
            return None
        exposed = expose_snapshot(latest, process_doc, context=context)
        return {
            "passport_id": exposed["id"],
            "version": exposed["version"],
            "passport_hash": exposed["passport_hash"],
            "generated_at": exposed["generated_at"],
            "status": exposed["status"],
            "status_label": exposed["status_label"],
            "warnings": len(exposed.get("warnings") or []),
            "spectral_risk": (
                (exposed.get("evidence") or {}).get("spectral") or {}
            ).get("max_risk"),
        }

    def remove_process(self, process_id: str) -> bool:
        with _STORE_WRITE_LOCK:
            return self._store.delete(process_id)

    @staticmethod
    def to_markdown(exposed: dict) -> str:
        evidence = exposed.get("evidence") or {}
        measurements = evidence.get("measurements") or {}
        recipes = evidence.get("recipes") or {}
        spectral = evidence.get("spectral") or {}
        calibration = evidence.get("calibration") or {}
        compensation = evidence.get("compensation") or {}
        process = exposed.get("process") or {}
        lines = [
            f"# 工艺护照 · {process.get('name', '未命名工艺')}",
            "",
            f"- 护照版本：v{exposed.get('version')}",
            f"- 状态：{exposed.get('status_label')}（{exposed.get('status')}）",
            f"- Hash：`{exposed.get('passport_hash', '')}`",
            f"- 生成时间：{exposed.get('generated_at', '')}",
            f"- 测量条件：{process.get('measurement_condition') or '未登记'}",
            f"- ICC：`{(evidence.get('icc') or {}).get('sha256', '')}`",
            "",
            "## 证据覆盖",
            "",
            "| 证据 | 状态 / 数量 |",
            "| --- | --- |",
            f"| 补偿模型 | n={compensation.get('n', 0)}，匹配 ICC={compensation.get('matches_current_icc')} |",
            f"| 校准轮次 | {calibration.get('rounds', 0)} 轮 |",
            f"| 可信测量 | {measurements.get('verified_records', 0)} 条 / {measurements.get('sessions', 0)} 会话 |",
            f"| 配方历史 | {recipes.get('total', 0)} 条，达标率 {recipes.get('pass_rate')} |",
            f"| 光谱 | {spectral.get('spectral_patches', 0)} 块，可比较会话 {spectral.get('comparable_sessions', 0)} |",
            "",
            f"> 光谱说明：{DISCLAIMER}",
            "",
        ]
        reasons = exposed.get("status_reasons") or []
        if reasons:
            lines.extend(["## 状态原因", ""])
            lines.extend(f"- {row.get('message', '')}" for row in reasons)
            lines.append("")
        warnings = exposed.get("warnings") or []
        if warnings:
            lines.extend(["## 警告", ""])
            lines.extend(
                f"- [{row.get('level', 'warning')}] {row.get('message', '')}"
                for row in warnings
            )
            lines.append("")
        return "\n".join(lines)

    @staticmethod
    def to_json(exposed: dict) -> str:
        return json.dumps(exposed, ensure_ascii=False, indent=2)

