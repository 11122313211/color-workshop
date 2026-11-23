"""数据完整性审计与修复（V0.11）。

审计范围
--------
- active / archived 工艺计数；
- 孤儿派生记录：配方、补偿模型、校准轮次、护照（``process_id`` 已不存在）；
- 孤儿证据记录：测量会话、主观评价（未 detach）、签样（未 orphaned）；
- 缺失 ICC（仅报告，绝不伪造）；
- 未被任何工艺引用的 ICC 文件（移至 ``data/orphaned_icc/`` 隔离）。

修复策略（显式 apply 才写盘）
----------------------------
- 测量会话 / 评价：解绑保留，写 ``detached_from_process`` / ``detached_at``；
- 签样：保留并标记 ``orphaned_at`` / ``orphan_reason``；
- 派生记录（配方/补偿/校准/护照）：删除；
- 未引用 ICC：移动到隔离目录，重名自动追加序号。

``dry_run=True`` 为默认值，只生成计划，绝不创建目录或写任何文件。
"""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

from .filedb import JsonStore
from .processes import DATA_DIR, PROFILE_DIR, ProcessStore

ORPHANED_ICC_DIR = DATA_DIR / "orphaned_icc"
ORPHAN_REASON_MISSING_PROCESS = "process_missing"

# 修复动作定义：action 为稳定标识；aliases 兼容按类别/详情键筛选。
ACTION_SPECS: tuple[dict, ...] = (
    {
        "action": "detach_orphan_measurement_sessions",
        "category": "measurement_sessions",
        "detail": "orphan_measurement_sessions",
        "description": "孤儿测量会话解绑保留（写 detached_from_process/detached_at）",
        "aliases": (
            "detach_measurements",
            "detach_measurement_sessions",
            "detach_orphan_measurements",
            "orphan_measurement_sessions",
            "orphan_measurements",
            "measurements",
        ),
    },
    {
        "action": "detach_orphan_evaluations",
        "category": "evaluations",
        "detail": "orphan_evaluations",
        "description": "孤儿主观评价解绑保留（写 detached_from_process/detached_at）",
        "aliases": (
            "detach_evaluations",
            "detach_orphan_evaluation",
            "orphan_evaluations",
            "evaluation",
        ),
    },
    {
        "action": "mark_orphan_signoffs",
        "category": "signoffs",
        "detail": "orphan_signoffs",
        "description": "孤儿签样标记 orphaned 保留（不删除证据）",
        "aliases": (
            "mark_signoffs_orphaned",
            "mark_orphan_signoff",
            "orphan_signoffs",
            "signoff",
        ),
    },
    {
        "action": "delete_orphan_recipes",
        "category": "recipes",
        "detail": "orphan_recipes",
        "description": "删除孤儿配方记录（派生数据）",
        "aliases": (
            "delete_recipes",
            "orphan_recipes",
            "recipe",
        ),
    },
    {
        "action": "delete_orphan_compensations",
        "category": "compensations",
        "detail": "orphan_compensations",
        "description": "删除孤儿补偿模型（派生数据）",
        "aliases": (
            "delete_compensations",
            "orphan_compensations",
            "compensation",
        ),
    },
    {
        "action": "delete_orphan_calibrations",
        "category": "calibrations",
        "detail": "orphan_calibrations",
        "description": "删除孤儿校准轮次（派生数据）",
        "aliases": (
            "delete_calibrations",
            "orphan_calibrations",
            "calibration",
        ),
    },
    {
        "action": "delete_orphan_passports",
        "category": "passports",
        "detail": "orphan_passports",
        "description": "删除孤儿护照版本桶（派生数据）",
        "aliases": (
            "delete_passports",
            "orphan_passports",
            "passport",
        ),
    },
    {
        "action": "quarantine_unreferenced_icc",
        "category": "icc_files",
        "detail": "unreferenced_icc",
        "description": "未引用 ICC 移至 data/orphaned_icc/ 隔离",
        "aliases": (
            "quarantine_icc",
            "quarantine_orphan_icc",
            "unreferenced_icc",
            "icc",
            "icc_files",
        ),
    },
)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _store(name: str) -> JsonStore:
    return JsonStore(DATA_DIR / name)


def _raw_passport_versions(bucket: dict) -> list:
    versions = bucket.get("versions") if isinstance(bucket, dict) else None
    return versions if isinstance(versions, list) else []


def _issue_item_process(doc: dict, key: str = "process_id") -> str:
    return str(doc.get(key, "") or "")


def build_report() -> dict:
    """构建只读完整性报告（不写盘）。"""
    process_docs = ProcessStore().list(include_archived=True)
    process_ids = {
        str(doc.get("id", "")) for doc in process_docs if doc.get("id")
    }
    active = [doc for doc in process_docs if not doc.get("archived_at")]
    archived = [doc for doc in process_docs if doc.get("archived_at")]

    recipe_docs = list(_store("recipes.json").all().values())
    compensation_docs = _store("compensations.json").all()
    calibration_docs = list(_store("calibrations.json").all().values())
    passport_buckets = _store("passports.json").all()

    from .evaluations import EvaluationStore
    from .measurements import MeasurementStore
    from .signoffs import SignoffStore

    sessions = MeasurementStore().list_sessions()
    evaluations = EvaluationStore().list_evaluations()
    signoffs = SignoffStore().list()

    orphan_recipes = [
        doc for doc in recipe_docs
        if _issue_item_process(doc) and _issue_item_process(doc) not in process_ids
    ]
    orphan_compensations = [
        {**doc, "process_id": str(pid)}
        for pid, doc in compensation_docs.items()
        if isinstance(doc, dict) and str(pid) not in process_ids
    ]
    orphan_calibrations = [
        doc for doc in calibration_docs
        if _issue_item_process(doc) and _issue_item_process(doc) not in process_ids
    ]
    orphan_passports = [
        {"process_id": str(pid), "versions": len(_raw_passport_versions(doc))}
        for pid, doc in passport_buckets.items()
        if str(pid) not in process_ids
    ]
    orphan_sessions = [
        session for session in sessions
        if _issue_item_process(session)
        and _issue_item_process(session) not in process_ids
        and not session.get("detached_from_process")
        and not session.get("detached_at")
    ]
    orphan_evaluations = [
        evaluation for evaluation in evaluations
        if _issue_item_process(evaluation)
        and _issue_item_process(evaluation) not in process_ids
        and not evaluation.get("detached_from_process")
        and not evaluation.get("detached_at")
    ]
    orphan_signoffs = [
        signoff for signoff in signoffs
        if _issue_item_process(signoff)
        and _issue_item_process(signoff) not in process_ids
        and not signoff.get("orphaned_at")
    ]

    missing_icc = []
    referenced_icc: set[str] = set()
    for doc in process_docs:
        icc_file = str(doc.get("icc_file", "") or "")
        if icc_file:
            referenced_icc.add(icc_file)
        path = PROFILE_DIR / icc_file if icc_file else None
        if not path or not path.exists():
            missing_icc.append({
                "process_id": doc.get("id", ""),
                "name": doc.get("name", ""),
                "icc_file": icc_file,
                "archived": bool(doc.get("archived_at")),
            })

    unreferenced_icc = []
    if PROFILE_DIR.exists():
        for path in sorted(PROFILE_DIR.iterdir(), key=lambda p: p.name.lower()):
            if not path.is_file() or path.suffix.lower() not in (".icc", ".icm"):
                continue
            if path.name in referenced_icc:
                continue
            unreferenced_icc.append({
                "filename": path.name,
                "size": path.stat().st_size,
                "quarantine_target": str(
                    ORPHANED_ICC_DIR / path.name
                ).replace("\\", "/"),
            })

    details = {
        "processes": {
            "active": [
                {"id": doc.get("id", ""), "name": doc.get("name", "")}
                for doc in active
            ],
            "archived": [
                {
                    "id": doc.get("id", ""),
                    "name": doc.get("name", ""),
                    "archived_at": doc.get("archived_at", ""),
                    "archive_reason": doc.get("archive_reason", ""),
                }
                for doc in archived
            ],
        },
        "orphan_recipes": [
            {
                "id": doc.get("id", ""),
                "process_id": doc.get("process_id", ""),
                "process_name": doc.get("process_name", ""),
                "label": doc.get("label", ""),
                "created_at": doc.get("created_at", ""),
            }
            for doc in orphan_recipes
        ],
        "orphan_compensations": [
            {
                "process_id": doc.get("process_id", ""),
                "icc_sha256": doc.get("icc_sha256", ""),
                "n": int(doc.get("n", 0) or 0),
                "updated_at": doc.get("updated_at", ""),
            }
            for doc in orphan_compensations
        ],
        "orphan_calibrations": [
            {
                "id": doc.get("id", ""),
                "process_id": doc.get("process_id", ""),
                "session_id": doc.get("session_id", ""),
                "created_at": doc.get("created_at", ""),
            }
            for doc in orphan_calibrations
        ],
        "orphan_passports": orphan_passports,
        "orphan_measurement_sessions": [
            {
                "id": session.get("id", ""),
                "process_id": session.get("process_id", ""),
                "instrument": session.get("instrument", ""),
                "records": len(session.get("records") or {}),
                "created_at": session.get("created_at", ""),
            }
            for session in orphan_sessions
        ],
        "orphan_evaluations": [
            {
                "id": evaluation.get("id", ""),
                "process_id": evaluation.get("process_id", ""),
                "title": evaluation.get("title", ""),
                "created_at": evaluation.get("created_at", ""),
            }
            for evaluation in orphan_evaluations
        ],
        "orphan_signoffs": [
            {
                "id": signoff.get("id", ""),
                "process_id": signoff.get("process_id", ""),
                "state": signoff.get("state", "draft"),
                "title": signoff.get("title", ""),
                "created_at": signoff.get("created_at", ""),
            }
            for signoff in orphan_signoffs
        ],
        "missing_icc": missing_icc,
        "unreferenced_icc": unreferenced_icc,
    }

    summary = {
        "processes_active": len(active),
        "processes_archived": len(archived),
        "processes_total": len(process_docs),
        "recipes_total": len(recipe_docs),
        "compensations_total": len(compensation_docs),
        "calibrations_total": len(calibration_docs),
        "passports_total": len(passport_buckets),
        "measurement_sessions_total": len(sessions),
        "evaluations_total": len(evaluations),
        "signoffs_total": len(signoffs),
        "orphan_recipes": len(orphan_recipes),
        "orphan_compensations": len(orphan_compensations),
        "orphan_calibrations": len(orphan_calibrations),
        "orphan_passports": len(orphan_passports),
        "orphan_measurement_sessions": len(orphan_sessions),
        "orphan_evaluations": len(orphan_evaluations),
        "orphan_signoffs": len(orphan_signoffs),
        "missing_icc": len(missing_icc),
        "unreferenced_icc": len(unreferenced_icc),
    }
    summary["issues_total"] = sum(
        summary[key]
        for key in (
            "orphan_recipes",
            "orphan_compensations",
            "orphan_calibrations",
            "orphan_passports",
            "orphan_measurement_sessions",
            "orphan_evaluations",
            "orphan_signoffs",
            "missing_icc",
            "unreferenced_icc",
        )
    )

    actions = []
    for spec in ACTION_SPECS:
        items = details[spec["detail"]]
        if not items:
            continue
        if spec["detail"] == "unreferenced_icc":
            ids = [item.get("filename", "") for item in items]
        elif spec["category"] == "compensations":
            ids = [item.get("process_id", "") for item in items]
        elif spec["category"] == "passports":
            ids = [item.get("process_id", "") for item in items]
        else:
            ids = [item.get("id", "") for item in items]
        actions.append({
            "action": spec["action"],
            "category": spec["category"],
            "description": spec["description"],
            "count": len(items),
            "ids": ids,
        })

    return {
        "generated_at": _now(),
        "summary": summary,
        "details": details,
        "actions": actions,
    }


def _normalize_filters(actions) -> list[str]:
    filters: list[str] = []
    for row in actions or []:
        if isinstance(row, str):
            value = row.strip().lower().replace("-", "_")
            if value:
                filters.append(value)
            continue
        if isinstance(row, dict):
            found = False
            for key in ("action", "category", "target_type", "type", "name"):
                value = row.get(key)
                if isinstance(value, str) and value.strip():
                    filters.append(value.strip().lower().replace("-", "_"))
                    found = True
                    break
            if not found:
                raise ValueError("actions 对象需包含 action/category 等动作标识")
            continue
        raise ValueError("actions 仅支持字符串或对象")
    return filters


def _selected_specs(filters: list[str]) -> list[dict]:
    if not filters or any(value in ("all", "*") for value in filters):
        return list(ACTION_SPECS)
    selected = []
    unknown = []
    for value in filters:
        matched = []
        for spec in ACTION_SPECS:
            aliases = {
                spec["action"].lower(),
                spec["category"].lower(),
                spec["detail"].lower(),
                *(str(alias).lower() for alias in spec["aliases"]),
            }
            if value in aliases:
                matched.append(spec)
        if not matched:
            unknown.append(value)
            continue
        for spec in matched:
            if spec not in selected:
                selected.append(spec)
    if unknown:
        valid = sorted({spec["action"] for spec in ACTION_SPECS})
        raise ValueError(
            f"未知修复动作: {', '.join(unknown)}；可用动作: {', '.join(valid)}"
        )
    return selected


def _quarantine_target(filename: str) -> Path:
    target = ORPHANED_ICC_DIR / filename
    if not target.exists():
        return target
    stem = Path(filename).stem
    suffix = Path(filename).suffix
    index = 1
    while True:
        candidate = ORPHANED_ICC_DIR / f"{stem}.{index}{suffix}"
        if not candidate.exists():
            return candidate
        index += 1


def repair(dry_run: bool = True, actions=None) -> dict:
    """审计并按策略修复；``dry_run=True`` 只生成计划，不写盘。"""
    filters = _normalize_filters(actions)
    specs = _selected_specs(filters)
    report = build_report()
    details = report["details"]
    summary = report["summary"]

    planned = {
        "deleted": {
            "recipes": summary["orphan_recipes"] if _has(specs, "delete_orphan_recipes") else 0,
            "compensations": summary["orphan_compensations"] if _has(specs, "delete_orphan_compensations") else 0,
            "calibrations": summary["orphan_calibrations"] if _has(specs, "delete_orphan_calibrations") else 0,
            "passports": (
                sum(item.get("versions", 0) for item in details["orphan_passports"])
                if _has(specs, "delete_orphan_passports") else 0
            ),
        },
        "detached": {
            "measurement_sessions": (
                summary["orphan_measurement_sessions"]
                if _has(specs, "detach_orphan_measurement_sessions") else 0
            ),
            "evaluations": (
                summary["orphan_evaluations"]
                if _has(specs, "detach_orphan_evaluations") else 0
            ),
        },
        "orphaned": {
            "signoffs": (
                summary["orphan_signoffs"]
                if _has(specs, "mark_orphan_signoffs") else 0
            ),
        },
        "quarantined": {
            "icc_files": (
                summary["unreferenced_icc"]
                if _has(specs, "quarantine_unreferenced_icc") else 0
            ),
        },
    }

    executed = {
        "deleted": {"recipes": 0, "compensations": 0, "calibrations": 0, "passports": 0},
        "detached": {"measurement_sessions": 0, "evaluations": 0},
        "orphaned": {"signoffs": 0},
        "quarantined": {"icc_files": 0},
        "skipped": {"items": 0},
    }

    action_results = []
    for spec in ACTION_SPECS:
        selected = spec in specs
        items = details[spec["detail"]]
        if not selected or not items:
            continue
        action_results.append({
            "action": spec["action"],
            "category": spec["category"],
            "description": spec["description"],
            "count": len(items),
            "selected": selected,
            "status": "planned" if dry_run else "executed",
        })

    if not dry_run:
        _apply(details, specs, executed)

    result = {
        "generated_at": _now(),
        "dry_run": bool(dry_run),
        "applied": not dry_run,
        "summary": summary,
        "details": details,
        "actions": action_results,
        "planned": planned,
        "counts": executed if not dry_run else {
            "deleted": {"recipes": 0, "compensations": 0, "calibrations": 0, "passports": 0},
            "detached": {"measurement_sessions": 0, "evaluations": 0},
            "orphaned": {"signoffs": 0},
            "quarantined": {"icc_files": 0},
            "skipped": {"items": 0},
        },
    }
    if not dry_run:
        after = build_report()
        result["after"] = {
            "summary": after["summary"],
            "actions": [
                action for action in after["actions"] if action["count"] > 0
            ],
        }
    return result


def _has(specs: list[dict], action: str) -> bool:
    return any(spec["action"] == action for spec in specs)


def _apply(details: dict, specs: list[dict], executed: dict) -> None:
    selected = {spec["action"] for spec in specs}
    now = _now()

    if "detach_orphan_measurement_sessions" in selected:
        store = _store("measurements.json")
        for item in details["orphan_measurement_sessions"]:
            sid = item.get("id", "")
            doc = store.get(sid)
            if not doc or doc.get("process_id") != item.get("process_id"):
                executed["skipped"]["items"] += 1
                continue
            doc["process_id"] = ""
            doc["detached_from_process"] = item.get("process_id", "")
            doc["detached_at"] = now
            store.put(sid, doc)
            executed["detached"]["measurement_sessions"] += 1

    if "detach_orphan_evaluations" in selected:
        store = _store("evaluations.json")
        for item in details["orphan_evaluations"]:
            eid = item.get("id", "")
            doc = store.get(eid)
            if not doc or doc.get("process_id") != item.get("process_id"):
                executed["skipped"]["items"] += 1
                continue
            doc["process_id"] = ""
            doc["detached_from_process"] = item.get("process_id", "")
            doc["detached_at"] = now
            store.put(eid, doc)
            executed["detached"]["evaluations"] += 1

    if "mark_orphan_signoffs" in selected:
        store = _store("signoffs.json")
        for item in details["orphan_signoffs"]:
            sid = item.get("id", "")
            doc = store.get(sid)
            if (
                not doc
                or doc.get("process_id") != item.get("process_id")
                or doc.get("orphaned_at")
            ):
                executed["skipped"]["items"] += 1
                continue
            doc["orphaned_at"] = now
            doc["orphan_reason"] = ORPHAN_REASON_MISSING_PROCESS
            store.put(sid, doc)
            executed["orphaned"]["signoffs"] += 1

    if "delete_orphan_recipes" in selected:
        store = _store("recipes.json")
        for item in details["orphan_recipes"]:
            if store.delete(item.get("id", "")):
                executed["deleted"]["recipes"] += 1
            else:
                executed["skipped"]["items"] += 1

    if "delete_orphan_compensations" in selected:
        store = _store("compensations.json")
        for item in details["orphan_compensations"]:
            if store.delete(item.get("process_id", "")):
                executed["deleted"]["compensations"] += 1
            else:
                executed["skipped"]["items"] += 1

    if "delete_orphan_calibrations" in selected:
        store = _store("calibrations.json")
        for item in details["orphan_calibrations"]:
            if store.delete(item.get("id", "")):
                executed["deleted"]["calibrations"] += 1
            else:
                executed["skipped"]["items"] += 1

    if "delete_orphan_passports" in selected:
        store = _store("passports.json")
        for item in details["orphan_passports"]:
            pid = item.get("process_id", "")
            if store.delete(pid):
                executed["deleted"]["passports"] += int(item.get("versions", 0) or 0)
            else:
                executed["skipped"]["items"] += 1

    if "quarantine_unreferenced_icc" in selected:
        for item in details["unreferenced_icc"]:
            filename = item.get("filename", "")
            source = PROFILE_DIR / filename
            if not source.exists():
                executed["skipped"]["items"] += 1
                continue
            ORPHANED_ICC_DIR.mkdir(parents=True, exist_ok=True)
            target = _quarantine_target(filename)
            shutil.move(str(source), str(target))
            executed["quarantined"]["icc_files"] += 1
