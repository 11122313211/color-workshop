"""API 层测试：工艺库（CRUD / 级联删除 / 色域 / 缓存回归 / GRAY）。

级联删除回归：删除工艺后其配方记录必须一并清理（P0 联动）。
"""

import pytest

pytestmark = pytest.mark.api


def _create_process(client, icc: bytes, name: str = "API测试工艺", **kw) -> dict:
    r = client.post(
        "/api/processes",
        params={"name": name, "filename": "fixture.icc", **kw},
        content=icc,
        headers={"Content-Type": "application/octet-stream"},
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_process_create_meta_and_download(api_client, cmyk_icc):
    proc = _create_process(api_client, cmyk_icc, paper="铜版纸", ink="四色")
    assert proc["icc_meta"]["device_space"] == "CMYK"
    assert proc["icc_meta"]["channels"] == 4
    assert proc["icc_sha256"]

    r = api_client.get(f"/api/processes/{proc['id']}/icc")
    assert r.status_code == 200
    assert r.content == cmyk_icc
    assert "attachment" in r.headers.get("content-disposition", "")

    got = api_client.get(f"/api/processes/{proc['id']}")
    assert got.status_code == 200
    assert got.json()["name"] == "API测试工艺"


def test_process_create_rejects_garbage(api_client):
    r = api_client.post("/api/processes", params={"name": "坏文件"}, content=b"too short")
    assert r.status_code == 400
    r2 = api_client.post("/api/processes", params={"name": "垃圾"}, content=b"x" * 256)
    assert r2.status_code == 400
    print("[PASS] 无效 ICC 拒绝（400）")


def test_process_get_unknown_404(api_client):
    assert api_client.get("/api/processes/nonexistent0").status_code == 404


def test_gamut_levels_clamped(api_client, process_factory):
    proc = process_factory("色域钳制")
    r = api_client.get(f"/api/processes/{proc['id']}/gamut?levels=99")
    assert r.status_code == 200
    assert r.json()["sampled"] == 10 ** 4  # 99 被钳制到 10
    r2 = api_client.get(f"/api/processes/{proc['id']}/gamut?levels=1")
    assert r2.status_code == 200
    assert r2.json()["sampled"] == 3 ** 4  # 1 被钳制到 3


def test_gamut_includes_hull_and_lab_points(api_client, process_factory):
    """gamut 响应包含 V0.3 新字段：lab_points / hull / coverage_srgb / coverages / intent。"""
    proc = process_factory("新字段")
    r = api_client.get(f"/api/processes/{proc['id']}/gamut?levels=6")
    data = r.json()
    assert len(data["lab_points"]) == len(data["xy_points"]) == 6 ** 4
    assert len(data["hull"]) >= 3
    assert 0.0 <= data["coverage_srgb"] <= 1.0
    assert data["intent"] == "relative"
    assert set(data["coverages"].keys()) == {"srgb", "dci-p3", "adobe-rgb", "rec2020"}
    hull_pts = {tuple(p) for p in (data["xy_points"][i] for i in data["hull"])}
    assert len(hull_pts) == len(data["hull"])
    print(f"[PASS] gamut 新字段（hull {len(data['hull'])} 顶点 / 覆盖率 {data['coverage_srgb']:.2%}）")


def test_gamut_intent_param_and_validation(api_client, process_factory):
    """gamut 支持 relative / absolute 渲染意图切换与缓存隔离，非法意图返回 422。"""
    proc = process_factory("意图切换")
    r_rel = api_client.get(f"/api/processes/{proc['id']}/gamut?levels=5&intent=relative")
    r_abs = api_client.get(f"/api/processes/{proc['id']}/gamut?levels=5&intent=absolute")
    assert r_rel.status_code == 200 and r_abs.status_code == 200
    d_rel, d_abs = r_rel.json(), r_abs.json()
    assert d_rel["intent"] == "relative" and d_abs["intent"] == "absolute"
    # 绝对色度含纸白偏移，白点 L* 通常低于相对色度的 100.0
    assert d_abs["L_range"][1] <= d_rel["L_range"][1]
    assert api_client.get(f"/api/processes/{proc['id']}/gamut?intent=perceptual").status_code == 422
    print("[PASS] gamut 渲染意图切换与 422 校验")


def test_gamut_levels_cache_regression(api_client, process_factory):
    """P0 回归：同一工艺先 levels=5 再 levels=6，必须返回不同密度（缓存 key 含 levels）。"""
    proc = process_factory("levels缓存")
    g5 = api_client.get(f"/api/processes/{proc['id']}/gamut?levels=5").json()
    g6 = api_client.get(f"/api/processes/{proc['id']}/gamut?levels=6").json()
    g5_again = api_client.get(f"/api/processes/{proc['id']}/gamut?levels=5").json()
    assert len(g5["xy_points"]) == 5 ** 4
    assert len(g6["xy_points"]) == 6 ** 4
    assert len(g5_again["xy_points"]) == 5 ** 4
    print("[PASS] levels 缓存 key 回归")


def test_gray_process_full_flow(api_client, gray_icc):
    """GRAY 工艺全链路：创建 → 色域 → 反查（1 通道泛化，P0 修复回归）。"""
    proc = _create_process(api_client, gray_icc, name="灰度工艺")
    assert proc["icc_meta"]["device_space"] == "GRAY"
    assert proc["icc_meta"]["channels"] == 1

    g = api_client.get(f"/api/processes/{proc['id']}/gamut?levels=5").json()
    assert g["sampled"] == 5

    r = api_client.post(
        "/api/recipes/predict",
        json={
            "process_id": proc["id"],
            "targets": [{"label": "灰", "lab": [60.0, 0.0, 0.0]}],
        },
    )
    assert r.status_code == 200, r.text
    result = r.json()["results"][0]
    assert set(result["recipe"].keys()) == {"K"}
    assert result["delta_e"] <= 1.5
    print(f"[PASS] GRAY 工艺全链路（ΔE={result['delta_e']}）")


def test_process_patch_update(api_client, cmyk_icc):
    """V0.3：PATCH 编辑工艺元数据（None 字段保持不变）。"""
    proc = _create_process(api_client, cmyk_icc, name="待编辑", paper="旧纸")
    r = api_client.patch(
        f"/api/processes/{proc['id']}",
        json={"name": "已编辑", "paper": "铜版纸 157g"},
    )
    assert r.status_code == 200
    doc = r.json()
    assert doc["name"] == "已编辑"
    assert doc["paper"] == "铜版纸 157g"
    assert doc["ink"] == ""  # 未传字段保持不变
    assert api_client.patch("/api/processes/nonexistent0", json={"name": "x"}).status_code == 404
    assert api_client.patch(f"/api/processes/{proc['id']}", json={"name": ""}).status_code == 422
    print("[PASS] PATCH 工艺编辑")


def test_process_replace_icc(api_client, cmyk_icc, rgb_icc):
    """V0.3：PUT 替换 ICC → sha 变更 + 设备空间变化提示。"""
    proc = _create_process(api_client, cmyk_icc, name="替换前")
    old_sha = proc["icc_sha256"]
    r = api_client.put(
        f"/api/processes/{proc['id']}/icc",
        params={"filename": "replaced.icc"},
        content=rgb_icc,
        headers={"Content-Type": "application/octet-stream"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "replaced"
    assert data["space_changed"] is True  # CMYK → RGB
    new_doc = data["process"]
    assert new_doc["icc_sha256"] != old_sha
    assert new_doc["icc_meta"]["device_space"] == "RGB"
    assert new_doc["icc_original_name"] == "replaced.icc"
    # 下载应返回新文件内容
    dl = api_client.get(f"/api/processes/{proc['id']}/icc")
    assert dl.content == rgb_icc
    # 无效 ICC 拒绝
    bad = api_client.put(f"/api/processes/{proc['id']}/icc", content=b"short")
    assert bad.status_code == 400
    print("[PASS] PUT 替换 ICC（sha 变更 + 空间变化提示）")


def test_process_list_summary(api_client, process_factory):
    """V0.3：summary=1 聚合接口 —— gamut_summary + pass_rate。"""
    proc = process_factory("聚合摘要")
    # 先预测一条配方，使 pass_rate 非 None
    pr = api_client.post(
        "/api/recipes/predict",
        json={"process_id": proc["id"], "targets": [{"label": "聚合", "lab": [50.0, 20.0, 5.0]}]},
    )
    assert pr.status_code == 200
    docs = api_client.get("/api/processes?summary=1").json()
    mine = [d for d in docs if d["id"] == proc["id"]]
    assert len(mine) == 1
    doc = mine[0]
    s = doc["gamut_summary"]
    assert s and s["volume"] > 0
    assert len(s["hull_xy"]) >= 3
    assert 0.0 <= s["coverage_srgb"] <= 1.0
    assert doc["pass_rate"] == 1.0  # 唯一一条配方已收敛
    print(f"[PASS] summary 聚合（hull {len(s['hull_xy'])} 顶点 / 达标率 {doc['pass_rate']}）")


def test_compensation_reset_unknown_404(api_client):
    """V0.4：补偿模型重置端点 —— 未知工艺 404。"""
    assert api_client.delete("/api/processes/nonexistent0/compensation").status_code == 404


def test_compensation_in_detail_payload(api_client, process_factory):
    """V0.4：工艺详情响应携带 compensation 字段（无模型时为 null）。"""
    proc = process_factory("详情补偿字段")
    doc = api_client.get(f"/api/processes/{proc['id']}").json()
    assert "compensation" in doc and doc["compensation"] is None


def test_gamut_compare(api_client, cmyk_icc):
    """V0.4 色域对比：同 ICC 双工艺 → 交集 = 全部（占比 1.0，归属全覆盖）。"""
    a = _create_process(api_client, cmyk_icc, name="对比A")
    b = _create_process(api_client, cmyk_icc, name="对比B")
    r = api_client.get(f"/api/processes/{a['id']}/gamut/compare?other={b['id']}&levels=5")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["a"]["sampled"] == 5 ** 4
    assert data["b"]["sampled"] == 5 ** 4
    assert len(data["inter_poly"]) >= 3
    assert data["area_inter_over_a"] > 0.99  # 同一 profile → 包络重合
    assert data["area_inter_over_b"] > 0.99
    # 有效色度点（排除退化原点）应全部落入对方包络
    assert data["a_tested"] > 0
    assert len(data["a_in_b"]) == data["a_tested"]
    print(f"[PASS] 色域对比（交集/A={data['area_inter_over_a']}）")


def test_gamut_compare_errors(api_client, cmyk_icc):
    """V0.4 色域对比错误路径：404 / 自身对比 400。"""
    a = _create_process(api_client, cmyk_icc, name="对比错误")
    assert api_client.get(f"/api/processes/{a['id']}/gamut/compare?other=nope").status_code == 404
    assert api_client.get("/api/processes/nope/gamut/compare?other=nope2").status_code == 404
    r = api_client.get(f"/api/processes/{a['id']}/gamut/compare?other={a['id']}")
    assert r.status_code == 400
    print("[PASS] 对比错误路径（404 / 自身 400）")


def test_process_delete_cascade_removes_recipes(api_client, cmyk_icc):
    """V0.11：DELETE 先归档保留证据；永久清理后才级联删除配方。"""
    proc = _create_process(api_client, cmyk_icc, name="级联删除")
    pr = api_client.post(
        "/api/recipes/predict",
        json={
            "process_id": proc["id"],
            "targets": [{"label": "级联", "lab": [50.0, 20.0, 5.0]}],
        },
    )
    assert pr.status_code == 200
    before = api_client.get(f"/api/recipes?process_id={proc['id']}").json()
    assert len(before) == 1

    d = api_client.delete(f"/api/processes/{proc['id']}")
    assert d.status_code == 200
    assert d.json()["status"] == "archived"
    assert api_client.get(f"/api/processes/{proc['id']}").status_code == 200
    active = api_client.get("/api/processes").json()
    assert all(row["id"] != proc["id"] for row in active)
    archived = api_client.get("/api/processes?include_archived=1").json()
    assert any(row["id"] == proc["id"] for row in archived)
    retained = api_client.get(f"/api/recipes?process_id={proc['id']}").json()
    assert retained == before

    permanent = api_client.delete(
        f"/api/processes/{proc['id']}/permanent",
        params={"confirm": proc["id"]},
    )
    assert permanent.status_code == 200
    after = api_client.get(f"/api/recipes?process_id={proc['id']}").json()
    assert after == []
    assert api_client.get(f"/api/processes/{proc['id']}").status_code == 404
    print("[PASS] V0.11 归档保留配方；永久清理后级联删除")
