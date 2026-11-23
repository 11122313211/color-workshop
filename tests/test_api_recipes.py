"""API 层测试：配方预测（批量上限 / verify 闭环 / forward / 错误路径）。"""

import pytest

pytestmark = pytest.mark.api


def _make_process(client, cmyk_icc, name="配方API工艺") -> dict:
    r = client.post(
        "/api/processes",
        params={"name": name},
        content=cmyk_icc,
        headers={"Content-Type": "application/octet-stream"},
    )
    return r.json()


def test_predict_batch_and_history(api_client, cmyk_icc):
    proc = _make_process(api_client, cmyk_icc)
    r = api_client.post(
        "/api/recipes/predict",
        json={
            "process_id": proc["id"],
            "targets": [
                {"label": "红", "lab": [50.0, 20.0, 5.0]},
                {"label": "青", "lab": [50.0, -20.0, -25.0]},
            ],
        },
    )
    assert r.status_code == 200
    data = r.json()
    assert data["process_id"] == proc["id"]
    assert data["process_name"] == "配方API工艺"
    assert len(data["results"]) == 2
    assert all(res["delta_e"] < 3.0 for res in data["results"])
    assert data["total_predictions"] > 0

    # 每目标各落一条配方历史
    history = api_client.get(f"/api/recipes?process_id={proc['id']}").json()
    assert len(history) == 2
    print("[PASS] 批量预测与历史落库")


def test_predict_targets_limit_422(api_client, cmyk_icc):
    """P0 回归：targets 超过 50 上限返回 422（防批量拖死服务）。"""
    proc = _make_process(api_client, cmyk_icc)
    r = api_client.post(
        "/api/recipes/predict",
        json={
            "process_id": proc["id"],
            "targets": [{"label": f"t{i}", "lab": [50.0, 20.0, 5.0]} for i in range(51)],
        },
    )
    assert r.status_code == 422
    print("[PASS] targets 上限 422")


def test_predict_unknown_process_404(api_client):
    r = api_client.post(
        "/api/recipes/predict",
        json={"process_id": "nope", "targets": [{"lab": [50, 20, 5]}]},
    )
    assert r.status_code == 404


def test_forward_channel_mismatch_400(api_client, cmyk_icc):
    proc = _make_process(api_client, cmyk_icc)
    r = api_client.post(
        "/api/recipes/forward",
        json={"process_id": proc["id"], "values": [10.0, 20.0]},
    )
    assert r.status_code == 400


def test_forward_ok(api_client, cmyk_icc):
    proc = _make_process(api_client, cmyk_icc)
    r = api_client.post(
        "/api/recipes/forward",
        json={"process_id": proc["id"], "values": [0.0, 100.0, 100.0, 0.0]},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["lab"][1] > 0  # M+Y 红 → a* > 0
    assert data["hex"].startswith("#")
    print(f"[PASS] 正查（hex={data['hex']}）")


def test_verify_closed_loop(api_client, cmyk_icc):
    """实测验证闭环：预测 → 回填带偏移实测 → 修正后 improved=True。"""
    proc = _make_process(api_client, cmyk_icc)
    target = [50.0, 20.0, 5.0]
    pr = api_client.post(
        "/api/recipes/predict",
        json={"process_id": proc["id"], "targets": [{"label": "闭环", "lab": target}]},
    ).json()
    result = pr["results"][0]
    p = result["predicted_lab"]
    measured = [p[0] + 3.0, p[1] - 2.0, p[2] + 1.5]

    rid = api_client.get(f"/api/recipes?process_id={proc['id']}").json()[0]["id"]
    ver = api_client.post(
        "/api/recipes/verify",
        json={"recipe_id": rid, "measured_lab": measured},
    )
    assert ver.status_code == 200
    data = ver.json()
    assert data["improved"] is True
    assert data["delta_e_corrected"] < data["delta_e_measured"]
    assert set(data["corrected_recipe"].keys()) == {"C", "M", "Y", "K"}

    # 验证记录应写入历史文档
    doc = api_client.get(f"/api/recipes/{rid}").json()
    assert len(doc["verifications"]) == 1
    print(f"[PASS] verify 闭环（{data['delta_e_measured']:.2f} -> {data['delta_e_corrected']:.2f}）")


def test_verify_unknown_recipe_404(api_client):
    r = api_client.post("/api/recipes/verify", json={"recipe_id": "nope", "measured_lab": [50, 20, 5]})
    assert r.status_code == 404


def test_stats_aggregation(api_client, cmyk_icc):
    """V0.4 监控聚合：总量/达标率/mean/P95/时间序列升序/分工艺。"""
    proc = _make_process(api_client, cmyk_icc, name="监控聚合工艺")
    api_client.post(
        "/api/recipes/predict",
        json={
            "process_id": proc["id"],
            "targets": [
                {"label": "红", "lab": [50.0, 20.0, 5.0]},
                {"label": "青", "lab": [50.0, -20.0, -25.0]},
                {"label": "黄", "lab": [85.0, -8.0, 30.0]},
            ],
        },
    )
    s = api_client.get("/api/recipes/stats").json()
    assert s["total"] >= 3
    assert 0.0 <= s["converged_rate"] <= 1.0
    assert s["mean_de"] is not None and s["p95_de"] is not None
    assert len(s["series"]) == s["total"]
    created = [p["created_at"] for p in s["series"]]
    assert created == sorted(created), "时间序列应为升序"
    mine = [p for p in s["processes"] if p["process_id"] == proc["id"]]
    assert mine and mine[0]["n"] >= 3
    assert mine[0]["pass_rate"] == 1.0
    print(f"[PASS] stats 聚合（total={s['total']} / mean ΔE={s['mean_de']}）")


def test_stats_drift_flag(api_client, cmyk_icc):
    """V0.4 漂移检测：最近 5 条 ΔE 显著抬升 → flag。直接写库构造序列。"""
    proc = _make_process(api_client, cmyk_icc, name="漂移工艺")
    from datetime import datetime, timedelta

    from app.services.recipe import RecipeStore

    store = RecipeStore()
    base = datetime(2026, 1, 1)
    for i in range(12):
        de = 0.2 if i < 7 else 2.0  # 前 7 条优、后 5 条劣化
        store.create(
            {"id": proc["id"], "name": "漂移工艺", "icc_meta": {"device_space": "CMYK"}},
            {
                "target_lab": [50, 20, 5], "predicted_lab": [50, 20, 5],
                "recipe": {"C": 1, "M": 1, "Y": 1, "K": 1},
                "delta_e": de, "converged": de <= 2.0,
                "iterations": [de], "predictions": 10, "lut_points": 1,
                "method": "cie2000", "tolerance": 2.0,
            },
        )
        # 覆写创建时间制造先后序
        doc = store.list(proc["id"])
        # create 用 now()，直接改最后一行的 created_at
        rid = max(store.list(proc["id"]), key=lambda d: d["created_at"])["id"]
        d = store.get(rid)
        d["created_at"] = (base + timedelta(hours=i)).isoformat(timespec="seconds")
        store._store.put(rid, d)

    s = api_client.get(f"/api/recipes/stats?process_id={proc['id']}").json()
    mine = [p for p in s["processes"] if p["process_id"] == proc["id"]]
    assert mine and mine[0]["n"] == 12
    assert mine[0]["drift_flag"] is True, f"应触发漂移告警: {mine[0]}"
    assert s["flags"], "全局 flags 应含告警"
    print(f"[PASS] 漂移告警触发（drift={mine[0]['drift']}）")


def test_verify_after_process_deleted_404(api_client, cmyk_icc):
    """V0.11：归档后写入 409；永久清理后才回到 404。"""
    proc = _make_process(api_client, cmyk_icc, name="删除工艺后verify")
    pr = api_client.post(
        "/api/recipes/predict",
        json={"process_id": proc["id"], "targets": [{"lab": [50.0, 20.0, 5.0]}]},
    ).json()
    rid = api_client.get(f"/api/recipes?process_id={proc['id']}").json()[0]["id"]
    assert api_client.delete(f"/api/processes/{proc['id']}").status_code == 200

    archived_predict = api_client.post(
        "/api/recipes/predict",
        json={"process_id": proc["id"], "targets": [{"lab": [50.0, 20.0, 5.0]}]},
    )
    assert archived_predict.status_code == 409
    assert archived_predict.json()["detail"]["code"] == "PROCESS_ARCHIVED"
    r = api_client.post("/api/recipes/verify", json={"recipe_id": rid, "measured_lab": [50, 20, 5]})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "PROCESS_ARCHIVED"
    assert api_client.get(f"/api/recipes?process_id={proc['id']}").json()

    permanent = api_client.delete(
        f"/api/processes/{proc['id']}/permanent",
        params={"confirm": proc["id"]},
    )
    assert permanent.status_code == 200
    r = api_client.post("/api/recipes/verify", json={"recipe_id": rid, "measured_lab": [50, 20, 5]})
    assert r.status_code == 404
    print("[PASS] 归档写操作 409；永久清理后 verify 404")


def test_compensation_accumulate_apply_reset(api_client, cmyk_icc):
    """V0.4 工艺级补偿模型：verify 沉淀 → n≥2 起 predict 应用 → 重置失效。"""
    proc = _make_process(api_client, cmyk_icc, name="补偿闭环工艺")
    target = [50.0, 20.0, 5.0]

    def _predict() -> dict:
        return api_client.post(
            "/api/recipes/predict",
            json={"process_id": proc["id"], "targets": [{"label": "补偿", "lab": target}]},
        ).json()["results"][0]

    # 第一次：n=0，未应用
    r0 = _predict()
    assert r0["compensation"] == {"n": 0, "applied": False}

    # 两次有效验证 → n=2
    for _ in range(2):
        res = _predict()
        p = res["predicted_lab"]
        measured = [p[0] + 3.0, p[1] - 2.0, p[2] + 1.5]
        rid = api_client.get(f"/api/recipes?process_id={proc['id']}").json()[0]["id"]
        ver = api_client.post("/api/recipes/verify", json={"recipe_id": rid, "measured_lab": measured})
        assert ver.status_code == 200 and ver.json()["improved"] is True

    det = api_client.get(f"/api/processes/{proc['id']}").json()
    assert det["compensation"]["n"] == 2
    assert any(v != 0.0 for v in det["compensation"]["bias_device"])

    # n≥2 → predict 应用偏置
    r2 = _predict()
    assert r2["compensation"] == {"n": 2, "applied": True}
    assert r2["delta_e"] < 3.0  # 应用偏置后仍正常收敛

    # 重置 → 失效
    rst = api_client.delete(f"/api/processes/{proc['id']}/compensation")
    assert rst.status_code == 200 and rst.json()["status"] == "reset"
    det2 = api_client.get(f"/api/processes/{proc['id']}").json()
    assert det2["compensation"] is None
    r3 = _predict()
    assert r3["compensation"]["applied"] is False
    print("[PASS] 补偿模型沉淀→应用→重置全链路")
