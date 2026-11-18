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
from ..services.measurements import MeasurementStore
from ..services.processes import ProcessStore
from ..services.signoffs import (
    EVIDENCE_KEYS,
    SIGN_STATES,
    SignoffStore,
    summary,
)

router = APIRouter(prefix="/api/signoffs", tags=["signoffs"])

_processes = ProcessStore()


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
    return evidence


def _act(signoff_id: str, action: str, req: SignoffActionRequest) -> dict:
    store = SignoffStore()
    try:
        doc = store.transition(signoff_id, action, req.actor, req.note)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    return summary(doc)


@router.post("")
def create_signoff(req: SignoffCreateRequest):
    """创建签样草稿；证据可留空，提交时至少需一项。"""
    proc = _process_or_404(req.process_id)
    evidence = _validate_evidence(proc["id"], req.evidence)
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
    try:
        deleted = store.delete(signoff_id)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(409, str(e)) from e
    return {"status": "deleted" if deleted else "not_found"}