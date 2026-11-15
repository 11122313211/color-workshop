"""V0.6 M3 会话对比与趋势测试：Δ 表口径、趋势时间序列（合成数据）。

对拍锚点：
- delta = 主会话 A − 对比会话 B：K 0.45 vs 0.50 → −0.05；TVI 16% vs 13% → +3pp
- trend_for_process 按 created_at 升序，仅含绑定该工艺的会话
"""

from __future__ import annotations

import pytest

from app.services.density import ink_quality_from_records
from app.services.qc_compare import compare_sessions, trend_for_process


def _solid(label, dens=None, lab=None):
    p = {"label": label}
    if dens is not None:
        p["densities"] = dens
    if lab is not None:
        p["lab"] = lab
    return p


def _rec(patches):
    return {"id": "r", "kind": "tsv", "reliability": "verified", "density_ok": True,
            "filename": "f.txt", "patches": patches}


def _full_rec(c_d75=0.80, b_over_m=1.45):
    """完整合成会话记录：实地 + 50/75 梯级 + 叠印（达标口径）。"""
    return _rec([
        _solid("C", {"C": 1.40, "M": 0.45, "Y": 0.12}),
        _solid("M", {"C": 0.10, "M": 1.42, "Y": 0.55}),
        _solid("c50", {"C": 0.45, "M": 0.20, "Y": 0.07}),
        _solid("c75", {"C": c_d75, "M": 0.22, "Y": 0.06}),
        _solid("B", {"C": 1.20, "M": b_over_m, "Y": 0.30}),
        _solid("GB1", lab=[52.0, 0.3, -0.5]),
    ])


def _ink_of(rec):
    return ink_quality_from_records([rec])


def test_compare_deltas_sign_and_rounding():
    a = _ink_of(_full_rec(c_d75=0.80, b_over_m=1.45))
    b = _ink_of(_full_rec(c_d75=0.90, b_over_m=1.15))
    cmp = compare_sessions(a, b)
    d = cmp["deltas"]
    # K：A=(1.40−0.80)/1.40≈0.4286，B=(1.40−0.90)/1.40≈0.3571 → Δ≈+0.0714
    k_c = d["k"]["C"]
    assert k_c["a"] == pytest.approx(0.4286, abs=1e-3)
    assert k_c["delta"] == pytest.approx(0.0714, abs=1e-3)
    # 叠印率：A=(1.45−0.45)/1.42≈70.4%，B=(1.15−0.45)/1.42≈49.3% → Δ≈+21.1
    trap = d["trapping"]["B"]
    assert trap["a"] == pytest.approx(70.4, abs=0.1)
    assert trap["delta"] == pytest.approx(21.1, abs=0.1)
    # 缺一侧数据时 delta=None
    empty = _ink_of(_rec([_solid("C", {"C": 1.35, "M": 0.45, "Y": 0.12})]))
    cmp2 = compare_sessions(_ink_of(_full_rec()), empty)
    assert cmp2["deltas"]["trapping"]["B"]["delta"] is None
    assert cmp2["deltas"]["trapping"]["B"]["a"] is not None
    assert cmp2["deltas"]["trapping"]["B"]["b"] is None


def test_trend_for_process_orders_and_filters():
    class FakeStore:
        def __init__(self, sessions):
            self._sessions = sessions

        def list_sessions(self):
            return [s["doc"] for s in self._sessions]

        def records(self, sid):
            for s in self._sessions:
                if s["doc"]["id"] == sid:
                    return s["records"]
            return []

    sessions = [
        {"doc": {"id": "s2", "process_id": "p1", "created_at": "2026-01-02T09:00:00", "instrument": "B"},
         "records": [_full_rec(c_d75=0.75)]},
        {"doc": {"id": "s1", "process_id": "p1", "created_at": "2026-01-01T09:00:00", "instrument": "A"},
         "records": [_full_rec(c_d75=0.80)]},
        {"doc": {"id": "s3", "process_id": "p2", "created_at": "2026-01-03T09:00:00", "instrument": "C"},
         "records": [_full_rec()]},
    ]
    points = trend_for_process(FakeStore(sessions), "p1")
    assert [p["session_id"] for p in points] == ["s1", "s2"]  # 时间升序 + 工艺过滤
    ks = [p["k"]["C"] for p in points]
    assert ks[1] > ks[0]  # D75 降低 → K 升高（暗调层次改善方向）
    assert points[0]["trap_min"] == pytest.approx(70.4, abs=0.2)
    assert all(p["verified"] == 1 for p in points)


def test_trend_skips_sessions_without_process():
    class FakeStore:
        def list_sessions(self):
            return [{"id": "s1", "process_id": "", "created_at": "2026-01-01T09:00:00"}]

        def records(self, sid):
            return []

    assert trend_for_process(FakeStore(), "p1") == []
