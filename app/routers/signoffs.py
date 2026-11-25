"""签样工作流路由（V0.9 · P0-3）。

- POST   /api/signoffs                    创建签样草稿（关联工艺 + 证据）
- GET    /api/signoffs                    列表 / 按工艺、状态过滤
- GET    /api/signoffs/gate               放行门禁（工艺维度）
- GET    /api/signoffs/release-manifest   交接清单（未放行返回 409）
- GET    /api/signoffs/{id}               详情（含历史）
- POST   /api/signoffs/{id}/submit        提交确认
- POST   /api/signoffs/{id}/approve       批准
- POST   /api/signoffs/{id}/reject        驳回（必须填写理由）
- POST   /api/signoffs/{id}/withdraw      撤回草稿
- POST   /api/signoffs/{id}/lock          锁定（终态 + 放行令牌）
- DELETE /api/signoffs/{id}               删除（仅草稿）
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from ..models.schemas import SignoffActionRequest, SignoffCreateRequest
from ..services.evaluations import EvaluationStore
from ..services.lifecycle import archived_detail, is_archived
from ..services.measurements import MeasurementStore
from ..services.processes import ProcessStore
from ..services.signoffs import (
    EVIDENCE_KEYS,
    SIGN_STATES,
    SignoffStore,
    summary,
)
from ._http import key_error_msg

router = APIRouter(prefix="/api/signoffs", tags=["signoffs"])

_processes = ProcessStore()


def _ensure_active(proc: dict) -> None:
    if is_archived(proc):
        raise HTTPException(409, detail=archived_detail())


def _process_or_404(process_id: str) -> dict:
    doc = _processes.get(process_id)
    if doc is None:
        raise HTTPException(404, "工艺不存在")
    return doc


def _validate_evidence(process_id: str, raw: dict) -> dict:
    """证据引用完整性：会话/评价必须存在且绑定同一工艺；未知键被丢弃。"""
    evidence = {k: (raw or {}).get(k) for k in EVIDENCE_KEYS if (raw or {}).get(k)}
    measurements = MeasurementStore()
    for key in ("calibration_session_id", "measurement_session_id"):
        sid = evidence.get(key)
        if not sid:
            continue
        session = measurements.get_session(sid)
        if session is None:
            raise HTTPException(422, f"{key} 对应的测量会话不存在")
        bound = session.get("process_id") or ""
        if bound and bound != process_id:
            raise HTTPException(422, f"{key} 绑定的是其他工艺，不能用于本签样")
    eid = evidence.get("evaluation_id")
    if eid:
        evaluation = EvaluationStore().get(eid)
        if evaluation is None:
            raise HTTPException(422, "evaluation_id 对应的评价不存在")
        bound = evaluation.get("process_id") or ""
        if bound and bound != process_id:
            raise HTTPException(422, "该评价绑定的是其他工艺，不能用于本签样")
    rv = evidence.get("render_verification")
    if rv is not None:
        if not isinstance(rv, dict):
            raise HTTPException(422, "render_verification 必须是对象")
        verdict = rv.get("verdict")
        if verdict is not None and verdict not in ("verified", "warning", "rejected"):
            raise HTTPException(422, "render_verification.verdict 必须为 verified / warning / rejected")
    return evidence


def _attach_passport(
    process_doc: dict,
    passport_id: str,
    passport_hash: str,
    evidence: dict,
) -> dict:
    """关联护照：必须属于当前工艺、状态 valid，并以快照实际 hash 为准。"""
    process_id = process_doc["id"]
    passport_id = (passport_id or "").strip()
    passport_hash = (passport_hash or "").strip()
    if not passport_id and not passport_hash:
        return evidence
    from ..services.passport import PassportStore

    store = PassportStore()
    snapshot = None
    if passport_id:
        snapshot = store.get_by_id(process_id, passport_id)
        if snapshot is None:
            raise HTTPException(422, "passport_id 对应的工艺护照不存在")
    if passport_hash:
        by_hash = store.get_by_hash(process_id, passport_hash)
        if by_hash is None:
            raise HTTPException(422, "passport_hash 对应的工艺护照不存在")
        if snapshot is not None and by_hash.get("passport_hash") != snapshot.get("passport_hash"):
            raise HTTPException(422, "passport_id 与 passport_hash 不匹配")
        snapshot = snapshot or by_hash
    exposed = store.expose(snapshot, process_doc)
    if exposed["status"] != "valid":
        reasons = "；".join(
            row.get("message", "") for row in (exposed.get("status_reasons") or [])
        )
        detail = (
            f"仅可关联状态为 valid 的工艺护照（当前 {exposed.get('status_label')}）"
            + (f"：{reasons}" if reasons else "")
        )
        raise HTTPException(422, detail)
    evidence = {**evidence}
    evidence["passport_id"] = snapshot.get("id", "") if snapshot else passport_id
    evidence["passport_hash"] = snapshot.get("passport_hash", "") if snapshot else passport_hash
    return evidence


def _act(signoff_id: str, action: str, req: SignoffActionRequest) -> dict:
    store = SignoffStore()
    existing = store.get(signoff_id)
    if existing is None:
        raise HTTPException(404, "签样记录不存在")
    if existing.get("orphaned_at"):
        raise HTTPException(409, "签样所属工艺已永久清理，孤儿审计证据只读")
    if existing.get("process_id"):
        proc = _processes.get(existing.get("process_id", ""))
        if proc is not None:
            _ensure_active(proc)
    try:
        doc = store.transition(signoff_id, action, req.actor, req.note)
    except KeyError as e:
        raise HTTPException(404, key_error_msg(e, "签样记录不存在")) from e
    except PermissionError as e:
        raise HTTPException(409, str(e)) from e
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    return summary(doc)


@router.post("")
def create_signoff(req: SignoffCreateRequest):
    """创建签样草稿；证据可留空，提交时至少需一项。"""
    proc = _process_or_404(req.process_id)
    _ensure_active(proc)
    evidence = _validate_evidence(proc["id"], req.evidence)
    evidence = _attach_passport(
        proc,
        req.passport_id or str(evidence.get("passport_id") or ""),
        req.passport_hash or str(evidence.get("passport_hash") or ""),
        evidence,
    )
    doc = SignoffStore().create(
        proc,
        title=req.title,
        job_no=req.job_no,
        created_by=req.created_by,
        evidence=evidence,
    )
    return summary(doc)


@router.get("")
def list_signoffs(
    process_id: str = Query("", description="按工艺过滤"),
    state: str = Query("", description="按状态过滤 draft/pending/approved/rejected/locked"),
):
    if state and state not in SIGN_STATES:
        raise HTTPException(422, f"未知状态：{state}")
    return [summary(d) for d in SignoffStore().list(process_id=process_id, state=state)]


@router.get("/gate")
def signoff_gate(process_id: str = Query(..., description="工艺 id")):
    """放行门禁：最新 approved/locked 签样 + ICC 指纹一致性。"""
    proc = _process_or_404(process_id)
    return SignoffStore().gate(proc)


@router.get("/release-manifest")
def release_manifest(process_id: str = Query(..., description="工艺 id")):
    """交接清单：未签样或 ICC 已变更时返回 409（下游导出的门禁消费者）。"""
    proc = _process_or_404(process_id)
    if is_archived(proc):
        raise HTTPException(409, detail=archived_detail())
    try:
        return SignoffStore().release_manifest(proc)
    except PermissionError as e:
        raise HTTPException(409, str(e)) from e


@router.get("/{signoff_id}")
def get_signoff(signoff_id: str):
    doc = SignoffStore().get(signoff_id)
    if doc is None:
        raise HTTPException(404, "签样记录不存在")
    return summary(doc)


@router.post("/{signoff_id}/submit")
def submit_signoff(signoff_id: str, req: SignoffActionRequest):
    return _act(signoff_id, "submit", req)


@router.post("/{signoff_id}/approve")
def approve_signoff(signoff_id: str, req: SignoffActionRequest):
    return _act(signoff_id, "approve", req)


@router.post("/{signoff_id}/reject")
def reject_signoff(signoff_id: str, req: SignoffActionRequest):
    return _act(signoff_id, "reject", req)


@router.post("/{signoff_id}/withdraw")
def withdraw_signoff(signoff_id: str, req: SignoffActionRequest):
    return _act(signoff_id, "withdraw", req)


@router.post("/{signoff_id}/lock")
def lock_signoff(signoff_id: str, req: SignoffActionRequest):
    return _act(signoff_id, "lock", req)


@router.delete("/{signoff_id}")
def delete_signoff(signoff_id: str):
    store = SignoffStore()
    existing = store.get(signoff_id)
    if existing is None:
        raise HTTPException(404, "签样记录不存在")
    if existing.get("orphaned_at"):
        raise HTTPException(409, "签样所属工艺已永久清理，孤儿审计证据只读")
    if existing.get("process_id"):
        proc = _processes.get(existing.get("process_id", ""))
        if proc is not None:
            _ensure_active(proc)
    try:
        deleted = store.delete(signoff_id)
    except KeyError as e:
        raise HTTPException(404, key_error_msg(e, "签样记录不存在")) from e
    except PermissionError as e:
        raise HTTPException(409, str(e)) from e
    except ValueError as e:
        raise HTTPException(409, str(e)) from e
    return {"status": "deleted" if deleted else "not_found"}
