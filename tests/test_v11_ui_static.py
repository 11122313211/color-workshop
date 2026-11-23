"""V0.11 前端静态契约：归档入口、数据维护与模态 / 光谱可达性。"""

import pytest

pytestmark = pytest.mark.api


def _text(api_client, path: str) -> str:
    response = api_client.get(path)
    assert response.status_code == 200, (path, response.status_code, response.text)
    assert response.text
    return response.text


def test_processes_js_exposes_archive_restore_permanent_and_history(api_client):
    text = _text(api_client, "/static/js/views/processes.js")
    for phrase in ("显示已归档", "恢复", "永久删除", "版本历史"):
        assert phrase in text, phrase
    assert "include_archived" in text
    assert "archived_at" in text


def test_tools_js_exposes_data_maintenance(api_client):
    text = _text(api_client, "/static/js/views/tools.js")
    assert "数据维护" in text
    assert "/api/integrity/report" in text
    assert "/api/integrity/repair" in text
    assert "dry_run" in text


def test_components_modal_sets_labelledby_and_background_inert(api_client):
    text = _text(api_client, "/static/js/components.js")
    assert "inert" in text
    assert "aria-labelledby" in text
    assert "aria-label" in text


def test_monitor_spectral_canvas_and_table_are_described(api_client):
    text = _text(api_client, "/static/js/views/monitor.js")
    assert "aria-describedby" in text
    assert 'role="img"' in text
    assert "spectral-modal-canvas" in text


def test_api_js_handles_structured_error_detail(api_client):
    text = _text(api_client, "/static/js/api.js")
    assert "data.detail" in text
    assert ".message" in text
    assert ".code" in text
    assert "PROCESS_ARCHIVED" in text or "String(detail" in text
