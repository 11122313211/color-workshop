"""V0.8 校准轮次历史与收敛判定测试。

覆盖：CalibrationStore CRUD（幂等/级联/sha 过滤）、compare_rounds 判定锚点
（等级序 + 噪声地板 ±0.05）、convergence_for_process（converged/improving/
worsening/flat/轮次不足）、API 全流程（apply→history→新会话报告带
prev_round_compare→再 apply verdict=improved）。测量数据全部合成。
"""

from __future__ import annotations

import pytest

from app.services.calibration import apply_suggestions, calibration_report
from app.services.calibration_history import (
    CalibrationStore,
    compare_rounds,
    convergence_for_process,
    trend_series,
)
from tests.test_calibration import (
    _expected_labs,
    _import_session,
    _jitter,
    _tsv_from_labs,
)

pytestmark = pytest.mark.api


def _report_for(proc, dark_k=False):
    exp = _expected_labs(proc)
    labs = {k: list(v) for k, v in exp.items()}
    if dark_k:
        labs["K100"] = [exp["K100"][0] - 6.0, exp["K100"][1], exp["K100"][2]]
    session = _import_session(proc, [_tsv_from_labs(labs), _tsv_from_labs(labs)])
    return calibration_report(proc, session)


# ---------------------------------------------------------------------------
# 存储
# ---------------------------------------------------------------------------

def test_save_round_idempotent_per_session(process_factory):
    proc = process_factory("测试工艺", paper="胶版纸")
    report = _report_for(proc)
    store = CalibrationStore()
    bias = [0.0, 0.0, 0.0, -0.05]
    r1 = store.save_round(proc["id"], proc["icc_sha256"], "sess_x", report, [], bias)
    r2 = store.save_round(proc["id"], proc["icc_sha256"], "sess_x", report, [], bias)
    assert r1["id"] == r2["id"]  # 幂等：同 session 更新不新增
    assert len(store.list_for_process(proc["id"])) == 1
    assert r1["bias_after"] == pytest.approx(bias, abs=1e-6)
    assert r1["overall"] == report["overall"]


def test_list_filter_and_cascade(process_factory):
    proc = process_factory("测试工艺", paper="胶版纸")
    report = _report_for(proc)
    store = CalibrationStore()
    store.save_round(proc["id"], proc["icc_sha256"], "s1", report, [], [])
    store.save_round(proc["id"], proc["icc_sha256"], "s2", report, [], [])
    assert len(store.list_for_process(proc["id"])) == 2
    # 换 ICC（sha 变化）：轮次保留，但按新 sha 过滤后为空
    assert store.list_for_process(proc["id"], "deadbeefdeadbeef") == []
    assert store.latest_for_process(proc["id"])["session_id"] in ("s1", "s2")
    assert store.remove_process(proc["id"]) == 2
    assert store.list_for_process(proc["id"]) == []


# ---------------------------------------------------------------------------
# 判定
# ---------------------------------------------------------------------------

def _round(overall, max_de, deltas=None, rid="r", ts="2026-01-01T00:00:00"):
    d = deltas if deltas is not None else {"C": 0.0, "M": 0.0, "Y": 0.0, "K": -10.0}
    return {
        "id": rid, "created_at": ts, "overall": overall,
        "summary": {"max_de_expected": max_de, "avg_de_expected": round(max_de / 2, 2)},
        "ink_suggestions": [{"channel": ch, "delta_pct": d} for ch, d in d.items()],
    }


def test_compare_rounds_verdicts():
    cur_ok = {"overall": "ok", "summary": {"max_de_expected": 1.0, "avg_de_expected": 0.5},
              "ink_suggestions": [{"channel": "K", "delta_pct": -2.0}]}
    # 等级升（needed→ok）→ improved
    assert compare_rounds(cur_ok, _round("needed", 6.0))["verdict"] == "improved"
    # max ΔE 显著下降 → improved
    assert compare_rounds(cur_ok, _round("ok", 4.0))["verdict"] == "improved"
    # 噪声地板内 → flat
    assert compare_rounds(cur_ok, _round("ok", 1.03))["verdict"] == "flat"
    # 显著上升 → worsened
    assert compare_rounds({"overall": "ok", "summary": {"max_de_expected": 3.0, "avg_de_expected": 1.0},
                           "ink_suggestions": []}, _round("ok", 1.0))["verdict"] == "worsened"
    # 等级降 → worsened
    assert compare_rounds({"overall": "needed", "summary": {"max_de_expected": 6.0, "avg_de_expected": 3.0},
                           "ink_suggestions": []}, _round("ok", 6.0))["verdict"] == "worsened"


def test_compare_rounds_per_channel():
    cur = {"overall": "ok", "summary": {"max_de_expected": 1.0, "avg_de_expected": 0.5},
           "ink_suggestions": [{"channel": "K", "delta_pct": -2.0}]}
    cmp = compare_rounds(cur, _round("ok", 1.0, deltas={"C": 0.0, "K": -10.0}))
    k = next(x for x in cmp["per_channel"] if x["channel"] == "K")
    assert k["prev_delta"] == -10.0 and k["cur_delta"] == -2.0 and k["d"] == 8.0
    c = next(x for x in cmp["per_channel"] if x["channel"] == "C")
    assert c["cur_delta"] is None and c["d"] is None  # 当前报告缺该通道


def test_convergence_for_process():
    assert convergence_for_process([_round("ok", 1.0)])["status"] == "n/a"
    conv = convergence_for_process([_round("needed", 6.0, ts="2026-01-01"),
                                    _round("ok", 1.5, ts="2026-01-02")])
    assert conv["status"] == "converged" and conv["rounds"] == 2
    imp = convergence_for_process([_round("needed", 6.0, ts="2026-01-01"),
                                   _round("needed", 4.0, ts="2026-01-02")])
    assert imp["status"] == "improving" and imp["trend"] == [6.0, 4.0]
    wor = convergence_for_process([_round("needed", 2.0, ts="2026-01-01"),
                                   _round("needed", 5.0, ts="2026-01-02")])
    assert wor["status"] == "worsening"
    flat = convergence_for_process([_round("needed", 4.0, ts="2026-01-01"),
                                    _round("needed", 4.03, ts="2026-01-02")])
    assert flat["status"] == "flat"
    assert trend_series([_round("needed", 6.0, ts="2026-01-02"),
                         _round("needed", 4.0, ts="2026-01-01")]) == [4.0, 6.0]


def test_convergence_same_second_uses_seq():
    """同秒落库（created_at 相同）必须按 seq 判先后，不能把新轮当旧轮。"""
    old = {**_round("needed", 0.42), "seq": 1, "created_at": "2026-01-01T00:00:00"}
    new = {**_round("needed", 0.13), "seq": 2, "created_at": "2026-01-01T00:00:00"}
    conv = convergence_for_process([new, old])  # 传入顺序为倒序
    assert conv["status"] == "improving"
    assert conv["trend"] == [0.42, 0.13]
    assert trend_series([new, old]) == [0.42, 0.13]


# ---------------------------------------------------------------------------
# API 全流程（apply 落库 → history → 新会话报告带对比 → 再 apply）
# ---------------------------------------------------------------------------

def test_api_calibration_history_flow(api_client, process_factory):
    proc = process_factory("收敛工艺", paper="胶版纸")
    exp = _expected_labs(proc)

    def import_session(shift_k):
        labs = {k: list(v) for k, v in exp.items()}
        if shift_k:
            labs["K100"] = [exp["K100"][0] - shift_k, exp["K100"][1], exp["K100"][2]]
        tsv = _tsv_from_labs(labs)
        r = api_client.post("/api/qc/measurements/import",
                            params={"filename": "h1.txt", "instrument": "色差仪", "condition": "M1",
                                    "calibrated_at": "2026-01-01T10:00:00", "repeat_mode": "single",
                                    "process_id": proc["id"]}, content=tsv)
        sid = r.json()["session"]["id"]
        api_client.post("/api/qc/measurements/import",
                        params={"filename": "h2.txt", "session_id": sid, "repeat_mode": "repeat"},
                        content=tsv)
        return sid

    # 轮次 1：K 明显偏暗 → apply 落库
    sid1 = import_session(6.0)
    r = api_client.post("/api/qc/calibration/apply", params={"session_id": sid1})
    assert r.status_code == 200
    body = r.json()
    assert body["round_no"] == 1 and body["round_id"] and body["verdict"] is None

    hist = api_client.get("/api/qc/calibration/history", params={"process_id": proc["id"]}).json()
    assert hist["round_no"] == 1 and len(hist["rounds"]) == 1
    assert hist["convergence"]["status"] == "n/a" and hist["trend"]

    # 会话 2：改善（K 偏差减半）→ 报告自带 prev_round_compare → apply verdict=improved
    sid2 = import_session(3.0)
    rep = api_client.get("/api/qc/calibration", params={"session_id": sid2}).json()
    assert rep["prev_round_compare"]["verdict"] == "improved"
    assert rep["prev_round_compare"]["prev_max_de_expected"] > rep["summary"]["max_de_expected"]
    r = api_client.post("/api/qc/calibration/apply", params={"session_id": sid2})
    body2 = r.json()
    assert body2["round_no"] == 2 and body2["verdict"]["verdict"] == "improved"

    hist = api_client.get("/api/qc/calibration/history", params={"process_id": proc["id"]}).json()
    assert hist["round_no"] == 2 and hist["rounds"][0]["session_id"] == sid2
    assert hist["convergence"]["rounds"] == 2

    # V0.11：归档后历史仍可读；永久清理才删除轮次
    assert api_client.delete(f"/api/processes/{proc['id']}").status_code == 200
    hist = api_client.get("/api/qc/calibration/history", params={"process_id": proc["id"]})
    assert hist.status_code == 200
    assert hist.json()["round_no"] == 2
    assert len(CalibrationStore().list_for_process(proc["id"])) == 2

    permanent = api_client.delete(
        f"/api/processes/{proc['id']}/permanent",
        params={"confirm": proc["id"]},
    )
    assert permanent.status_code == 200
    hist = api_client.get("/api/qc/calibration/history", params={"process_id": proc["id"]})
    assert hist.status_code == 404
    assert CalibrationStore().list_for_process(proc["id"]) == []


def test_apply_suggestions_and_round_snapshot_consistency(process_factory):
    """落库轮次的建议快照与报告一致（防止后续 report 重算漂移）。"""
    proc = process_factory("快照工艺", paper="胶版纸")
    report = _report_for(proc, dark_k=True)
    doc = apply_suggestions(proc, report)
    store = CalibrationStore()
    rnd = store.save_round(proc["id"], proc["icc_sha256"], "sess_snap", report,
                           [0.0] * 4, doc["bias_device"])
    assert rnd["overall"] == report["overall"]
    assert rnd["summary"]["max_de_expected"] == report["summary"]["max_de_expected"]
    assert [s["delta_pct"] for s in rnd["ink_suggestions"]] == \
        [s["delta_pct"] for s in report["ink_suggestions"]]
