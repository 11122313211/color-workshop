"""一次性调试残留工艺清理脚本（已适配 V0.11 归档优先与完整性审计）。

注意：
- V0.11 起 DELETE /api/processes/{id} 仅为归档，不会删除 ICC；
- 对账未引用 ICC 时必须同时包含已归档工艺（include_archived=1），并优先使用
  ``scripts/audit_data.py --dry-run / --apply`` 做安全的隔离移动，而非直接 unlink。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

JUNK = {"E2E-SWOP", "冒烟工艺-SWOP", "dbg", "dbg2", "哈希1", "哈希2", "测试工艺-CMYK", "色域测试"}


def main() -> int:
    parser = argparse.ArgumentParser(description="清理调试残留工艺（默认仅预览）")
    parser.add_argument("--base-url", default="http://127.0.0.1:2354")
    parser.add_argument("--apply", action="store_true", help="显式执行归档并运行完整性修复")
    args = parser.parse_args()

    client = httpx.Client(base_url=args.base_url, timeout=15.0)
    procs = client.get("/api/processes", params={"include_archived": 1}).json()
    targets = [p for p in procs if p.get("name") in JUNK and not p.get("archived_at")]

    for p in targets:
        if args.apply:
            resp = client.post(
                f"/api/processes/{p['id']}/archive",
                json={"reason": "cleanup_script", "actor": "cleanup_data.py"},
            )
            resp.raise_for_status()
            print("archived:", p["name"], p["id"])
        else:
            print("[dry-run] would archive:", p["name"], p["id"])

    print("--- 剩余活跃工艺 ---")
    for p in client.get("/api/processes").json():
        print(" ", p["id"], p["name"])

    # 未引用 ICC 统一走 V0.11 完整性服务（自动包含已归档工艺引用，隔离而非直接删除）
    from app.services.integrity import repair

    report = repair(dry_run=not args.apply)
    print(
        "完整性检查:",
        f"issues={report['summary']['issues_total']}",
        f"unreferenced_icc={report['summary']['unreferenced_icc']}",
        f"applied={report['applied']}",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
