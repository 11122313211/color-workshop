"""V0.10 幂等迁移：补默认字段并初始化护照库，不删除任何旧数据。

- processes.json：补 ``measurement_condition=""``；
- recipes.json：补 ``recipe_kind="device_values"`` 与 ``recipe_meta``；
- signoffs.json：快照补空的 ``passport_id`` / ``passport_hash`` /
  ``passport_version``，证据保持原样；
- passports.json：不存在时初始化为 ``{}``。

用法：``python scripts/migrate_v10.py [--dry-run]``
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.filedb import JsonStore  # noqa: E402

DEFAULT_DATA_DIR = Path(
    os.environ.get("COLORFORGE_DATA_DIR", str(ROOT / "data"))
)
RECIPE_META = {
    "unit": "percent",
    "basis": "device_channels",
    "note": "分色值（设备值），不是基础油墨调墨配方",
}


def migrate(data_dir: Path | str = DEFAULT_DATA_DIR, dry_run: bool = False) -> dict:
    data_dir = Path(data_dir)
    report = {
        "data_dir": str(data_dir),
        "processes_updated": 0,
        "recipes_updated": 0,
        "signoffs_updated": 0,
        "passports_initialized": False,
    }

    process_store = JsonStore(data_dir / "processes.json")
    for pid, doc in process_store.all().items():
        if isinstance(doc, dict) and "measurement_condition" not in doc:
            doc["measurement_condition"] = ""
            report["processes_updated"] += 1
            if not dry_run:
                process_store.put(pid, doc)

    recipe_store = JsonStore(data_dir / "recipes.json")
    for rid, doc in recipe_store.all().items():
        if not isinstance(doc, dict):
            continue
        changed = False
        if "recipe_kind" not in doc:
            doc["recipe_kind"] = "device_values"
            changed = True
        if "recipe_meta" not in doc:
            doc["recipe_meta"] = dict(RECIPE_META)
            changed = True
        if changed:
            report["recipes_updated"] += 1
            if not dry_run:
                recipe_store.put(rid, doc)

    signoff_store = JsonStore(data_dir / "signoffs.json")
    for sid, doc in signoff_store.all().items():
        if not isinstance(doc, dict):
            continue
        snapshot = doc.get("snapshot")
        if not isinstance(snapshot, dict):
            snapshot = {}
            doc["snapshot"] = snapshot
        changed = False
        for key, value in (
            ("passport_id", ""),
            ("passport_hash", ""),
            ("passport_version", None),
        ):
            if key not in snapshot:
                snapshot[key] = value
                changed = True
        if changed:
            report["signoffs_updated"] += 1
            if not dry_run:
                signoff_store.put(sid, doc)

    passports_path = data_dir / "passports.json"
    if not passports_path.exists():
        report["passports_initialized"] = True
        if not dry_run:
            passports_path.parent.mkdir(parents=True, exist_ok=True)
            passports_path.write_text("{}\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="色彩工坊 V0.10 幂等迁移")
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    report = migrate(args.data_dir, args.dry_run)
    mode = "预览" if args.dry_run else "完成"
    print(
        f"[{mode}] processes={report['processes_updated']} "
        f"recipes={report['recipes_updated']} "
        f"signoffs={report['signoffs_updated']} "
        f"passports_init={report['passports_initialized']} "
        f"data_dir={report['data_dir']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
