"""API 层测试：静态资源与应用外壳（吸收 e2e_http 第 0 步）。"""

import pytest


@pytest.mark.api
def test_health(api_client):
    r = api_client.get("/api/health")
    assert r.status_code == 200
    data = r.json()
    assert data.get("status") == "ok"
    assert data.get("version") == "0.11.0"
    print("[PASS] /api/health")


@pytest.mark.api
def test_root_redirects_to_frontend(api_client):
    r = api_client.get("/", follow_redirects=False)
    assert r.status_code in (301, 302, 307, 308)
    assert "/static/index.html" in r.headers["location"]
    print("[PASS] / 重定向到前端")


@pytest.mark.api
def test_static_shell_and_assets(api_client):
    shell = api_client.get("/static/index.html")
    assert shell.status_code == 200
    assert "工艺库" in shell.text and "v0.11.0" in shell.text
    for path in ("/static/css/app.css", "/static/js/main.js", "/static/js/router.js"):
        r = api_client.get(path)
        assert r.status_code == 200, path
        assert r.content, path
    process_js = api_client.get("/static/js/views/processes.js")
    assert "工艺护照" in process_js.text and "measurement_condition" in process_js.text
    assert "显示已归档" in process_js.text and "版本历史" in process_js.text
    monitor_js = api_client.get("/static/js/views/monitor.js")
    assert "同色异谱" in monitor_js.text and "spectral-compare" in monitor_js.text
    recipe_js = api_client.get("/static/js/views/recipes.js")
    assert "分色值（设备值）" in recipe_js.text and "recipe_kind" in recipe_js.text
    print("[PASS] 静态外壳与核心资源 200")
