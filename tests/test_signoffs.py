"""V0.9 签样工作流与放行门禁测试（P0-3）。

覆盖：状态机全路径（draft→pending→approved→locked）、驳回重提、
非法迁移拦截、提交时证据要求、ICC 变更导致签样失效、交接清单门禁。
"""

from __future__ import annotations

import pytest

from app.services.processes import ProcessStore
from app.services.signoffs import SignoffStore

pytestmark = pytest.mark.api

EVIDENCE = {"file_name": "封面-v3.pdf"}


def _create(api_client, process_id: str, evidence: dict | None = None) -> dict:
    r = api_client.post("/api/signoffs", json={
        "process_id": process_id,
        "title": "封面签样",
        "job_no": "J-2026-001",
        "created_by": "美工组",
        "evidence": EVIDENCE if evidence is None else evidence,
    })
    assert r.status_code == 200, r.text
    return r.json()


def _act(api_client, signoff_id: str, action: str, actor: str = "主管", note: str = ""):
    return api_client.post(f"/api/signoffs/{signoff_id}/{action}",
                           json={"actor": actor, "note": note})


def _gate(api_client, process_id: str) -> dict:
    r = api_client.get(f"/api/signoffs/gate?process_id={process_id}")
    assert r.status_code == 200, r.text
    return r.json()


def test_signoff_full_path_and_gate(api_client, process_factory):
    """草稿→提交→批准→放行→锁定，门禁全程联动。"""
    proc = process_factory("签样工艺")
    sg = _create(api_client, proc["id"])
    assert sg["state"] == "draft"
    assert sg["snapshot"]["icc_sha256"] == proc["icc_sha256"]

    gate = _gate(api_client, proc["id"])
    assert gate["allowed"] is False and gate["reason"] == "no_signoff"

    r = _act(api_client, sg["id"], "submit", "设计组")
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "pending"
    # pending 尚未批准，不放行
    assert _gate(api_client, proc["id"])["allowed"] is False

    r = _act(api_client, sg["id"], "approve", "客户张工", "色差与样张一致")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "approved"
    assert body["decision"]["by"] == "客户张工"

    gate = _gate(api_client, proc["id"])
    assert gate["allowed"] is True and gate["signoff_id"] == sg["id"]

    manifest = api_client.get(f"/api/signoffs/release-manifest?process_id={proc['id']}")
    assert manifest.status_code == 200, manifest.text
    data = manifest.json()
    assert data["process"]["icc_sha256"] == proc["icc_sha256"]
    assert data["signoff"]["id"] == sg["id"]

    r = _act(api_client, sg["id"], "lock", "客户张工")
    assert r.status_code == 200, r.text
    locked = r.json()
    assert locked["state"] == "locked"
    assert locked["release_token"].startswith("sha256:")
    assert locked["locked_at"]

    gate = _gate(api_client, proc["id"])
    assert gate["allowed"] is True and gate["state"] == "locked"


def test_release_manifest_blocked_without_signoff(api_client, process_factory):
    """无签样时交接清单 409，门禁给出 no_signoff。"""
    proc = process_factory("未签样工艺")
    assert _gate(api_client, proc["id"])["reason"] == "no_signoff"
    r = api_client.get(f"/api/signoffs/release-manifest?process_id={proc['id']}")
    assert r.status_code == 409
    assert "尚未有已批准" in r.json()["detail"]


def test_submit_requires_evidence(api_client, process_factory):
    """无任何依据的签样不允许提交。"""
    proc = process_factory("空证据工艺")
    sg = _create(api_client, proc["id"], evidence={})
    r = _act(api_client, sg["id"], "submit")
    assert r.status_code == 422
    assert "依据" in r.json()["detail"]


def test_reject_requires_reason_then_resubmit(api_client, process_factory):
    """驳回必须给理由；驳回后可修改并重新提交。"""
    proc = process_factory("驳回工艺")
    sg = _create(api_client, proc["id"])
    assert _act(api_client, sg["id"], "submit").json()["state"] == "pending"

    r = _act(api_client, sg["id"], "reject")
    assert r.status_code == 422
    assert "理由" in r.json()["detail"]

    r = _act(api_client, sg["id"], "reject", "客户", "封面偏红，需重新打样")
    assert r.status_code == 200
    assert r.json()["state"] == "rejected"
    assert _gate(api_client, proc["id"])["allowed"] is False

    r = _act(api_client, sg["id"], "submit", "设计组")
    assert r.status_code == 200
    assert r.json()["state"] == "pending"
    # 历史保留驳回与重提
    actions = [h["action"] for h in r.json()["history"]]
    assert actions == ["submit", "reject", "submit"]


def test_transition_guards(api_client, process_factory):
    """非法迁移：草稿不能直接批准/锁定，pending 不能删除，locked 为终态。"""
    proc = process_factory("迁移守卫工艺")
    sg = _create(api_client, proc["id"])

    assert _act(api_client, sg["id"], "approve").status_code == 422
    assert _act(api_client, sg["id"], "lock").status_code == 422

    assert _act(api_client, sg["id"], "submit").status_code == 200
    assert api_client.delete(f"/api/signoffs/{sg['id']}").status_code == 409
    assert _act(api_client, sg["id"], "lock").status_code == 422

    assert _act(api_client, sg["id"], "approve").status_code == 200
    assert _act(api_client, sg["id"], "lock").status_code == 200
    # locked 终态：任何动作与删除都被拒
    assert _act(api_client, sg["id"], "withdraw").status_code == 422
    assert _act(api_client, sg["id"], "approve").status_code == 422
    assert api_client.delete(f"/api/signoffs/{sg['id']}").status_code == 409


def test_icc_change_invalidates_signoff(api_client, process_factory, cmyk_icc):
    """工艺替换 ICC（指纹变化）→ 原签样失效，必须重新签样。"""
    proc = process_factory("换 ICC 工艺")
    sg = _create(api_client, proc["id"])
    _act(api_client, sg["id"], "submit")
    _act(api_client, sg["id"], "approve")
    assert _gate(api_client, proc["id"])["allowed"] is True

    changed = bytearray(cmyk_icc)
    changed[24:36] = b"\x00" * 12  # 改创建时间（元数据），ICC 仍可解析且指纹变化
    ProcessStore().replace_icc(proc["id"], bytes(changed), "replaced.icc")

    gate = _gate(api_client, proc["id"])
    assert gate["allowed"] is False
    assert gate["reason"] == "icc_changed"
    assert gate["snapshot_icc"] != gate["current_icc"]

    r = api_client.get(f"/api/signoffs/release-manifest?process_id={proc['id']}")
    assert r.status_code == 409
    assert "失效" in r.json()["detail"]


def test_evidence_reference_validated(api_client, process_factory):
    """证据引用的测量会话必须存在，且不能指向其他工艺。"""
    proc = process_factory("证据校验工艺")
    r = api_client.post("/api/signoffs", json={
        "process_id": proc["id"],
        "evidence": {"measurement_session_id": "sess_missing"},
    })
    assert r.status_code == 422
    assert "不存在" in r.json()["detail"]


def test_list_filters(api_client, process_factory):
    """列表按工艺与状态过滤。"""
    p1 = process_factory("工艺甲")
    p2 = process_factory("工艺乙")
    sg1 = _create(api_client, p1["id"])
    _create(api_client, p2["id"])

    rows = api_client.get(f"/api/signoffs?process_id={p1['id']}").json()
    assert len(rows) == 1 and rows[0]["id"] == sg1["id"]

    _act(api_client, sg1["id"], "submit")
    pending = api_client.get(
        f"/api/signoffs?process_id={p1['id']}&state=pending").json()
    assert len(pending) == 1 and pending[0]["id"] == sg1["id"]
    assert api_client.get("/api/signoffs?state=bogus").status_code == 422


def test_service_release_token_stable_after_lock(process_factory):
    """锁定令牌确定性：同一签样重复读取不变，draft 无令牌。"""
    proc = process_factory("令牌工艺")
    store = SignoffStore()
    doc = store.create(proc, evidence={"file_name": "proof.pdf"})
    assert doc.get("release_token") is None
    store.transition(doc["id"], "submit", "designer")
    store.transition(doc["id"], "approve", "client")
    locked = store.transition(doc["id"], "lock", "client")
    token = locked["release_token"]
    assert store.get(doc["id"])["release_token"] == token
    assert store.gate(proc)["release_token"] == token