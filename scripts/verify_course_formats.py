"""对真实测量导出文件做格式模板验证（只读，不入库）。

用法: python scripts/verify_course_formats.py <测量导出目录>
目录递归扫描 .txt，逐文件解析并报告：kind / 块数 / 密度列 / Lab↔XYZ 交叉校验状态。
退出码: 全部可解析=0，存在解析失败=1。不读取数值真值、不落库、不修改文件。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.cgats import parse_measurement_text, probe_lab_condition  # noqa: E402


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    root = Path(sys.argv[1])
    if not root.is_dir():
        print(f"目录不存在: {root}")
        return 2

    ok = fail = 0
    for p in sorted(root.rglob("*.txt")):
        try:
            parsed = parse_measurement_text(p.read_bytes())
            dens = sum(1 for pt in parsed.patches if pt.densities)
            pairs = sum(1 for pt in parsed.patches if pt.lab and pt.xyz)
            worst, statuses = 0.0, set()
            for pt in parsed.patches:
                if pt.lab and pt.xyz:
                    r = probe_lab_condition(pt.xyz, pt.lab)
                    worst = max(worst, r["best_de"])
                    statuses.add(r["status"])
            rel = p.relative_to(root)
            print(f"PASS {str(rel):28s} kind={parsed.kind:5s} patches={len(parsed.patches):3d} "
                  f"dens={dens:3d} pairs={pairs:3d} max_dE={worst:.2f} {sorted(statuses) if statuses else ''}")
            ok += 1
        except Exception as e:  # noqa: BLE001 —— 验证脚本需报告而非中断
            print(f"FAIL {p.name}: {e}")
            fail += 1
    print(f"\n{ok} passed, {fail} failed")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
