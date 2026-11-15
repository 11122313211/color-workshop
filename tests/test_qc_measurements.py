"""QC 测量可靠性闭环测试（V0.5 M1）。

覆盖：CGATS.17 / Xrite530 TSV 解析（编码容错、结构变体）、CIE 积分白点自检、
五条可靠性规则（正反用例）、导入 API 全流程（含 rejected 场景与补偿无关的门禁语义）。
fixtures 全部为合成数据（课程真实导出仅作格式模板，不入库）。
"""

from __future__ import annotations

import numpy as np
import pytest

from app.services.cgats import (
    CgatsParseError,
    integrate_lab,
    lab_from_xyz,
    parse_measurement_text,
    white_xyz,
    xyz_from_spectral,
)
from app.services.measurement_qa import grade_session

pytestmark = pytest.mark.api

# ---------------------------------------------------------------------------
# 合成测量文件构造器（课程格式的结构模板，数值为合成）
# ---------------------------------------------------------------------------

SPECTRAL_FIELDS = "\t".join(["SampleID", "SAMPLE_NAME"] + [f"nm{w}" for w in range(380, 731, 10)])


def _spectral_row(label: str, refl: list[float]) -> str:
    return "\t".join([label.split("-")[0][-3:], label] + [f"{v:.4f}" for v in refl])


def make_spectral_cgats(rows: dict[str, list[float]], sets: int | None = None, condition=True) -> bytes:
    """rows: {label: 36 点反射率}。"""
    n = len(rows)
    header = [
        'INSTRUMENTATION\t"Eye-One Pro"',
        'MEASUREMENT_SOURCE\t"WhiteBase=Absolute  Filter=No"',
    ]
    if condition:
        header += ['ILLUMINATION_NAME\t"D50"', 'OBSERVER_ANGLE\t"2"']
    header += ['KEYWORD\t"SampleID"', 'KEYWORD\t"SAMPLE_NAME"',
               f"NUMBER_OF_FIELDS\t{2 + 36}", "BEGIN_DATA_FORMAT",
               SPECTRAL_FIELDS, "END_DATA_FORMAT",
               f"NUMBER_OF_SETS\t{sets if sets is not None else n}", "BEGIN_DATA"]
    body = [f"1\t{label}\t" + "\t".join(f"{v:.4f}" for v in vals) for label, vals in rows.items()]
    return ("\n".join(header + body + ["END_DATA"]) + "\n").encode("utf-8")


def _smooth_refl(t: float) -> list[float]:
    """平滑反射率曲线（t∈[0,1] 控制整体深浅），36 点 380-730nm。"""
    w = np.arange(380, 731, 10)
    base = 0.05 + 0.85 / (1 + np.exp(-(w - 520) / 40))  # S 型曲线
    return list(base * (0.15 + 0.85 * (1 - t)))


def make_xyzlab_cgats(rows: list[tuple[str, list[float], list[float]]], condition=True) -> bytes:
    """rows: (label, xyz, lab)。"""
    fields = ["SampleID", "SAMPLE_NAME", "XYZ_X", "XYZ_Y", "XYZ_Z", "LAB_L", "LAB_A", "LAB_B"]
    header = [
        'INSTRUMENTATION\t"Eye-One Pro"',
        'MEASUREMENT_SOURCE\t"Illumination=D50  ObserverAngle=2°  WhiteBase=Absolute  Filter=No"',
    ]
    if condition:
        header += ['ILLUMINATION_NAME\t"D50"', 'OBSERVER_ANGLE\t"2"']
    header += ["NUMBER_OF_FIELDS\t8", "BEGIN_DATA_FORMAT", "\t".join(fields), "END_DATA_FORMAT",
               f"NUMBER_OF_SETS\t{len(rows)}", "BEGIN_DATA"]
    body = ["\t".join([str(i + 1), lb,
                       f"{xyz[0]:.4f}", f"{xyz[1]:.4f}", f"{xyz[2]:.4f}",
                       f"{lab[0]:.4f}", f"{lab[1]:.4f}", f"{lab[2]:.4f}"])
            for i, (lb, xyz, lab) in enumerate(rows)]
    return ("\n".join(header + body + ["END_DATA"]) + "\n").encode("utf-8")


def make_tsv(rows: list[tuple[str, list[float], list[float], list[float]]]) -> bytes:
    """rows: (label, lab, xyz, densities[C,M,Y,K])。"""
    head = "Color Name\tCIE Lab\t\t\tCIE XYZ\t\t\tLab LCh\t\t\tStatus T C\tM\tY\tK\r\n"
    body = "".join(
        f"{lb}\t{lab[0]:.2f}\t{lab[1]:.2f}\t{lab[2]:.2f}\t"
        f"{xyz[0]:.2f}\t{xyz[1]:.2f}\t{xyz[2]:.2f}\t"
        f"{np.hypot(lab[1], lab[2]):.2f}\t90.00\t210.00\t"
        f"{den[0]:.2f}\t{den[1]:.2f}\t{den[2]:.2f}\t{den[3]:.2f}\r\n"
        for lb, lab, xyz, den in rows
    )
    return (head + body).encode("utf-8")


def _consistent_xyzlab(n: int = 6, shift: list[float] | None = None) -> list[tuple[str, list[float], list[float]]]:
    """构造 Lab↔XYZ 在 D50/2° 下自洽的行（近中性轴、物理合理）；shift 可注入转录级偏差。"""
    white = np.asarray(white_xyz("D50", "2"))
    rng = np.random.default_rng(42)
    rows = []
    for i in range(n):
        s = 0.35 + 0.55 * rng.random()  # 中性灰深浅
        xyz = white * s * (1 + 0.08 * (rng.random(3) - 0.5))
        lab = lab_from_xyz(xyz, white)
        if shift is not None:
            lab = [lab[0] + shift[0], lab[1] + shift[1], lab[2] + shift[2]]
        rows.append((f"A{i + 1}", [float(v) for v in xyz], [float(v) for v in lab]))
    return rows


# ---------------------------------------------------------------------------
# 解析器
# ---------------------------------------------------------------------------


class TestCgatsParser:
    def test_spectral_variant(self):
        rows = {f"A{i + 1}": _smooth_refl(i / 5) for i in range(6)}
        parsed = parse_measurement_text(make_spectral_cgats(rows))
        assert parsed.kind == "cgats"
        assert parsed.source_condition == "D50/2°"
        assert len(parsed.patches) == 6
        p0 = parsed.patches[0]
        assert set(p0.spectral) == set(range(380, 731, 10))
        assert p0.lab is None and p0.lab_integrated is not None
        assert -2 < p0.lab_integrated[1] < 30  # S 型曲线积分出合理 a*

    def test_xyzlab_variant_and_encoding(self):
        rows = _consistent_xyzlab()
        raw = make_xyzlab_cgats(rows).decode("utf-8").replace("D50", "D50").encode("cp1252")
        parsed = parse_measurement_text(raw)
        assert parsed.meta.get("_encoding", parsed.meta.get("_encoding")) in ("cp1252", "utf-8")
        assert parsed.patches[0].xyz[1] > 0
        assert parsed.patches[0].lab[0] > 0

    def test_tsv_variant_with_densities(self):
        rows = [
            ("c60", [73.69, -25.44, -35.77], [41.39, 46.23, 30.70], [0.51, 0.19, 0.06, 0.34]),
            ("C", [47.98, -54.25, -65.89], [9.55, 16.78, 24.32], [1.55, 0.43, 0.17, 0.78]),
            ("w", [99.36, -0.28, 1.26], [107.85, 98.36, 34.32], [0.01, 0.01, 0.02, 0.01]),
        ]
        parsed = parse_measurement_text(make_tsv(rows))
        assert parsed.kind == "tsv"
        assert parsed.patches[1].densities == {"C": 1.55, "M": 0.43, "Y": 0.17, "K": 0.78}
        assert parsed.patches[0].lab[0] == pytest.approx(73.69)
        assert parsed.patches[2].xyz[1] == pytest.approx(98.36)

    def test_unrecognized_structure(self):
        with pytest.raises(CgatsParseError):
            parse_measurement_text("随便写两行\n没有任何结构\n".encode("utf-8"))

    def test_row_length_mismatch_noted_and_skipped(self):
        rows = _consistent_xyzlab(3)
        raw = make_xyzlab_cgats(rows).decode("utf-8")
        broken = raw.replace("END_DATA\n", "1\tA1\tbroken\trow\nEND_DATA\n", 1)
        parsed = parse_measurement_text(broken.encode("utf-8"))
        assert any("字段数不符" in n for n in parsed.notes)
        assert len(parsed.patches) == 3  # 坏行被跳过，好行保留

    def test_white_self_check(self):
        d50 = white_xyz("D50", "2")
        assert d50[0] == pytest.approx(96.42, abs=0.15)
        assert d50[1] == pytest.approx(100.0, abs=0.05)
        assert d50[2] == pytest.approx(82.49, abs=0.15)
        d65 = white_xyz("D65", "2")
        assert d65[0] == pytest.approx(95.05, abs=0.15)
        assert d65[2] == pytest.approx(108.90, abs=0.2)

    def test_spectral_roundtrip(self):
        refl = _smooth_refl(0.5)
        xyz = xyz_from_spectral(dict(zip(range(380, 731, 10), refl)))
        lab = integrate_lab(dict(zip(range(380, 731, 10), refl)))
        assert lab == lab_from_xyz(xyz, white_xyz("D50", "2"))


# ---------------------------------------------------------------------------
# 五条可靠性规则
# ---------------------------------------------------------------------------


def _rec(patches: list[dict], kind: str = "cgats", condition: str = "D50/2°") -> dict:
    return {
        "id": "r", "kind": kind, "source_condition": condition,
        "patches": patches, "reliability": "verified", "density_ok": True, "reasons": [],
    }


def test_rule3_crosscheck_consistent_verified():
    """自洽 Lab↔XYZ + 两条同标签复测记录 → verified。"""
    session = {"repeat_mode": "repeat"}
    patches = [
        {"label": lb, "lab": lab, "xyz": xyz, "spectral": None, "densities": None, "lab_integrated": None}
        for lb, xyz, lab in _consistent_xyzlab(6)
    ]
    recs = [_rec(patches), _rec(patches)]
    grade_session(session, recs)
    assert all(r["reliability"] == "verified" for r in recs)
    assert recs[0]["density_ok"] is True


def test_rule3_crosscheck_rejected_on_transcription_error():
    session = {"repeat_mode": "repeat"}
    recs = [_rec([
        {"label": lb, "lab": [lab[0] + 25, lab[1] + 30, lab[2] - 40], "xyz": xyz,
         "spectral": None, "densities": None, "lab_integrated": None}
        for lb, xyz, lab in _consistent_xyzlab(6)
    ])]
    grade_session(session, recs)
    assert recs[0]["reliability"] == "rejected"
    assert any("交叉校验失败" in r for r in recs[0]["reasons"])


def test_rule3_tsv_lab_channel_suspect_but_density_ok():
    """TSV：Lab 通道失配 → warning；密度通道独立生效。"""
    session = {"repeat_mode": "repeat"}
    patches = [
        {"label": "C", "lab": [47.98, -54.25, -65.89], "xyz": [9.55, 16.78, 24.32],
         "densities": {"C": 1.55, "M": 0.43, "Y": 0.17, "K": 0.78}},
        {"label": "w", "lab": [99.36, -0.28, 1.26], "xyz": [107.85, 98.36, 34.32],
         "densities": {"C": 0.01, "M": 0.01, "Y": 0.02, "K": 0.01}},
    ]
    recs = [_rec(patches, kind="tsv")]
    grade_session(session, recs)
    assert recs[0]["reliability"] == "warning"
    assert recs[0]["density_ok"] is True
    assert any("Lab 通道存疑" in r for r in recs[0]["reasons"])
    assert any("密度通道独立生效" in r for r in recs[0]["reasons"])


def test_rule1_single_measurement_warns():
    session = {"repeat_mode": "single"}
    recs = [_rec([{"label": "A1", "lab": [50, 0, 0], "xyz": None} for _ in range(1)])]
    grade_session(session, recs)
    assert recs[0]["reliability"] == "warning"
    assert any("单次测量" in r for r in recs[0]["reasons"])


def test_rule1_repeat_consistency_pass_and_fail():
    white = np.asarray(white_xyz("D50", "2"))
    base = lab_from_xyz(white * 0.5, white)
    near = [base[0] + 0.1, base[1] + 0.1, base[2] + 0.1]
    far = [base[0] + 3.0, base[1] - 2.0, base[2] + 2.0]

    # 一致复测 → verified
    session = {"repeat_mode": "repeat"}
    recs = [
        _rec([{"label": "A1", "lab": base, "xyz": None}]),
        _rec([{"label": "A1", "lab": near, "xyz": None}]),
    ]
    grade_session(session, recs)
    assert all(r["reliability"] == "verified" for r in recs)

    # 复测超差 → 双方 warning
    session = {"repeat_mode": "repeat"}
    recs = [
        _rec([{"label": "A1", "lab": base, "xyz": None}]),
        _rec([{"label": "A1", "lab": far, "xyz": None}]),
    ]
    grade_session(session, recs)
    assert all(r["reliability"] == "warning" for r in recs)
    assert any("复测不一致" in r for r in recs[0]["reasons"])


def test_rule2_paper_white_intrinsic_and_icc():
    # 本征异常纸白
    session = {"repeat_mode": "repeat"}
    recs = [_rec([{"label": "w", "lab": [55.0, 12.0, 25.0], "xyz": None}])]
    grade_session(session, recs)
    assert any("纸白" in r and "本征异常" in r for r in recs[0]["reasons"])

    # 与工艺 ICC 纸白偏差超限
    session = {"repeat_mode": "repeat"}
    recs = [_rec([{"label": "w", "lab": [95.0, 0.5, 2.0], "xyz": None}])]
    grade_session(session, recs, paper_white_lab=[88.0, 1.5, -6.0])
    assert any("与工艺 ICC 纸白偏差" in r for r in recs[0]["reasons"])


def test_rule4_density_sanity():
    def run(dens: dict, label: str = "C"):
        session = {"repeat_mode": "repeat"}
        recs = [_rec([{"label": label, "lab": None, "xyz": None, "densities": dens}])]
        grade_session(session, recs)
        return recs[0]

    r = run({"C": -0.02, "M": 0.1, "Y": 0.1, "K": 0.1})
    assert r["reliability"] == "rejected" and r["density_ok"] is False

    r = run({"C": 2.8, "M": 0.1, "Y": 0.1, "K": 0.1})
    assert r["reliability"] == "rejected" and any("量程上限" in x for x in r["reasons"])

    r = run({"C": 2.2, "M": 0.1, "Y": 0.1, "K": 0.1})
    assert r["reliability"] == "warning" and r["density_ok"] is False

    # 实地 C 块的 M 滤色最大 → 通道错位嫌疑
    r = run({"C": 0.9, "M": 1.3, "Y": 0.2, "K": 0.4}, label="C")
    assert any("错位嫌疑" in x for x in r["reasons"])

    # 正常实地 C 块 → 本通道最大，无发现
    r = run({"C": 1.55, "M": 0.43, "Y": 0.17, "K": 0.78}, label="C")
    assert r["reliability"] == "warning"  # 仅单次测量提示
    assert not any("错位" in x for x in r["reasons"])
    assert r["density_ok"] is True


def test_rule4_chromaticity_range():
    session = {"repeat_mode": "repeat"}
    recs = [_rec([
        {"label": "A1", "lab": [30.0, 8.58, 305.27], "xyz": [-22.69, -23.75, -26.41]},
    ])]
    grade_session(session, recs)
    assert recs[0]["reliability"] == "rejected"
    assert any("负值" in r for r in recs[0]["reasons"])
    assert any("超出真实材料范围" in r for r in recs[0]["reasons"])


def test_rule5_ramp_outlier():
    n = 8
    patches = []
    for i in range(n):
        L = 10.0 + i * 10.0
        if i == 5:
            L += 45.0  # 注入离群
        patches.append({"label": f"A{i + 1}", "lab": [L, 0.0, 0.0], "xyz": None})
    session = {"repeat_mode": "repeat"}
    recs = [_rec(patches)]
    grade_session(session, recs)
    assert any("梯尺离群" in r for r in recs[0]["reasons"])


# ---------------------------------------------------------------------------
# API 全流程
# ---------------------------------------------------------------------------


def _import(client, content: bytes, **params):
    return client.post(
        "/api/qc/measurements/import",
        params=params,
        content=content,
        headers={"Content-Type": "application/octet-stream"},
    )


def test_api_import_single_then_repeat_becomes_verified(api_client):
    rows = {f"A{i + 1}": _smooth_refl(i / 5) for i in range(6)}
    r = _import(api_client, make_spectral_cgats(rows), filename="m1.txt",
                instrument="Eye-One Pro", calibrated_at="2026-07-06T09:00:00", repeat_mode="single")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["record"]["reliability"] == "warning"
    sid = body["session"]["id"]
    assert body["session"]["condition"] == "M1"

    # 追加复测（同标签小扰动）并把会话切到 repeat → 两条均 verified
    rows2 = {k: [v * (1 + 0.002) for v in vals] for k, vals in rows.items()}
    r2 = _import(api_client, make_spectral_cgats(rows2), filename="m2.txt",
                 session_id=sid, repeat_mode="repeat")
    assert r2.status_code == 200
    b2 = r2.json()
    assert b2["record"]["reliability"] == "verified"
    assert b2["session"]["stats"] == {"verified": 2, "warning": 0, "rejected": 0}

    detail = api_client.get("/api/qc/measurements", params={"session_id": sid}).json()
    assert detail["repeat_mode"] == "repeat"
    assert len(detail["records"]) == 2
    assert detail["records"][0]["patches"][0]["lab_integrated"] is not None

    # 列表
    lst = api_client.get("/api/qc/measurements").json()
    assert any(s["id"] == sid for s in lst)


def test_api_import_rejected_garbage_and_parse_error(api_client):
    garbage = make_xyzlab_cgats([("A1", [-22.69, -23.75, -26.41], [0.0, 8.58, 305.27])])
    r = _import(api_client, garbage, filename="bad.txt",
                instrument="Eye-One Pro", calibrated_at="2026-07-06T09:00:00")
    assert r.status_code == 200
    assert r.json()["record"]["reliability"] == "rejected"
    assert r.json()["session"]["stats"]["rejected"] == 1

    r2 = _import(api_client, "完全不是测量文件".encode("utf-8"), filename="x.txt",
                 instrument="X", calibrated_at="t")
    assert r2.status_code == 422

    r3 = _import(api_client, b"\x00\xff\xfe garbage bytes \x00", filename="bin.txt",
                 instrument="X", calibrated_at="t")
    assert r3.status_code in (422, 400)


def test_api_import_requires_calibration_and_instrument(api_client):
    rows = {f"A{i + 1}": _smooth_refl(i / 5) for i in range(6)}
    r = _import(api_client, make_spectral_cgats(rows), filename="m.txt", instrument="Eye-One Pro")
    assert r.status_code == 400  # 缺校准时间（方法闭环第 2 步）
    r2 = _import(api_client, make_spectral_cgats(rows), filename="m.txt", calibrated_at="t")
    assert r2.status_code == 400  # 缺仪器登记


def test_api_ink_quality_verified_gate(api_client):
    """TSV 密度会话：复测 verified → QC 指标可用；追加 rejected 记录被排除。"""
    from app.services.cgats import lab_from_xyz, white_xyz

    white = np.asarray(white_xyz("D50", "2"))
    def _row(label, scale, den):
        xyz = [float(v) for v in white * scale]
        return (label, lab_from_xyz(xyz, white), xyz, den)

    rows = [
        _row("C", 0.09, [1.55, 0.43, 0.17, 0.78]),
        _row("M", 0.22, [0.20, 1.56, 0.86, 0.60]),
        _row("Y", 0.45, [0.02, 0.10, 1.09, 0.06]),
        _row("c60", 0.30, [0.51, 0.19, 0.06, 0.34]),
        _row("w", 1.00, [0.01, 0.01, 0.02, 0.01]),
    ]
    body = make_tsv(rows)
    r = _import(api_client, body, filename="q1.txt", instrument="Xrite530",
                calibrated_at="2026-07-08T10:00:00", repeat_mode="single")
    sid = r.json()["session"]["id"]
    assert r.json()["record"]["reliability"] == "warning"  # 单次测量
    q = api_client.get("/api/qc/ink-quality", params={"session_id": sid}).json()
    assert q["verified_records"] == 0 and q["inks"] == {}  # 门禁：warning 不进指标

    _import(api_client, body, filename="q2.txt", session_id=sid, repeat_mode="repeat")
    q2 = api_client.get("/api/qc/ink-quality", params={"session_id": sid}).json()
    assert q2["verified_records"] == 2 and q2["skipped_records"] == 0
    assert set(q2["inks"]) == {"C", "M", "Y"}
    assert q2["inks"]["C"]["strength"] == pytest.approx(1.55)
    assert q2["tvi"] and q2["tvi"][0]["nominal"] == 60
    assert q2["session"]["instrument"] == "Xrite530"

    # 追加一条 rejected 记录 → 被排除并计数
    garbage = make_xyzlab_cgats([("A1", [-22.0, -23.0, -26.0], [0.0, 8.58, 305.27])])
    _import(api_client, garbage, filename="bad.txt", session_id=sid)
    q3 = api_client.get("/api/qc/ink-quality", params={"session_id": sid}).json()
    assert q3["skipped_records"] == 1
    assert "bad.txt" not in q3["source_records"]


def test_api_session_and_record_delete_regrade(api_client):
    rows = {f"A{i + 1}": _smooth_refl(i / 5) for i in range(6)}
    sid = _import(api_client, make_spectral_cgats(rows), filename="a.txt",
                  instrument="I", calibrated_at="t").json()["session"]["id"]
    rec2 = _import(api_client, make_spectral_cgats(rows), filename="b.txt",
                   session_id=sid, repeat_mode="repeat").json()
    assert rec2["session"]["stats"]["verified"] == 2

    rid = rec2["record"]["id"]
    r = api_client.delete("/api/qc/measurements", params={"session_id": sid, "record_id": rid})
    assert r.status_code == 200
    # 只剩单条 → 未声明复测回到 warning（重分级语义）
    assert r.json()["session"]["stats"] == {"verified": 0, "warning": 1, "rejected": 0}

    r2 = api_client.delete("/api/qc/measurements", params={"session_id": sid})
    assert r2.status_code == 200
    assert api_client.get("/api/qc/measurements", params={"session_id": sid}).status_code == 404
