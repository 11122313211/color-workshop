"""V0.7 印刷校准闭环测试。

覆盖：校准色块集完整性、校准版渲染、参考表（ISO 列仅固体+CMY）、双基准报告
（配对/分级/overall 判定）、校正曲线手算锚点（scaled-axis 构造：e(t)=0.8t）、
逐通道墨量建议方向、补偿模型写入与门禁、导出文本结构、API 全流程。
所有测量数据为合成（由 testbed ICC 正查构造，无非公开实测数据）。
"""

from __future__ import annotations

import numpy as np
import pytest

from app.services.calibration import (
    ISO_TOL_SOLID,
    WEDGE_DE_MAX,
    _axis_search,
    apply_suggestions,
    build_reference,
    calibration_report,
    paper_class,
)
from app.services.compensation import BIAS_CAP, MIN_N, CompensationStore
from app.services.measurement_qa import effective_lab
from app.services.recipe import _build_forward
from app.services.testform import CAL_INDEX_MAP, CAL_PATCHES, generate_calibration_form

pytestmark = pytest.mark.api

TSV_HEAD = "Color Name\tCIE Lab\t\t\tStatus T C\tM\tY\tK\r\n"


def _expected_labs(process_doc: dict) -> dict[str, list[float]]:
    ref = build_reference(process_doc)
    return {p["label"]: p["expected_lab"] for p in ref["patches"]}


def _tsv_from_labs(labs: dict[str, list[float]], use_p_index: bool = False) -> bytes:
    """合成测量 TSV：{label: lab} → Xrite530 风格（仅 Lab 列）。use_p_index 时用 P 编号。"""
    rows = []
    for i, (label, lab) in enumerate(labs.items(), 1):
        tag = f"P{i:02d}" if use_p_index else label
        rows.append(f"{tag}\t{lab[0]:.2f}\t{lab[1]:.2f}\t{lab[2]:.2f}")
    return (TSV_HEAD + "\r\n".join(rows) + "\r\n").encode("utf-8")


def _import_session(process_doc: dict, files: list[bytes], instrument="测试色差仪",
                    repeat_mode: str | None = None) -> dict:
    from app.services.measurements import MeasurementStore

    store = MeasurementStore()
    session = store.create_session(instrument, "M1", "2026-11-15T10:00:00", "黑",
                                   repeat_mode or ("repeat" if len(files) > 1 else "single"),
                                   process_doc["id"])
    for i, data in enumerate(files, 1):
        store.add_record(session["id"], f"r{i}.txt", data, process_doc)
    return store.get_session(session["id"])


# ---------------------------------------------------------------------------
# 色块集与校准版
# ---------------------------------------------------------------------------

def test_patch_set_integrity():
    assert len(CAL_PATCHES) == 28
    labels = [p["label"] for p in CAL_PATCHES]
    indexes = [p["index"] for p in CAL_PATCHES]
    assert len(set(labels)) == 28 and len(set(indexes)) == 28
    assert indexes == [f"P{i:02d}" for i in range(1, 29)]
    # 单色梯级标签与 density.parse_ink_label 兼容
    from app.services.density import parse_ink_label

    assert parse_ink_label("C100") == ("C", 100)
    assert parse_ink_label("k75")[0] == "K"
    # 双点配对映射：P 编号与标签等价
    assert CAL_INDEX_MAP["P01"] == "C100" and CAL_INDEX_MAP["P25"] == "W"


def test_calibration_form_render():
    form = generate_calibration_form("测试工艺", 300)
    assert form["pages"] == 1 and form["pairs_count"] == 28
    assert len(form["patches"]) == 28 and form["pdf_b64"] and form["tiff_b64"]
    assert form["preview_b64"].startswith("data:image/png;base64,")
    pair_labels = {p["label"] for p in form["pairs"]}
    assert pair_labels == {p["label"] for p in CAL_PATCHES}


def test_paper_class_mapping():
    assert paper_class("铜版纸") == "coated"
    assert paper_class("哑粉纸") == "coated"
    assert paper_class("胶版纸") == "uncoated"
    assert paper_class("特种纸") is None
    assert paper_class("") is None


# ---------------------------------------------------------------------------
# 参考表（M1）
# ---------------------------------------------------------------------------

def test_build_reference_iso_columns(process_factory):
    proc = process_factory("测试工艺", paper="铜版纸")
    ref = build_reference(proc)
    assert ref["paper_class"] == "coated" and ref["iso_available"]
    iso_rows = {p["label"]: p["iso_ref_lab"] for p in ref["patches"] if p["iso_ref_lab"]}
    assert set(iso_rows) == {"C100", "M100", "Y100", "K100", "CMY"}
    assert iso_rows["C100"] == [55.0, -37.0, -50.0]
    # 预期 Lab 与正向模型对拍
    forward = _build_forward(process_doc_bytes(proc), 4)
    for p in ref["patches"]:
        vals = np.asarray([p["device"]], dtype=np.float64) / 100.0
        assert p["expected_lab"] == [round(float(v), 2) for v in forward(vals)[0]]


def process_doc_bytes(proc: dict) -> bytes:
    from app.services.measurements import process_doc_icc_bytes

    return process_doc_icc_bytes(proc)


def test_build_reference_uncoated_iso_none(process_factory):
    proc = process_factory("非涂布工艺", paper="胶版纸")
    ref = build_reference(proc)
    assert ref["paper_class"] == "uncoated" and ref["iso_available"] is False
    assert all(p["iso_ref_lab"] is None for p in ref["patches"])


# ---------------------------------------------------------------------------
# 报告（M2）：配对 / 判定 / overall
# ---------------------------------------------------------------------------

def _jitter(labs: dict[str, list[float]], scale=0.05) -> dict[str, list[float]]:
    rng = np.random.default_rng(7)
    return {k: [v[0] + scale, v[1], v[2]] for k, v in labs.items()}


def test_report_ok_scenario_and_pairing(process_factory):
    """非涂布工艺（无 ISO 列）+ 微抖动实测 → overall ok；P 编号与标签在配对层等价。"""
    proc = process_factory("非涂布工艺", paper="胶版纸")
    exp = _expected_labs(proc)
    r1 = _tsv_from_labs(_jitter(exp), use_p_index=True)
    r2 = _tsv_from_labs(_jitter(exp), use_p_index=True)
    session = _import_session(proc, [r1, r2])
    assert session["stats"]["verified"] == 2

    report = calibration_report(proc, session)
    assert report["matched"] == 28 and report["overall"] == "ok"
    assert report["summary"]["wedge_max_ok"] and report["summary"]["wedge_avg_ok"]
    assert report["summary"]["solid_iso_fail"] == []
    row = next(p for p in report["patches"] if p["label"] == "C50")
    assert row["n"] == 2 and row["iso_ref_lab"] is None and row["pass_iso"] is None
    assert row["de_expected"] <= 1.0


def test_report_accepts_label_or_pindex(process_factory):
    """单记录场景：标签与 P 编号两种书写方式都能配对到校准色块。"""
    proc = process_factory("非涂布工艺", paper="胶版纸")
    exp = _expected_labs(proc)
    by_label = _import_session(proc, [_tsv_from_labs(_jitter(exp)),
                                      _tsv_from_labs(_jitter(exp))])
    by_index = _import_session(proc, [_tsv_from_labs(_jitter(exp), use_p_index=True),
                                      _tsv_from_labs(_jitter(exp), use_p_index=True)])
    rep_label = calibration_report(proc, by_label)
    rep_index = calibration_report(proc, by_index)
    assert rep_label["matched"] == rep_index["matched"] == 28
    assert rep_label["overall"] == rep_index["overall"] == "ok"


def test_report_needed_on_iso_fail(process_factory):
    """涂布工艺（testbed 与 ISO 固体参考不一致）→ 固体 ISO 超限 → needed。"""
    proc = process_factory("涂布工艺", paper="铜版纸")
    exp = _expected_labs(proc)
    session = _import_session(proc, [_tsv_from_labs(_jitter(exp)),
                                     _tsv_from_labs(_jitter(exp))])
    report = calibration_report(proc, session)
    assert report["overall"] == "needed"
    assert report["summary"]["solid_iso_fail"], "testbed 固体 Lab 与 ISO 12647-2 参考必然超限"
    solid_row = next(p for p in report["patches"] if p["label"] == "C100")
    assert solid_row["pass_iso"] is False
    assert solid_row["iso_tol"] == ISO_TOL_SOLID["C"]
    tint_row = next(p for p in report["patches"] if p["label"] == "C50")
    assert tint_row["iso_ref_lab"] is None and tint_row["pass_iso"] is None
    # ΔE 分量方向：实测 = 预期 + dL（+0.05）
    assert solid_row["comps"]["dL"] == pytest.approx(0.05, abs=0.02)


def test_report_na_few_matched(process_factory):
    proc = process_factory("测试工艺", paper="铜版纸")
    exp = _expected_labs(proc)
    partial = {k: v for k, v in list(exp.items())[:3]}
    session = _import_session(proc, [_tsv_from_labs(partial), _tsv_from_labs(partial)])
    report = calibration_report(proc, session)
    assert report["overall"] == "n/a" and report["matched"] == 3


def test_report_excludes_non_verified(process_factory):
    """rejected 记录不参与配对（可信度分级门禁）。"""
    proc = process_factory("测试工艺", paper="胶版纸")
    exp = _expected_labs(proc)
    session = _import_session(proc, [_tsv_from_labs(_jitter(exp)), _tsv_from_labs(_jitter(exp, 8.0))])
    # 手工重分级：偏差大的那条标记 rejected、正常那条 verified（模拟分级结果）
    recs = list(session["records"].values())
    recs[0]["reliability"] = "verified"
    recs[1]["reliability"] = "rejected"
    session["records"] = {r["id"]: r for r in recs}

    report = calibration_report(proc, session)
    assert report["matched"] == 28
    assert all(p["n"] == 1 for p in report["patches"])


# ---------------------------------------------------------------------------
# 校正曲线（手算锚点：measured = forward(0.8·t) → e(t)=0.8t → corrected=t/0.8）
# ---------------------------------------------------------------------------

def test_curve_scaled_axis_handcheck(process_factory):
    proc = process_factory("测试工艺", paper="胶版纸")
    forward = _build_forward(process_doc_bytes(proc), 4)
    exp = _expected_labs(proc)
    labs = {}
    for p in CAL_PATCHES:
        label = p["label"]
        dev = p["device"]
        if label.startswith("C") and label[1:].isdigit() and int(label[1:]) <= 75:
            t = int(label[1:])
            labs[label] = [round(float(v), 2)
                           for v in forward(np.asarray([[0.8 * t / 100.0, 0, 0, 0]]))[0]]
        else:
            labs[label] = [round(v[0] + 0.05, 2) for v in [exp[label]]] + exp[label][1:]
    session = _import_session(proc, [_tsv_from_labs(labs), _tsv_from_labs(labs)])
    report = calibration_report(proc, session)

    curve = {e["t"]: e for e in report["curves"]["C"]}
    for t, e_expect, c_expect in ((10, 8.0, 12.5), (25, 20.0, 31.25), (50, 40.0, 62.5)):
        assert curve[t]["effective"] == pytest.approx(e_expect, abs=0.6)
        assert curve[t]["corrected"] == pytest.approx(c_expect, abs=1.5)
    # 单调：校正曲线随名义网点递增
    corrected = [e["corrected"] for e in report["curves"]["C"]]
    assert corrected == sorted(corrected)
    # 端点固定：0→0、100→100（实地偏差交给墨量建议）
    assert curve[0.0]["corrected"] == 0.0 and curve[100.0]["corrected"] == 100.0
    # 无偏通道：曲线 ≈ 恒等（±1.5）
    for e in report["curves"]["M"]:
        assert e["corrected"] == pytest.approx(e["t"], abs=1.5)


def test_axis_search_nearest(process_factory):
    """轴反查基本行为：目标=轴上某设备值的 Lab → 反查回该值（±0.5%）。"""
    proc = process_factory("测试工艺", paper="胶版纸")
    forward = _build_forward(process_doc_bytes(proc), 4)
    target = forward(np.asarray([[0.62, 0, 0, 0]]))[0]
    v_opt, ach = _axis_search(forward, 0, target)
    assert v_opt == pytest.approx(62.0, abs=0.6)
    assert ach == pytest.approx(0.0, abs=0.6)


# ---------------------------------------------------------------------------
# 墨量建议方向与补偿写入
# ---------------------------------------------------------------------------

def test_suggestion_direction_dark_press(process_factory):
    """实测比预期更暗（K 固体 −5L）→ adjusted 偏亮 → 建议减墨（delta < 0）。"""
    proc = process_factory("测试工艺", paper="胶版纸")
    exp = _expected_labs(proc)
    labs = {k: list(v) for k, v in exp.items()}
    labs["K100"] = [exp["K100"][0] - 5.0, exp["K100"][1], exp["K100"][2]]
    session = _import_session(proc, [_tsv_from_labs(labs), _tsv_from_labs(labs)])
    report = calibration_report(proc, session)
    k_sug = next(s for s in report["ink_suggestions"] if s["channel"] == "K")
    assert k_sug["available"] and k_sug["delta_pct"] < 0
    assert k_sug["corrected_pct"] < 100.0
    assert abs(k_sug["delta_pct"]) <= BIAS_CAP * 100 + 0.05
    # 未偏离的通道建议 ≈ 0
    m_sug = next(s for s in report["ink_suggestions"] if s["channel"] == "M")
    assert abs(m_sug["delta_pct"]) <= 1.0


def test_missing_solid_suggestion_unavailable(process_factory):
    proc = process_factory("测试工艺", paper="胶版纸")
    exp = _expected_labs(proc)
    partial = {k: v for k, v in exp.items() if not k.startswith("Y")}
    session = _import_session(proc, [_tsv_from_labs(partial)])
    report = calibration_report(proc, session)
    y_sug = next(s for s in report["ink_suggestions"] if s["channel"] == "Y")
    assert y_sug["available"] is False and y_sug["delta_pct"] is None


def test_apply_writes_bias_and_gate(process_factory):
    proc = process_factory("测试工艺", paper="胶版纸")
    exp = _expected_labs(proc)
    labs = {k: list(v) for k, v in exp.items()}
    labs["K100"] = [exp["K100"][0] - 5.0, exp["K100"][1], exp["K100"][2]]
    session = _import_session(proc, [_tsv_from_labs(labs), _tsv_from_labs(labs)])
    report = calibration_report(proc, session)

    doc = apply_suggestions(proc, report)
    k_delta = next(s for s in report["ink_suggestions"] if s["channel"] == "K")["delta_pct"]
    assert doc["bias_device"][3] == pytest.approx(k_delta / 100.0, abs=1e-3)
    assert doc["n"] >= MIN_N and doc["history"][-1]["source"] == "calibration"
    # 应用门槛已越过 → 预测立即可用
    assert CompensationStore().bias_for_predict(proc["id"], proc["icc_sha256"]) == doc["bias_device"]
    # 再次应用：直接叠加（区别于 EMA）
    doc2 = apply_suggestions(proc, report)
    expected = float(np.clip(doc["bias_device"][3] * 2, -BIAS_CAP, BIAS_CAP))
    assert doc2["bias_device"][3] == pytest.approx(expected, abs=1e-3)


def test_apply_resets_on_sha_mismatch(process_factory):
    proc = process_factory("测试工艺", paper="胶版纸")
    exp = _expected_labs(proc)
    session = _import_session(proc, [_tsv_from_labs(_jitter(exp))])
    report = calibration_report(proc, session)
    apply_suggestions(proc, report)
    # ICC 替换（sha 变化）→ 补偿重置后写入新基线
    fake = {**proc, "icc_sha256": "deadbeefdeadbeef"}
    doc = apply_suggestions(fake, report)
    assert doc["icc_sha256"] == "deadbeefdeadbeef"


# ---------------------------------------------------------------------------
# 导出文本
# ---------------------------------------------------------------------------

def test_exports_structure(process_factory):
    proc = process_factory("测试工艺", paper="胶版纸")
    exp = _expected_labs(proc)
    session = _import_session(proc, [_tsv_from_labs(_jitter(exp)),
                                     _tsv_from_labs(_jitter(exp))])
    report = calibration_report(proc, session)
    exports = report["exports"]

    csv_lines = exports["curves_csv"].strip().splitlines()
    assert csv_lines[0] == "channel,t_nominal,t_effective,t_corrected"
    assert len(csv_lines) == 1 + 4 * 6  # 4 通道 × 6 档

    char_lines = exports["characterization_cgats"].strip().splitlines()
    assert "NUMBER_OF_FIELDS\t9" in char_lines
    assert "NUMBER_OF_SAMPLES\t28" in char_lines
    assert "CMYK_C\tCMYK_M\tCMYK_Y\tCMYK_K" in char_lines[3]
    c100 = next(line for line in char_lines if "\tC100\t" in line).split("\t")
    assert [float(c100[2]), float(c100[3]), float(c100[4]), float(c100[5])] == [100.0, 0.0, 0.0, 0.0]

    corrected = exports["corrected_cgats"].strip().splitlines()
    c75 = next(line for line in corrected if "\tC75\t" in line).split("\t")
    curve_c75 = next(e for e in report["curves"]["C"] if e["t"] == 75.0)
    assert float(c75[2]) == pytest.approx(curve_c75["corrected"], abs=0.2)
    assert len(exports["corrected_devices"]) == 28


# ---------------------------------------------------------------------------
# API 全流程
# ---------------------------------------------------------------------------

def test_api_calibration_form_and_guards(api_client, process_factory, rgb_icc):
    proc = process_factory("校准工艺", paper="铜版纸")
    r = api_client.post("/api/qc/calibration/form", params={"process_id": proc["id"], "dpi": 300})
    assert r.status_code == 200
    body = r.json()
    assert body["pages"] == 1 and len(body["reference"]["patches"]) == 28

    rgb_proc = process_factory("显示工艺", icc=rgb_icc)
    r = api_client.post("/api/qc/calibration/form", params={"process_id": rgb_proc["id"]})
    assert r.status_code == 422
    r = api_client.post("/api/qc/calibration/form", params={"process_id": "nope"})
    assert r.status_code == 404
    r = api_client.get("/api/qc/calibration", params={"session_id": "nope"})
    assert r.status_code == 404


def test_api_calibration_full_flow(api_client, process_factory):
    proc = process_factory("全流程工艺", paper="胶版纸")
    form = api_client.post("/api/qc/calibration/form",
                           params={"process_id": proc["id"], "dpi": 300}).json()
    exp = {p["label"]: p["expected_lab"] for p in form["reference"]["patches"]}
    tsv = _tsv_from_labs(_jitter(exp))
    r = api_client.post("/api/qc/measurements/import",
                        params={"filename": "cal.txt", "instrument": "色差仪", "condition": "M1",
                                "calibrated_at": "2026-11-15T10:00:00", "repeat_mode": "single",
                                "process_id": proc["id"]},
                        content=tsv)
    assert r.status_code == 200
    sid = r.json()["session"]["id"]
    # 第二遍复测导入（声明 repeat → 会话整体重分级为 verified）
    r = api_client.post("/api/qc/measurements/import",
                        params={"filename": "cal2.txt", "session_id": sid, "repeat_mode": "repeat"},
                        content=tsv)
    assert r.status_code == 200

    r = api_client.get("/api/qc/calibration", params={"session_id": sid})
    assert r.status_code == 200
    report = r.json()
    assert report["overall"] == "ok" and report["matched"] == 28
    assert report["exports"]["curves_csv"]

    r = api_client.post("/api/qc/calibration/apply", params={"session_id": sid})
    assert r.status_code == 200
    body = r.json()
    assert body["applied"] and body["n"] >= MIN_N and len(body["bias_device"]) == 4

    # 未绑定工艺的会话 → 422
    from app.services.measurements import MeasurementStore

    sid2 = MeasurementStore().create_session("裸会话", "M1", "t", "", "single")["id"]
    r = api_client.get("/api/qc/calibration", params={"session_id": sid2})
    assert r.status_code == 422
