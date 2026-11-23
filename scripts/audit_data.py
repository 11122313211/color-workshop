#!/usr/bin/env python
"""数据完整性审计 CLI（V0.11）。

默认 ``--dry-run``：只扫描并输出修复预览，不写任何文件。
显式 ``--apply`` 才执行修复（孤儿派生记录删除、测量/评价解绑、签样 orphaned、
未引用 ICC 隔离）。

用法::

    python scripts/audit_data.py              # dry-run（默认）
    python scripts/audit_data.py --dry-run
    python scripts/audit_data.py --apply

输出为单个 JSON 对象，便于流水线解析。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="色彩工坊数据完整性审计与修复")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="只输出报告与修复预览，不写盘（默认）",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="执行修复（谨慎：会删除孤儿派生记录并隔离未引用 ICC）",
    )
    args = parser.parse_args(argv)

    # 参数解析完成后再导入服务，避免误在导入阶段触发任何数据写入。
    from app.services.integrity import repair

    if args.apply:
        result = repair(dry_run=False)
    else:
        result = repair(dry_run=True)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
