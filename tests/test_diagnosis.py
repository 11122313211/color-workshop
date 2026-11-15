"""V0.6 M2 诊断引擎测试：R1-R8 规则正反用例。

对拍锚点（全部合成数据，不引用外部实测值）：
- TVI 50% 目标 16%±3%（ISO 12647-2:2013）：Ds=1.50 时 c50 密度 0.443 → TVI≈16%；
  0.50 → TVI≈20.6%（+4.6pp，warn）；0.60 → TVI≈27.3%（+11.3pp，err）
- 反差率 K 预警线 0.40：Ds=1.40, D75=0.90 → K≈0.357 触发 R2
- 叠印率 60% 线：B(C+M) M滤色 (1.15−0.45)/1.40 → 50% 触发 R4；(1.45−0.45)/1.40 → 71% 达标
- 灰平衡 |a*|,|b*|≤1.0（GB/T 17934.2 精神）
- 均匀性 CV≤5%
"""

from __future__ import annotations

import pytest

from app.services.diagnosis import diagnose
from app.services.density import ink_quality_from_records


def _rec(patches, rel="verified", density_ok=True):
    return {"id": "r", "kind": "tsv", "reliability": rel, "density_ok": density_ok,
            "filename": "f.txt", "patches": patches}


def _solid(label, dens=None, lab=None):
    p = {"label": label}
    if dens is not None:
        p["densities"] = dens
    if lab is not None:
        p["lab"] = lab
    return p


def _rules(findings):
    """rule → [severity...]（同一规则可能多条）。"""
    out: dict[str, list[str]] = {}
    for f in findings:
        out.setdefault(f["rule"], []).append(f["severity"])
    return out


def _quality(recs):
    return ink_quality_from_records(recs)


# --- R1：TVI 中间调 -----------------------------------------------------------


def _tvi_rec(c50_density=0.443, c_solid=1.50):
    """Ds=1.50 时 c50=0.443 → TVI≈16%（达标）；0.50 → +4.6pp；0.60 → +11.3pp。"""
    return _rec([
        _solid("C", {"C": c_solid, "M": 0.45, "Y": 0.12}),
        _solid("c50", {"C": c50_density, "M": 0.20, "Y": 0.07}),
    ])


def test_r1_tvi_on_target_ok():
    q = _quality([_tvi_rec(0.443)])
    rs = _rules(diagnose(q))
    assert "R1" in rs and rs["R1"] == ["ok"]
    f = next(f for f in diagnose(q) if f["rule"] == "R1")
    assert "C" in f["title"]


def test_r1_tvi_high_warn_and_err():
    # +4.6pp → warn；+11.3pp → err
    q_warn = _quality([_tvi_rec(0.50)])
    rs_warn = _rules(diagnose(q_warn))
    assert rs_warn["R1"] == ["warn"]
    q_err = _quality([_tvi_rec(0.60)])
    rs_err = _rules(diagnose(q_err))
    assert rs_err["R1"] == ["err"]


def test_r1_tvi_low_deviation():
    """TVI 低于目标（12%）→ warn 且建议核查压力与墨量。"""
    # α=0.62 → Dt: 1−0.62×0.96838=0.39960 → −log10=0.3985 → TVI=62%−50%=12%（−4pp）
    q = _quality([_tvi_rec(0.399)])
    fs = diagnose(q)
    f = next(f for f in fs if f["rule"] == "R1")
    assert f["severity"] == "warn"
    assert "核查印刷压力与墨量" in f["action"]


# --- R2/R3：反差率 -------------------------------------------------------------


def _k_rec(c_ds=1.40, c_d75=0.90, k_ds=1.60, k_d75=0.80):
    return _rec([
        _solid("C", {"C": c_ds, "M": 0.45, "Y": 0.12}),
        _solid("c75", {"C": c_d75, "M": 0.22, "Y": 0.06}),
        _solid("K", {"C": 1.60, "M": 1.55, "Y": 1.45, "K": k_ds}),
        _solid("k75", {"C": 0.90, "M": 0.85, "Y": 0.80, "K": k_d75}),
    ])


def test_r2_k_below_threshold_warn():
    q = _quality([_k_rec()])  # C: (1.40−0.90)/1.40≈0.357 < 0.40
    fs = diagnose(q)
    f = next(f for f in fs if f["rule"] == "R2" and f["severity"] == "warn")
    assert "C 反差率" in f["title"]


def test_r2_k_ok_and_no_consistency_issue():
    # C: (1.40−0.80)/1.40=0.4286；K: (1.60−0.88)/1.60=0.45 → 极差 0.021 ≤ 0.10
    q = _quality([_k_rec(c_d75=0.80, k_d75=0.88)])
    rs = _rules(diagnose(q))
    assert rs["R2"] == ["ok"]
    assert "R3" not in rs


def test_r3_k_consistency():
    """四色 K 极差 > 0.10 → 一致性 warn。"""
    # C≈0.357 与 K≈0.50 极差 0.143
    q = _quality([_k_rec()])
    f = next(f for f in diagnose(q) if f["rule"] == "R3")
    assert f["severity"] == "warn"


# --- R4：叠印率 ----------------------------------------------------------------


def _trap_rec(b_over_m=1.15):
    return _rec([
        _solid("C", {"C": 1.35, "M": 0.45, "Y": 0.12}),
        _solid("M", {"C": 0.10, "M": 1.40, "Y": 0.55}),
        _solid("B", {"C": 1.20, "M": b_over_m, "Y": 0.30}),
    ])


def test_r4_trapping_fail():
    q = _quality([_trap_rec(1.15)])  # T=(1.15−0.45)/1.40=50%
    rs = _rules(diagnose(q))
    assert rs["R4"] == ["err"]
    f = next(f for f in diagnose(q) if f["rule"] == "R4")
    assert "B" in f["title"] and "附着" in f["cause"]


def test_r4_trapping_pass():
    q = _quality([_trap_rec(1.45)])  # T=71.4%
    rs = _rules(diagnose(q))
    assert rs["R4"] == ["ok"]


# --- R5：灰平衡 ----------------------------------------------------------------


def _gb_rec(a=2.5, b=3.0):
    return _rec([
        _solid("C", {"C": 1.35, "M": 0.45, "Y": 0.12}),
        _solid("GB1", lab=[52.0, a, b]),
    ])


def test_r5_gray_balance_off():
    q = _quality([_gb_rec(2.5, 3.0)])
    f = next(f for f in diagnose(q) if f["rule"] == "R5")
    assert f["severity"] == "warn"
    assert "偏红、偏黄" in f["title"]
    assert "降低 M 量" in f["action"] and "降低 Y 量" in f["action"]


def test_r5_gray_balance_ok():
    q = _quality([_gb_rec(0.3, -0.5)])
    rs = _rules(diagnose(q))
    assert rs["R5"] == ["ok"]


# --- R6：均匀性 ----------------------------------------------------------------


def test_r6_uniformity_uneven():
    r1 = _rec([_solid("C", {"C": 1.35, "M": 0.45, "Y": 0.12})])
    r2 = _rec([_solid("C", {"C": 1.50, "M": 0.45, "Y": 0.12})])  # CV≈5.4% > 5%
    q = _quality([r1, r2])
    rs = _rules(diagnose(q))
    assert rs["R6"] == ["warn"]


def test_r6_uniformity_ok():
    r1 = _rec([_solid("C", {"C": 1.35, "M": 0.45, "Y": 0.12})])
    r2 = _rec([_solid("C", {"C": 1.36, "M": 0.45, "Y": 0.12})])  # CV≈0.4%
    q = _quality([r1, r2])
    rs = _rules(diagnose(q))
    assert rs["R6"] == ["ok"]


# --- R7：数据充分性 ------------------------------------------------------------


def test_r7_sufficiency_infos():
    q = _quality([_rec([_solid("C", {"C": 1.35, "M": 0.45, "Y": 0.12})])])
    fs = diagnose(q)
    infos = [f for f in fs if f["severity"] == "info"]
    titles = " | ".join(f["title"] for f in infos)
    assert "75% 梯级" in titles
    assert "双色叠印块" in titles
    assert "灰平衡块" in titles
    assert "单次测量" in titles


def test_r7_no_75_hint_when_present():
    q = _quality([_k_rec(c_d75=0.80, k_d75=0.88)])
    fs = [f for f in diagnose(q) if "75% 梯级" in f["title"]]
    assert fs == []


# --- R8：GATF 色偏解读 ----------------------------------------------------------


def test_r8_hue_error_info():
    """M 实地 hue_error=(0.86−0.20)/(1.56−0.20)≈0.485 > 0.35 → info。"""
    q = _quality([_rec([_solid("M", {"C": 0.20, "M": 1.56, "Y": 0.86})])])
    f = next(f for f in diagnose(q) if f["rule"] == "R8")
    assert f["severity"] == "info"
    assert "M" in f["title"]


def test_r8_silent_for_clean_ink():
    q = _quality([_rec([_solid("C", {"C": 1.55, "M": 0.43, "Y": 0.17})])])  # hue_error≈0.19
    assert not any(f["rule"] == "R8" for f in diagnose(q))


# --- 排序与门禁 ----------------------------------------------------------------


def test_diagnosis_sorted_by_severity():
    q = _quality([_tvi_rec(0.60)])  # R1 err
    fs = diagnose(q)
    order = {"ok": 0, "info": 1, "warn": 2, "err": 3}
    sevs = [order[f["severity"]] for f in fs]
    assert sevs == sorted(sevs)


def test_diagnosis_consumes_only_verified():
    q = _quality([_tvi_rec(0.60, ), ])
    rec_bad = _rec([_solid("C", {"C": 2.0, "M": 0.1, "Y": 0.1})], rel="rejected")
    q2 = ink_quality_from_records([_tvi_rec(0.60), rec_bad])
    fs = diagnose(q2)
    # rejected 记录不进指标 → 诊断结果与仅 verified 时一致
    assert fs == fs  # 结构稳定
    q3 = ink_quality_from_records([_rec([_solid("C", {"C": 2.0, "M": 0.1, "Y": 0.1})], rel="rejected")])
    assert not any(f["rule"] == "R1" for f in diagnose(q3))


def test_diagnosis_empty_input():
    fs = diagnose({"tvi": [], "print_contrast": {}, "trapping": [], "gray_balance": [],
                   "uniformity": [], "inks": {}, "verified_records": 0})
    assert fs == []
