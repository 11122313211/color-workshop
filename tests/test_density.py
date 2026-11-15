"""M2 密度与油墨质量测试：GATF 公式手算对拍 + verified 门禁。

对拍基准（课程实验报告可复算数值，仅作公式正确性锚点）：
- C100 实地：Dc=1.55 Dm=0.43 Dy=0.17 → 色相误差 0.19、灰度 0.11、色效率 0.81、色纯度 0.89
- Murray–Davies：Dt=0.51, Ds=1.49 → α=0.714（对应 c60 名义 60% → TVI≈+11.4pp）
"""

from __future__ import annotations

import pytest

from app.services.density import (
    ink_metrics,
    ink_quality_from_records,
    murray_davies_dot_area,
    parse_ink_label,
    tvi_for_ink,
)


# ---------------------------------------------------------------------------
# 公式手算对拍
# ---------------------------------------------------------------------------


def test_ink_metrics_c100_handcheck():
    m = ink_metrics("C", {"C": 1.55, "M": 0.43, "Y": 0.17, "K": 0.78})
    assert m is not None
    assert m.strength == pytest.approx(1.55)
    assert m.hue_error == pytest.approx((0.43 - 0.17) / (1.55 - 0.17), abs=1e-3)
    assert m.hue_error == pytest.approx(0.19, abs=0.01)
    assert m.grayness == pytest.approx(0.17 / 1.55, abs=1e-3)
    assert m.efficiency == pytest.approx(1 - (0.43 + 0.17) / 2 / 1.55, abs=1e-3)
    assert m.purity == pytest.approx((1.55 - 0.17) / 1.55, abs=1e-3)


def test_ink_metrics_m_channel():
    """M 实地：DH=1.56, DM_mid=0.86, DL=0.20（课程 M100 组）。"""
    m = ink_metrics("M", {"C": 0.20, "M": 1.56, "Y": 0.86, "K": 0.60})
    assert m.strength == pytest.approx(1.56)
    assert m.hue_error == pytest.approx((0.86 - 0.20) / (1.56 - 0.20), abs=1e-3)
    assert m.grayness == pytest.approx(0.20 / 1.56, abs=1e-3)


def test_ink_metrics_invalid_inputs():
    assert ink_metrics("C", {"C": 0, "M": 0.1, "Y": 0.1}) is None  # 全零/负不参与
    assert ink_metrics("C", {}) is None
    m = ink_metrics("C", {"C": 0.1, "M": 0.1, "Y": 0.1})  # H==L 退化
    assert m is None


def test_murray_davies_handcheck():
    """课程 c60：Dt=0.51，Ds=1.49 → α=0.714。"""
    alpha = murray_davies_dot_area(0.51, 1.49)
    assert alpha == pytest.approx((1 - 10 ** (-0.51)) / (1 - 10 ** (-1.49)), abs=1e-4)
    assert alpha == pytest.approx(0.714, abs=0.002)
    # 实地密度下 α=1；纸白密度 Dt≈0 → α≈0
    assert murray_davies_dot_area(1.49, 1.49) == pytest.approx(1.0, abs=1e-6)
    assert murray_davies_dot_area(0.0, 1.49) == pytest.approx(0.0, abs=1e-6)
    with pytest.raises(ValueError):
        murray_davies_dot_area(0.5, 0.0)


def test_tvi_pairing():
    solid = {"_channel": "C", "densities": {"C": 1.49}}
    tints = [(60, {"C": 0.51}), (0, {"C": 0.0})]
    out = tvi_for_ink(solid, tints)
    assert len(out) == 2
    assert out[0]["nominal"] == 60
    assert out[0]["dot_area"] == pytest.approx(0.714, abs=0.002)
    assert out[0]["tvi"] == pytest.approx(0.714 - 0.60, abs=0.002)  # ≈ +11.4pp
    assert out[1]["tvi"] == pytest.approx(0.0, abs=1e-6)  # Dt=0 → α=0


def test_parse_ink_label():
    assert parse_ink_label("C") == ("C", None)
    assert parse_ink_label("c60") == ("C", 60)
    assert parse_ink_label("m30") == ("M", 30)
    assert parse_ink_label("y40") == ("Y", 40)
    assert parse_ink_label("w") == ("", None)
    assert parse_ink_label("10M10Y") == ("", None)


# ---------------------------------------------------------------------------
# verified 门禁（分级数据流向）
# ---------------------------------------------------------------------------


def _rec(rel, density_ok=True, patches=None):
    return {
        "id": "r1", "kind": "tsv", "reliability": rel, "density_ok": density_ok,
        "filename": f"{rel}.txt",
        "patches": patches if patches is not None else [
            {"label": "C", "densities": {"C": 1.55, "M": 0.43, "Y": 0.17, "K": 0.78}},
            {"label": "c60", "densities": {"C": 0.51, "M": 0.19, "Y": 0.06, "K": 0.34}},
        ],
    }


def test_quality_consumes_verified_only():
    result = ink_quality_from_records([_rec("verified")])
    assert result["verified_records"] == 1
    assert result["skipped_records"] == 0
    assert "C" in result["inks"]
    assert result["tvi"] and result["tvi"][0]["nominal"] == 60


def test_quality_skips_warning_and_rejected():
    result = ink_quality_from_records([_rec("warning"), _rec("rejected")])
    assert result["verified_records"] == 0
    assert result["skipped_records"] == 2
    assert result["inks"] == {}
    assert result["tvi"] == []
    assert result["source_records"] == []


def test_quality_skips_density_not_ok():
    """verified 但密度通道被判不可用 → 同样不进指标。"""
    result = ink_quality_from_records([_rec("verified", density_ok=False)])
    assert result["verified_records"] == 0
    assert result["inks"] == {}


def test_quality_mix_and_overprints():
    rec = _rec("verified", patches=[
        {"label": "C", "densities": {"C": 1.55, "M": 0.43, "Y": 0.17, "K": 0.78}},
        {"label": "M", "densities": {"C": 0.20, "M": 1.56, "Y": 0.86, "K": 0.60}},
        {"label": "Y", "densities": {"C": 0.02, "M": 0.10, "Y": 1.09, "K": 0.06}},
        {"label": "c60", "densities": {"C": 0.51, "M": 0.19, "Y": 0.06, "K": 0.34}},
        {"label": "w", "densities": {"C": 0.01, "M": 0.01, "Y": 0.02, "K": 0.01}},
    ])
    result = ink_quality_from_records([rec])
    assert set(result["inks"]) == {"C", "M", "Y"}
    assert result["inks"]["Y"]["strength"] == pytest.approx(1.09)
    assert result["tvi"][0]["ink"] == "C" and result["tvi"][0]["nominal"] == 60
    # w 不进叠印
    assert result["overprints"] == []
