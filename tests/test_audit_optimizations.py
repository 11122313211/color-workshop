"""针对全项目审计与优化项的专项回归测试。

覆盖存储并发与容错、核心色彩算法、测量质检与 CGATS 解析、API 安全门禁、
校准幂等性以及前端关键静态修复点，防止后续迭代发生回退。
"""

from __future__ import annotations

import json
import math
import threading
from pathlib import Path

import numpy as np
import pytest

from app.algorithms.core import convert_color, delta_e
from app.services.cgats import parse_measurement_text
from app.services.evaluations import EvaluationStore
from app.services.filedb import JsonStore
from app.services.measurement_qa import grade_session
from app.services.measurements import MeasurementStore, _icc_paper_white
from app.services.processes import ProcessStore
from app.services.recipe import RecipeStore, _build_forward, predict_recipe
from app.services.signoffs import SignoffStore
from app.models.schemas import CorrectionRequest
from app.services.store import DeviceStore, run_correction


# ---------- 1. 存储并发、NaN 拒绝与异常隔离 ----------


def test_jsonstore_cross_instance_concurrent_writes(tmp_path):
    """不同 JsonStore 实例指向同一文件时，跨实例并发写入不得互相覆盖丢数据。"""
    db_path = tmp_path / "concurrent.json"
    errors: list[Exception] = []

    def worker(idx: int):
        try:
            st = JsonStore(db_path)
            st.put(f"k-{idx}", {"v": idx})
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(24)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    final = JsonStore(db_path).all()
    assert len(final) == 24
    for i in range(24):
        assert final[f"k-{i}"]["v"] == i


def test_jsonstore_rejects_nan_and_preserves_valid_file(tmp_path):
    """写入 NaN 必须抛 ValueError 且不得污染原有合法 JSON。"""
    db_path = tmp_path / "nan_guard.json"
    store = JsonStore(db_path)
    store.put("good", {"val": 1.0})

    with pytest.raises(ValueError):
        store.put("bad", {"val": float("nan")})

    assert store.get("good") == {"val": 1.0}
    assert store.get("bad") is None


def test_jsonstore_quarantines_corrupt_json_only(tmp_path):
    """JSON 损坏时自动隔离备份并返回空库，而非崩溃。"""
    db_path = tmp_path / "broken.json"
    db_path.write_text("{not valid json", encoding="utf-8")

    store = JsonStore(db_path)
    assert store.all() == {}
    backups = list(tmp_path.glob("broken.json.corrupt-*"))
    assert len(backups) == 1


def test_jsonstore_oserror_does_not_quarantine_healthy_file(tmp_path, monkeypatch):
    """暂时性 OSError 不得把健康 JSON 当损坏库隔离。"""
    db_path = tmp_path / "healthy.json"
    store = JsonStore(db_path)
    store.put("a", {"ok": True})

    orig_read = Path.read_text

    def fail_read(self, *args, **kwargs):
        if self == db_path:
            raise PermissionError("simulated transient lock")
        return orig_read(self, *args, **kwargs)

    # 绕过命中缓存，触发实际磁盘读取
    from app.services import filedb

    filedb._READ_CACHE.clear()
    monkeypatch.setattr(Path, "read_text", fail_read)

    with pytest.raises(PermissionError):
        store.all()

    monkeypatch.undo()
    assert not list(tmp_path.glob("healthy.json.corrupt-*"))
    assert store.get("a") == {"ok": True}


# ---------- 2. 色彩算法与转换修复 ----------


def test_convert_color_lab_to_srgb_not_scaled_by_255():
    """Lab 输入不应被误当作 0-255 RGB 除以 255（L*=50 应落于归一化 sRGB ~0.466，而非 ~0.002）。"""
    mid_gray_rgb = convert_color([50.0, 0.0, 0.0], "lab", "srgb")
    assert all(0.42 <= c <= 0.52 for c in mid_gray_rgb)

    white_rgb = convert_color([100.0, 0.0, 0.0], "lab", "srgb")
    assert all(0.98 <= c <= 1.01 for c in white_rgb)


def test_cmyk_unsupported_returns_422(api_client):
    """通用色彩转换与色域查询不再伪装支持无 ICC 的 cmyk，应返回 422。"""
    r1 = api_client.post(
        "/api/convert",
        json={"rgb": [10, 20, 30], "from_space": "srgb", "to_space": "cmyk"},
    )
    assert r1.status_code == 422

    r2 = api_client.get("/api/visualization/gamut?spaces=cmyk")
    assert r2.status_code == 422


def test_recipe_predict_with_device_bias_matches_forward(cmyk_icc):
    """传入 device_bias 时，predict_recipe 返回的 predicted_lab 必须与 forward(recipe) 一致。"""
    target = [55.0, 15.0, -10.0]
    bias = [0.05, -0.03, 0.02, 0.0]
    res = predict_recipe(cmyk_icc, "CMYK", target, device_bias=bias, tolerance=2.0)
    fwd = _build_forward(cmyk_icc, 4)
    vals = [res["recipe"][ch] / 100.0 for ch in ("C", "M", "Y", "K")]
    recomputed = fwd(np.array([vals], dtype=np.float64))[0]
    assert np.allclose(res["predicted_lab"], recomputed, atol=0.15)
    expected_de = delta_e(target, recomputed)
    assert abs(res["delta_e"] - expected_de) < 0.15


def test_store_run_correction_matrix_orientation():
    """run_correction 矩阵乘法方向正确，校正后 ΔE 显著收敛并接近 0。"""
    req = CorrectionRequest(
        device_id="dev-1",
        measured_rgb=(180.0, 90.0, 60.0),
        target_rgb=(160.0, 120.0, 80.0),
    )
    res = run_correction(DeviceStore(), req)
    assert res.delta_e_before > 5.0
    assert res.delta_e_after < 0.05
    assert res.converged is True


# ---------- 3. ICC 纸白检查、复测 QA 与 CGATS 解析 ----------


def test_icc_paper_white_and_rule2_drift(cmyk_icc, process_factory):
    """_icc_paper_white 应正常计算 CMYK 纸白 Lab，并能触发 Rule 2 漂移告警。"""
    proc = process_factory("纸白检查测试工艺")
    pw = _icc_paper_white(proc)
    assert pw is not None
    assert len(pw) == 3
    assert pw[0] > 80.0  # 纸白 L* 应在 80 以上

    # 构造明显偏黄/变暗的纸白记录，验证 Rule 2 会触发 warning
    session = {"instrument": "Eye-One", "calibrated_at": "2026-04-01", "repeat_mode": "single"}
    rec = {
        "id": "r1",
        "kind": "cgats",
        "patches": [{"label": "PAPER", "lab": [pw[0] - 8.0, pw[1] + 2.0, pw[2] + 6.0]}],
    }
    grade_session(session, [rec], paper_white_lab=pw)
    assert rec["reliability"] == "warning"
    assert any("纸白" in r for r in rec["reasons"])


def test_measurement_qa_single_file_dual_spot_and_three_sample_spread():
    """单文件双点复测不一致以及 3 点复测全对最大极差都必须被检出。"""
    session = {"instrument": "Eye-One", "calibrated_at": "2026-04-01", "repeat_mode": "repeat"}
    # 单文件内含同名色块 P1 两次测量，差异很大
    rec = {
        "id": "single-dual",
        "kind": "cgats",
        "patches": [
            {"label": "P1", "lab": [50.0, 0.0, 0.0]},
            {"label": "P1", "lab": [55.0, 0.0, 0.0]},
        ],
    }
    grade_session(session, [rec])
    assert rec["reliability"] == "warning"
    assert any("复测不一致" in r for r in rec["reasons"])

    # 3 条记录中首尾差异超过阈值（相邻差都在 0.35 内，但 0 与 2 差 0.7）
    recs3 = [
        {"id": "a", "kind": "cgats", "patches": [{"label": "P1", "lab": [50.0, 0.0, 0.0]}]},
        {"id": "b", "kind": "cgats", "patches": [{"label": "P1", "lab": [50.35, 0.0, 0.0]}]},
        {"id": "c", "kind": "cgats", "patches": [{"label": "P1", "lab": [50.70, 0.0, 0.0]}]},
    ]
    grade_session(session, recs3)
    assert all(r["reliability"] == "warning" for r in recs3)
    assert all(any("复测不一致" in msg for msg in r["reasons"]) for r in recs3)


def test_cgats_out_of_order_lab_and_nan_rejection():
    """CGATS 解析应支持乱序 LAB_B/LAB_A/LAB_L 列，同时拒绝 NaN。"""
    text = "\n".join([
        "CGATS.17",
        "NUMBER_OF_FIELDS 4",
        "BEGIN_DATA_FORMAT",
        "SAMPLE_NAME LAB_B LAB_A LAB_L",
        "END_DATA_FORMAT",
        "NUMBER_OF_SAMPLES 2",
        "BEGIN_DATA",
        "P1 12.5 -4.0 62.0",
        "P2 NaN 0.0 50.0",
        "END_DATA",
    ])
    parsed = parse_measurement_text(text.encode("utf-8"))
    assert len(parsed.patches) == 2
    p1, p2 = parsed.patches
    assert p1.label == "P1"
    assert p1.lab == [62.0, -4.0, 12.5]
    # NaN 行不得产出含 NaN 的 Lab
    assert p2.lab is None


# ---------- 4. 主观评价校验 ----------


def test_evaluation_rejects_bool_and_non_int_float():
    """主观评价分值必须为严格整数 1..5，拒绝 bool 和 3.9。"""
    store = EvaluationStore()
    ev = store.create_evaluation("book", title="严格分值测试")
    item = "颜色无明显偏色"
    try:
        with pytest.raises(ValueError):
            store.add_entry(ev["id"], "评委1", {item: True})  # type: ignore[dict-item]
        with pytest.raises(ValueError):
            store.add_entry(ev["id"], "评委1", {item: 3.9})  # type: ignore[dict-item]
        updated = store.add_entry(ev["id"], "评委1", {item: 4})
        assert updated["entries"][0]["scores"][item] == 4
    finally:
        store._store.delete(ev["id"])


# ---------- 5. API 门禁、中文下载头、漂移公式与校准幂等 ----------


def test_orphaned_signoff_is_read_only(api_client, process_factory):
    """孤儿签样禁止流转与删除（返回 409）。"""
    proc = process_factory("孤儿签样测试工艺")
    store = SignoffStore()
    rec = store.create(proc, title="测试签样", created_by="tester")
    # 模拟工艺永久删除后签样被标为 orphaned
    store._store.mutate_key(rec["id"], lambda r: {**r, "orphaned_at": "2026-04-01T00:00:00"})
    try:
        r_trans = api_client.post(
            f"/api/signoffs/{rec['id']}/submit",
            json={"actor": "boss"},
        )
        assert r_trans.status_code == 409

        r_del = api_client.delete(f"/api/signoffs/{rec['id']}")
        assert r_del.status_code == 409
    finally:
        store._store.delete(rec["id"])


def test_chinese_icc_download_and_missing_icc_409(api_client, process_factory):
    """中文名称 ICC 下载应返回 RFC 5987 Content-Disposition；文件缺失时返回 409。"""
    from app.services.processes import PROFILE_DIR

    proc = process_factory("铜版纸中文测试工艺")
    pid = proc["id"]

    r_ok = api_client.get(f"/api/processes/{pid}/icc")
    assert r_ok.status_code == 200
    cd = r_ok.headers.get("content-disposition", "")
    assert "filename*=UTF-8''" in cd

    # 移除磁盘上的 ICC 文件以验证 409 优雅降级
    icc_path = PROFILE_DIR / proc["icc_file"]
    backup = icc_path.read_bytes()
    icc_path.unlink()
    try:
        r_miss_dl = api_client.get(f"/api/processes/{pid}/icc")
        assert r_miss_dl.status_code == 409

        r_miss_pred = api_client.post(
            "/api/recipes/predict",
            json={"process_id": pid, "targets": [{"lab": [50, 0, 0]}]},
        )
        assert r_miss_pred.status_code == 409
    finally:
        icc_path.write_bytes(backup)


def test_recipes_stats_drift_direction_and_verify_session_guard(api_client, process_factory):
    """验证漂移告警方向（达标率上升不告警、下降 >25pp 告警）及跨工艺会话校验。"""
    p_improve = process_factory("达标率提升工艺")
    p_drop = process_factory("达标率下跌工艺")
    rstore = RecipeStore()
    created_ids: list[str] = []

    def _mock_result(de: float) -> dict:
        return {
            "target_lab": [50.0, 0.0, 0.0],
            "predicted_lab": [50.0, 0.0, 0.0],
            "recipe": {"C": 0.0, "M": 0.0, "Y": 0.0, "K": 50.0},
            "delta_e": de,
            "converged": de <= 2.0,
            "iterations": [de],
            "predictions": 1,
            "lut_points": 1,
        }

    try:
        # 提升工艺：前 5 条超差（ΔE=4.0），后 5 条全达标（ΔE=0.5），时间递增
        for idx, de in enumerate([4.0] * 5 + [0.5] * 5):
            rec = rstore.create(p_improve, _mock_result(de))
            rstore._store.mutate_key(rec["id"], lambda r, i=idx: {**r, "created_at": f"2026-04-01T10:00:{i:02d}"})
            created_ids.append(rec["id"])

        # 下跌工艺：前 5 条全达标（ΔE=0.5），后 5 条全超差（ΔE=4.0），时间递增
        for idx, de in enumerate([0.5] * 5 + [4.0] * 5):
            rec = rstore.create(p_drop, _mock_result(de))
            rstore._store.mutate_key(rec["id"], lambda r, i=idx: {**r, "created_at": f"2026-04-01T10:00:{i:02d}"})
            created_ids.append(rec["id"])

        stats = api_client.get("/api/recipes/stats").json()
        by_pid = {p["process_id"]: p for p in stats["processes"]}
        assert by_pid[p_improve["id"]]["drift_flag"] is False
        assert by_pid[p_drop["id"]]["drift_flag"] is True

        # 跨工艺测量会话核验应返回 422
        mstore = MeasurementStore()
        sess = mstore.create_session(
            instrument="Eye-One",
            calibrated_at="2026-04-01",
            process_id=p_drop["id"],
        )
        try:
            r_verify = api_client.post(
                "/api/recipes/verify",
                json={
                    "recipe_id": created_ids[0],
                    "measured_lab": [50.0, 0.0, 0.0],
                    "measurement_session_id": sess["id"],
                },
            )
            assert r_verify.status_code == 422
        finally:
            mstore.delete_session(sess["id"])
    finally:
        rstore._store.delete_many(created_ids)


def test_calibration_apply_idempotent_no_bias_doubling(api_client, process_factory):
    """同一会话重复调用 /api/qc/calibration/apply 不得把设备补偿偏置叠加翻倍。"""
    proc = process_factory("校准幂等测试工艺")
    pid = proc["id"]

    # 生成参考 CGATS 并对所有 C 梯尺施加轻微偏置
    from app.services.calibration import build_reference

    ref = build_reference(proc)
    mstore = MeasurementStore()
    sess = mstore.create_session(
        instrument="Eye-One",
        calibrated_at="2026-04-01",
        repeat_mode="repeat",
        process_id=pid,
    )
    try:
        # 构造两份一致的 CGATS（满足复测 verified 门禁），对 C 梯尺略微增浓
        lines = [
            "CGATS.17",
            "NUMBER_OF_FIELDS 4",
            "BEGIN_DATA_FORMAT",
            "SAMPLE_NAME LAB_L LAB_A LAB_B",
            "END_DATA_FORMAT",
            f"NUMBER_OF_SAMPLES {len(ref['patches'])}",
            "BEGIN_DATA",
        ]
        for p in ref["patches"]:
            L, a, b = p["expected_lab"]
            if p["label"].startswith("C") and p["label"][1:].isdigit():
                L = max(0.0, L - 4.0)
            lines.append(f"{p['label']} {L:.2f} {a:.2f} {b:.2f}")
        lines.append("END_DATA")
        payload = "\n".join(lines).encode("utf-8")

        for idx in (1, 2):
            r_imp = api_client.post(
                f"/api/qc/measurements/import?session_id={sess['id']}&filename=r{idx}.txt",
                content=payload,
            )
            assert r_imp.status_code == 200

        r1 = api_client.post(f"/api/qc/calibration/apply?session_id={sess['id']}")
        assert r1.status_code == 200
        bias1 = r1.json()["bias_device"]

        r2 = api_client.post(f"/api/qc/calibration/apply?session_id={sess['id']}")
        assert r2.status_code == 200
        bias2 = r2.json()["bias_device"]

        assert any(abs(v) > 1e-4 for v in bias1)
        assert np.allclose(bias1, bias2, atol=1e-4)
    finally:
        mstore.delete_session(sess["id"])


# ---------- 6. 前端静态回归检查 ----------


def test_frontend_audit_fixes_static():
    """静态校验前端关键安全、色彩准确度、生命周期与 CSS 修复点。"""
    root = Path(__file__).resolve().parent.parent / "static"

    comp = (root / "js" / "components.js").read_text(encoding="utf-8")
    assert "srgbOetf" in comp
    assert "closeAllModals" in comp
    assert "safeHex" in comp
    assert "downloadText" in comp

    router = (root / "js" / "router.js").read_text(encoding="utf-8")
    assert "closeAllModals()" in router
    assert "AbortController" in router
    assert "aria-current" in router

    recipes = (root / "js" / "views" / "recipes.js").read_text(encoding="utf-8")
    assert "esc(r.L)" in recipes
    assert "esc(r.label" in recipes

    processes = (root / "js" / "views" / "processes.js").read_text(encoding="utf-8")
    assert 'class="card passport-card"' in processes
    assert "esc(p.measurement_condition)" in processes
    assert "ref.reference_cgats" in processes

    monitor = (root / "js" / "views" / "monitor.js").read_text(encoding="utf-8")
    assert "(data.source_records || []).map(esc)" in monitor
    assert "disposeSpectralRedraw" in monitor
    assert "disposeQcRedraw" in monitor

    tools = (root / "js" / "views" / "tools.js").read_text(encoding="utf-8")
    assert "procGamutCache" in tools
    assert "ctx.onCleanup(disposeRedraw)" in tools

    css = (root / "css" / "app.css").read_text(encoding="utf-8")
    assert ".gamut-stage > .gamut-error" in css
    assert ".proc-chip" in css
    assert "100dvh" in css
