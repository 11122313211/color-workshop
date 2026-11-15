"""API 层测试：静态资源与应用外壳（吸收 e2e_http 第 0 步）。"""

import pytest


@pytest.mark.api
def test_health(api_client):
    r = api_client.get("/api/health")
    assert r.status_code == 200
    data = r.json()
    assert data.get("status") == "ok"
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
    assert "工艺库" in shell.text
    for path in ("/static/css/app.css", "/static/js/main.js", "/static/js/router.js"):
        r = api_client.get(path)
        assert r.status_code == 200, path
        assert r.content, path
    print("[PASS] 静态外壳与核心资源 200")
