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
    """gamut 响应包含 V0.3 新字段：lab_points / hull / coverage_srgb。"""
    proc = process_factory("新字段")
    r = api_client.get(f"/api/processes/{proc['id']}/gamut?levels=6")
    data = r.json()
    assert len(data["lab_points"]) == len(data["xy_points"]) == 6 ** 4
    assert len(data["hull"]) >= 3
    assert 0.0 <= data["coverage_srgb"] <= 1.0
    hull_pts = {tuple(p) for p in (data["xy_points"][i] for i in data["hull"])}
    assert len(hull_pts) == len(data["hull"])
    print(f"[PASS] gamut 新字段（hull {len(data['hull'])} 顶点 / 覆盖率 {data['coverage_srgb']:.2%}）")


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


def test_process_delete_cascade_removes_recipes(api_client, cmyk_icc):
    """级联删除回归：删工艺后 /api/recipes?process_id= 必须为空。"""
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
    after = api_client.get(f"/api/recipes?process_id={proc['id']}").json()
    assert after == []
    assert api_client.get(f"/api/processes/{proc['id']}").status_code == 404
    print("[PASS] 工艺删除级联清理配方记录")
