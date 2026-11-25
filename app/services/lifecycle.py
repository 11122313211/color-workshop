"""工艺生命周期：归档只读、依赖统计与永久清理（V0.11）。

删除策略为「归档优先」：归档工艺及其证据默认全部保留、只读；只有显式永久
清理才删除工艺与派生记录，而测量会话、主观评价与签样作为审计证据保留：

- 测量会话 / 评价：解绑（``process_id`` 置空）并写 ``detached_from_process`` /
  ``detached_at``；
- 签样：不删除，写 ``orphaned_at`` / ``orphan_reason``。

本模块只做编排，不引入 FastAPI，便于 CLI 与测试直接复用。
"""

from __future__ import annotations

from datetime import datetime

from pathlib import Path

from .filedb import JsonStore, MAINTENANCE_LOCK
from .processes import DATA_DIR

ARCHIVED_CODE = "PROCESS_ARCHIVED"
ARCHIVED_MESSAGE = "工艺已归档，只读"
ARCHIVE_REASON_MANUAL = "manual_archive"
ORPHAN_REASON_PERMANENT_DELETE = "process_permanently_deleted"


class ProcessNotArchivedError(Exception):
    """尝试永久清理未归档工艺。"""

    code = "not_archived"
    message = "仅已归档工艺可永久清理"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def archived_detail() -> dict:
    """归档只读 409 的结构化 detail（每次返回新 dict，避免调用方误改）。"""
    return {"code": ARCHIVED_CODE, "message": ARCHIVED_MESSAGE}


def is_archived(process_doc: dict | None) -> bool:
    return bool((process_doc or {}).get("archived_at"))


def dependency_report(process_doc: dict) -> dict:
    """返回工艺证据依赖的分状态计数与永久清理 blockers。"""
    from .calibration_history import CalibrationStore
    from .compensation import CompensationStore
    from .evaluations import EvaluationStore
    from .measurements import MeasurementStore
    from .passport import PassportStore
    from .recipe import RecipeStore
    from .signoffs import SIGN_STATES, SignoffStore

    pid = process_doc.get("id", "")
    archived = is_archived(process_doc)

    recipes = RecipeStore().list(pid)
    compensation = CompensationStore().get(pid)
    calibrations = CalibrationStore().list_for_process(pid)
    passport_versions = PassportStore().list_versions(pid)
    sessions = MeasurementStore().list_sessions()
    bound_sessions = [s for s in sessions if s.get("process_id") == pid]
    detached_sessions = [s for s in sessions if s.get("detached_from_process") == pid]
    evaluations = EvaluationStore().list_evaluations()
    bound_evaluations = [e for e in evaluations if e.get("process_id") == pid]
    detached_evaluations = [
        e for e in evaluations if e.get("detached_from_process") == pid
    ]
    signoffs = SignoffStore().list(process_id=pid)
    signoff_states = {state: 0 for state in SIGN_STATES}
    for doc in signoffs:
        state = str(doc.get("state") or "draft")
        signoff_states[state] = signoff_states.get(state, 0) + 1
    approved_or_locked = sum(
        1 for doc in signoffs if doc.get("state") in ("approved", "locked")
    )
    orphaned_signoffs = sum(1 for doc in signoffs if doc.get("orphaned_at"))

    blockers: list[dict] = []
    if not archived:
        blockers.append({
            "code": ProcessNotArchivedError.code,
            "message": ProcessNotArchivedError.message,
        })

    counts = {
        "recipes": len(recipes),
        "compensation": 1 if compensation else 0,
        "calibrations": len(calibrations),
        "passports": len(passport_versions),
        "measurement_sessions": len(bound_sessions),
        "evaluations": len(bound_evaluations),
        "signoffs": len(signoffs),
    }
    return {
        "process_id": pid,
        "name": process_doc.get("name", ""),
        "archived": archived,
        "archived_at": process_doc.get("archived_at", ""),
        "archive_reason": process_doc.get("archive_reason", ""),
        "archived_by": process_doc.get("archived_by", ""),
        "recipes": {
            "total": len(recipes),
            "by_state": {"active": len(recipes)},
            "latest_at": recipes[0].get("created_at", "") if recipes else "",
        },
        "compensation": {
            "exists": bool(compensation),
            "total": 1 if compensation else 0,
            "n": int((compensation or {}).get("n", 0) or 0),
            "icc_sha256": (compensation or {}).get("icc_sha256", ""),
            "updated_at": (compensation or {}).get("updated_at", ""),
        },
        "calibrations": {
            "total": len(calibrations),
            "by_state": {"active": len(calibrations)},
        },
        "passports": {
            "total": len(passport_versions),
            "by_state": {"active": len(passport_versions)},
            "latest_version": (
                int(passport_versions[0].get("version", 0) or 0)
                if passport_versions
                else None
            ),
        },
        "measurement_sessions": {
            "total": len(bound_sessions),
            "by_state": {
                "bound": len(bound_sessions),
                "detached": len(detached_sessions),
            },
            "records": sum(
                len(session.get("records") or {}) for session in bound_sessions
            ),
        },
        "evaluations": {
            "total": len(bound_evaluations),
            "by_state": {
                "bound": len(bound_evaluations),
                "detached": len(detached_evaluations),
            },
        },
        "signoffs": {
            "total": len(signoffs),
            "by_state": signoff_states,
            "approved_or_locked": approved_or_locked,
            "orphaned": orphaned_signoffs,
        },
        "counts": counts,
        "blockers": blockers,
        "can_permanent_delete": not blockers,
    }


def permanently_delete_process(process_doc: dict) -> dict:
    """永久清理已归档工艺。

    删除工艺 + ICC + 派生记录（配方/补偿/校准/护照）；测量会话与评价解绑保留；
    签样保留并标记 orphaned。调用方需确保 ``process_doc`` 已归档。
    """
    from .calibration_history import CalibrationStore
    from .compensation import CompensationStore
    from .passport import PassportStore
    from .processes import ProcessStore
    from .recipe import RecipeStore

    pid = process_doc.get("id", "")
    with MAINTENANCE_LOCK:
        # 持维护锁后重读工艺记录，防止并发 restore 后仍继续永久清理
        latest_proc = ProcessStore().get(pid)
        if latest_proc is None:
            raise KeyError(pid)
        if not is_archived(latest_proc):
            raise ProcessNotArchivedError(ProcessNotArchivedError.message)

        now = _now()
        passport_versions = PassportStore().list_versions(pid)
        icc_name = Path(str(latest_proc.get("icc_file", "") or "")).name
        icc_path = DATA_DIR / "processes" / icc_name if icc_name else None
        icc_existed = bool(icc_path and icc_path.exists())

        # 1) 测量会话批量解绑保留（单次原子落盘，证据不删）
        session_store = JsonStore(DATA_DIR / "measurements.json")

        def _detach_sessions(data: dict) -> int:
            count = 0
            for current in data.values():
                if not isinstance(current, dict) or current.get("process_id") != pid:
                    continue
                current["process_id"] = ""
                current["detached_from_process"] = pid
                current["detached_at"] = now
                count += 1
            return count

        detached_sessions = session_store.update(_detach_sessions)

        # 2) 主观评价批量解绑保留（单次原子落盘，证据不删）
        evaluation_store = JsonStore(DATA_DIR / "evaluations.json")

        def _detach_evaluations(data: dict) -> int:
            count = 0
            for current in data.values():
                if not isinstance(current, dict) or current.get("process_id") != pid:
                    continue
                current["process_id"] = ""
                current["detached_from_process"] = pid
                current["detached_at"] = now
                count += 1
            return count

        detached_evaluations = evaluation_store.update(_detach_evaluations)

        # 3) 签样批量保留并标记 orphaned（单次原子落盘，不删证据）
        signoff_store = JsonStore(DATA_DIR / "signoffs.json")

        def _orphan_signoffs(data: dict) -> int:
            count = 0
            for current in data.values():
                if not isinstance(current, dict) or current.get("process_id") != pid:
                    continue
                if not current.get("orphaned_at"):
                    current["orphaned_at"] = now
                    current["orphan_reason"] = ORPHAN_REASON_PERMANENT_DELETE
                    count += 1
            return count

        orphaned_signoffs = signoff_store.update(_orphan_signoffs)

        # 4) 删除派生记录
        deleted_recipes = RecipeStore().remove_process(pid)
        deleted_compensation = 1 if CompensationStore().reset(pid) else 0
        deleted_calibrations = CalibrationStore().remove_process(pid)
        deleted_passport_versions = len(passport_versions)
        PassportStore().remove_process(pid)

        # 5) 最后删除工艺与 ICC（ProcessStore.delete 内部同时 unlink ICC 并清理缓存）
        if not _hard_delete_process(pid):
            raise KeyError(pid)

    return {
        "status": "permanently_deleted",
        "process_id": pid,
        "deleted": {
            "processes": 1,
            "icc": 1 if icc_existed else 0,
            "recipes": deleted_recipes,
            "compensation": deleted_compensation,
            "calibrations": deleted_calibrations,
            "passports": deleted_passport_versions,
        },
        "detached": {
            "measurement_sessions": detached_sessions,
            "evaluations": detached_evaluations,
        },
        "orphaned": {"signoffs": orphaned_signoffs},
        "detached_at": now,
        "orphaned_at": now if orphaned_signoffs else "",
    }


def _hard_delete_process(pid: str) -> bool:
    """延迟导入的硬删除包装（避免模块级循环依赖）。"""
    from .processes import ProcessStore

    return ProcessStore().delete(pid)
