"""V0.9 P0-4 显示器档案与软打样门禁测试。

覆盖：校准状态（有效/即将到期/过期/未校准）、有效期计算、校准确认追加、
激活互斥、目标值与日期校验、软打样转换响应门禁、报告导出。
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.services.displays import DisplayStore
from tests.test_api_files import _png_b64

pytestmark = pytest.mark.api


@pytest.fixture(autouse=True)
def clean_displays():
    """显示器档案跨测试隔离（共享临时数据目录）。"""
    store = DisplayStore()
    for doc in store.list():
        store.delete(doc["id"])
    yield


def _create(api_client, *, calibration="default", **overrides):
    today = date.today()
    if calibration == "default":
        calibration = {
            "calibrated_at": today.isoformat(),
            "valid_months": 6,
            "calibrator": "i1Display Pro",
            "software": "DisplayCAL",
            "white_point": "D50",
            "luminance_cd_m2": 118.5,
            "black_luminance_cd_m2": 0.12,
            "contrast_ratio": 987.5,
            "profile_name": "Eizo_D50.icc",
        }
    payload = {
        "name": "Eizo CG2700X",
        "model": "CG2700X",
        "serial": "SN-001",
        "is_active": True,
        "targets": {
            "white_point": "D50",
            "luminance_cd_m2": 120,
            "contrast_ratio": 1000,
            "gamut_coverage": {"sRGB": 100, "AdobeRGB": 98.5},
        },
        "calibration": calibration,
    }
    payload.update(overrides)
    r = api_client.post("/api/displays", json=payload)
    assert r.status_code == 200, r.text
    return r.json()


def test_create_display_with_calibration(api_client):
    today = date.today()
    disp = _create(api_client)
    assert disp["is_active"] is True
    assert disp["targets"]["white_point"] == "D50"
    assert disp["status"]["status"] == "ok"
    assert disp["status"]["calibrated"] is True
    assert disp["status"]["expires_at"] > today.isoformat()
    assert len(disp["history"]) == 1

    rows = api_client.get("/api/displays").json()
    assert len(rows) == 1 and rows[0]["status"]["status"] == "ok"

    st = api_client.get("/api/displays/active-status").json()
    assert st["display_id"] == disp["id"] and st["calibrated"] is True


def test_expired_calibration_blocks_softproof(api_client, process_factory):
    disp = _create(api_client, calibration={
        "calibrated_at": "2020-01-01", "valid_months": 6, "calibrator": "旧校准",
    })
    assert disp["status"]["status"] == "expired"
    assert disp["status"]["calibrated"] is False
    assert "过期" in disp["status"]["message"]

    proc = process_factory("软打样工艺")
    r = api_client.post("/api/files/convert", json={
        "image_b64": _png_b64(), "process_id": proc["id"], "display_id": disp["id"],
    })
    assert r.status_code == 200, r.text
    sp = r.json()["softproof"]
    assert sp["status"] == "expired" and sp["calibrated"] is False
    assert "仅参考" in sp["message"]


def test_uncalibrated_and_missing_display(api_client):
    disp = _create(api_client, calibration=None)
    assert disp["status"]["status"] == "uncalibrated"
    assert disp["status"]["message"].count("仅参考") == 1

    missing = api_client.get("/api/displays/disp_missing/status")
    assert missing.status_code == 404

    st = api_client.get("/api/displays/active-status").json()
    assert st["status"] == "uncalibrated"


def test_add_calibration_refreshes_status(api_client):
    disp = _create(api_client, calibration={
        "calibrated_at": "2020-01-01", "valid_months": 6,
    })
    r = api_client.post(f"/api/displays/{disp['id']}/calibrations", json={
        "calibrated_at": date.today().isoformat(),
        "valid_months": 12,
        "calibrator": "i1Pro 3",
        "profile_name": "Eizo_D50_v2.icc",
        "luminance_cd_m2": 119.2,
        "contrast_ratio": 995.0,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"]["status"] == "ok"
    assert body["calibration"]["profile_name"] == "Eizo_D50_v2.icc"
    assert len(body["history"]) == 2

    report = api_client.get(f"/api/displays/{disp['id']}/report").json()
    assert report["status"]["status"] == "ok"
    assert report["display"]["id"] == disp["id"]
    assert report["notes"]


def test_activate_is_mutually_exclusive(api_client):
    first = _create(api_client, name="显示器 A")
    second = _create(api_client, name="显示器 B")
    assert second["is_active"] is True

    rows = {d["id"]: d for d in api_client.get("/api/displays").json()}
    assert rows[first["id"]]["is_active"] is False
    assert rows[second["id"]]["is_active"] is True

    r = api_client.post(f"/api/displays/{first['id']}/activate")
    assert r.status_code == 200
    rows = {d["id"]: d for d in api_client.get("/api/displays").json()}
    assert rows[first["id"]]["is_active"] is True
    assert rows[second["id"]]["is_active"] is False


def test_validation_guards(api_client):
    base = {"name": "X", "targets": {"luminance_cd_m2": 120, "contrast_ratio": 1000}}

    r = api_client.post("/api/displays", json={**base, "name": ""})
    assert r.status_code == 422

    r = api_client.post("/api/displays", json={**base, "targets": {"luminance_cd_m2": 10}})
    assert r.status_code == 422 and "亮度" in r.json()["detail"]

    r = api_client.post("/api/displays", json={**base, "targets": {"white_point": "D99"}})
    assert r.status_code == 422 and "白点" in r.json()["detail"]

    r = api_client.post("/api/displays", json={
        **base, "calibration": {"calibrated_at": "2026-01-01", "valid_months": 0},
    })
    assert r.status_code == 422 and "有效期" in r.json()["detail"]

    r = api_client.post("/api/displays", json={
        **base, "calibration": {"calibrated_at": "not-a-date"},
    })
    assert r.status_code == 422 and "日期" in r.json()["detail"]


def test_update_and_delete(api_client):
    disp = _create(api_client)
    r = api_client.patch(f"/api/displays/{disp['id']}", json={
        "name": "Eizo CG2700X（主）",
        "targets": {"white_point": "D65", "luminance_cd_m2": 100, "contrast_ratio": 800},
    })
    assert r.status_code == 200, r.text
    assert r.json()["name"].endswith("（主）")
    assert r.json()["targets"]["white_point"] == "D65"

    r = api_client.patch(f"/api/displays/{disp['id']}", json={"targets": {"luminance_cd_m2": 110}})
    assert r.status_code == 200, r.text
    targets = r.json()["targets"]
    assert targets["luminance_cd_m2"] == 110
    assert targets["white_point"] == "D65"      # 未提供的字段保持原值
    assert targets["contrast_ratio"] == 800

    assert api_client.delete(f"/api/displays/{disp['id']}").status_code == 200
    assert api_client.get(f"/api/displays/{disp['id']}").status_code == 404
    assert api_client.delete(f"/api/displays/{disp['id']}").status_code == 404


def test_expiring_threshold(api_client):
    """到期前 30 天内 → expiring（仍视为 calibrated）。"""
    # 1 个月有效、10 天前校准 → 到期日约 20 天后
    started = date.today() - timedelta(days=10)
    disp = _create(api_client, calibration={
        "calibrated_at": started.isoformat(), "valid_months": 1,
    })
    assert disp["status"]["status"] == "expiring"
    assert disp["status"]["calibrated"] is True
    assert 0 <= disp["status"]["days_left"] <= 30


CGATS = b"""CGATS.17
NUMBER_OF_FIELDS 4
NUMBER_OF_SETS 2
BEGIN_DATA_FORMAT
SAMPLE_NAME XYZ_X XYZ_Y XYZ_Z
END_DATA_FORMAT
BEGIN_DATA
White 95.05 100.0 108.90
Black 0.0100 0.0100 0.0100
END_DATA
"""


def test_measurement_import_and_export(api_client):
    """屏幕测量导入 → 白点/对比度推导 → 原样导出；缺省实测值自动回填校准记录。"""
    disp = _create(api_client, calibration={
        "calibrated_at": date.today().isoformat(), "valid_months": 6, "calibrator": "i1Pro",
    })
    r = api_client.post(
        f"/api/displays/{disp['id']}/measurements",
        params={"filename": "screen.cgats", "white_luminance_cd_m2": 120},
        content=CGATS,
        headers={"Content-Type": "text/plain"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    m = body["measurement"]
    assert m["patches"] == 2 and m["xyz_patches"] == 2
    assert m["contrast_ratio"] == 10000.0
    assert m["luminance_cd_m2"] == 120.0
    assert abs(m["white_xy"][0] - 0.3127) < 0.001
    assert abs(m["white_xy"][1] - 0.3290) < 0.001
    # 校准记录中为空的实测字段被测量结果补齐
    assert body["calibration"]["luminance_cd_m2"] == 120.0
    assert body["calibration"]["contrast_ratio"] == 10000.0

    r = api_client.get(f"/api/displays/{disp['id']}/measurements/latest")
    assert r.status_code == 200
    assert r.content == CGATS
    assert r.headers["x-content-sha256"] == m["sha256"]
    assert "attachment" in r.headers["content-disposition"]


def test_measurement_guards(api_client):
    disp = _create(api_client)
    # 尚未导入 → 导出 404
    assert api_client.get(f"/api/displays/{disp['id']}/measurements/latest").status_code == 404
    # 无 XYZ 字段 → 422
    bad = b"""CGATS.17
BEGIN_DATA_FORMAT
SAMPLE_NAME LAB_L LAB_A LAB_B
END_DATA_FORMAT
BEGIN_DATA
D50 50 0 0
D65 60 0 0
END_DATA
"""
    r = api_client.post(f"/api/displays/{disp['id']}/measurements", content=bad)
    assert r.status_code == 422
    assert "XYZ" in r.json()["detail"]
    # 空文件 → 422
    r = api_client.post(f"/api/displays/{disp['id']}/measurements", content=b"")
    assert r.status_code == 422