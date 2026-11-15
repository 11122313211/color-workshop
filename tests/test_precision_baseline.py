"""精度基线回归门禁（PRD §10.4 要求：算法精度回归纳入 CI）。

- 硬门禁：色域内目标 ΔE<3 且收敛；色域外目标不得虚假收敛
- 回归门禁：ΔE 不得超过基线 +0.15；各墨量分量漂移 ≤2 个百分点
- 基线文件 tests/baselines/precision_baseline.json 的改动必须出现在
  版本 diff 中并说明理由，防止「改断言凑通过」式回退。

标记 @pytest.mark.precision，算法重构 / Pillow·lcms2 升级前后必跑。
"""

import json
from pathlib import Path

import pytest

from app.services.recipe import predict_recipe

BASELINE_PATH = Path(__file__).resolve().parent / "baselines" / "precision_baseline.json"
DE_REGRESSION_TOLERANCE = 0.15  # ΔE 允许漂移
RECIPE_DRIFT_PCT = 2.0  # 墨量分量允许漂移（百分点）


def _load_baseline() -> dict:
    if not BASELINE_PATH.exists():
        pytest.skip("缺少精度基线文件（tests/baselines/precision_baseline.json）")
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))


@pytest.mark.precision
def test_precision_baseline(cmyk_icc):
    baseline = _load_baseline()
    failures: list[str] = []
    rows: list[str] = []

    for t in baseline["targets"]:
        r = predict_recipe(cmyk_icc, "CMYK", t["lab"], tolerance=2.0)
        row = (
            f"{t['label']:　<6} baseline ΔE={t['delta_e']:.3f} got ΔE={r['delta_e']:.3f} | "
            + " ".join(f"{k}={r['recipe'][k]:.1f}" for k in t["recipe"])
        )
        rows.append(row)

        if t["converged"]:
            # 硬门禁
            if r["delta_e"] >= 3.0 or not r["converged"]:
                failures.append(f"{t['label']}: 硬门禁失败 ΔE={r['delta_e']}")
            # 回归门禁
            if r["delta_e"] > t["delta_e"] + DE_REGRESSION_TOLERANCE:
                failures.append(
                    f"{t['label']}: ΔE 回归 {t['delta_e']:.3f} -> {r['delta_e']:.3f}"
                    f"（超过 +{DE_REGRESSION_TOLERANCE}）"
                )
        else:
            # 色域外：不虚假收敛，且偏差不得显著恶化
            if r["converged"]:
                failures.append(f"{t['label']}: 色域外目标虚假收敛！")
            if r["delta_e"] > t["delta_e"] + DE_REGRESSION_TOLERANCE:
                failures.append(
                    f"{t['label']}: 色域外 ΔE 回归 {t['delta_e']:.3f} -> {r['delta_e']:.3f}"
                )

        # 墨量分量漂移
        for ch, base_v in t["recipe"].items():
            got_v = r["recipe"].get(ch)
            if got_v is None:
                failures.append(f"{t['label']}: 缺少通道 {ch}")
                continue
            if abs(got_v - base_v) > RECIPE_DRIFT_PCT:
                failures.append(
                    f"{t['label']}: 通道 {ch} 漂移 {base_v:.1f} -> {got_v:.1f}"
                    f"（>{RECIPE_DRIFT_PCT}pp）"
                )

    print("精度基线对照：")
    for row in rows:
        print("  " + row)
    assert not failures, "精度回归门禁失败:\n" + "\n".join(failures)
    print(f"[PASS] 精度基线门禁（{len(baseline['targets'])} 个目标全部达标）")
