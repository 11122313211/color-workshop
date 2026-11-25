"""工艺护照路由（V0.10）。

- POST /api/processes/{pid}/passport                 生成/刷新（内容相同则幂等）
- GET  /api/processes/{pid}/passport                 最新护照（动态状态）
- GET  /api/processes/{pid}/passport/versions        版本列表
- GET  /api/processes/{pid}/passport/{version}       指定版本
- GET  /api/processes/{pid}/passport/{version}/export?format=json|md
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from ..services.lifecycle import archived_detail, is_archived
from ..services.passport import PassportStore, build_eval_context
from ..services.processes import ProcessStore
from ._http import content_disposition

router = APIRouter(prefix="/api/processes", tags=["passports"])

_processes = ProcessStore()


def _process_or_404(pid: str) -> dict:
    doc = _processes.get(pid)
    if not doc:
        raise HTTPException(404, "工艺不存在")
    return doc


def _ensure_active(proc: dict) -> None:
    if is_archived(proc):
        raise HTTPException(409, detail=archived_detail())


@router.post("/{pid}/passport")
def create_passport(pid: str):
    proc = _process_or_404(pid)
    _ensure_active(proc)
    exposed, created = PassportStore().create(proc)
    return {**exposed, "created": created}


@router.get("/{pid}/passport")
def get_latest_passport(pid: str):
    proc = _process_or_404(pid)
    store = PassportStore()
    snapshot = store.latest_snapshot(pid)
    if snapshot is None:
        raise HTTPException(404, "该工艺尚未生成护照")
    return store.expose(snapshot, proc)


@router.get("/{pid}/passport/versions")
def list_passport_versions(pid: str):
    proc = _process_or_404(pid)
    store = PassportStore()
    versions = store.list_versions(pid)
    latest_version = int((versions[0] if versions else {}).get("version", 0) or 0)
    # 一次性预计算当前工艺评估上下文与该工艺签样列表，消除 50 次重复解析 ICC 与 150+ 次读盘
    eval_ctx = build_eval_context(proc) if versions else None
    from ..services.signoffs import SignoffStore

    proc_signoffs = SignoffStore().list(process_id=pid) if versions else []
    out = []
    for snapshot in versions:
        exposed = store.expose(snapshot, proc, context=eval_ctx)
        pp_id = str(exposed.get("id", "") or "")
        pp_hash = str(exposed.get("passport_hash", "") or "")
        referenced_by = []
        for doc in proc_signoffs:
            matched = False
            for source in (doc.get("snapshot") or {}, doc.get("evidence") or {}):
                s_id = str(source.get("passport_id", "") or "")
                s_hash = str(source.get("passport_hash", "") or "")
                if (pp_id and s_id == pp_id) or (pp_hash and s_hash == pp_hash):
                    matched = True
                    break
            if matched:
                referenced_by.append({
                    "id": doc.get("id", ""),
                    "state": doc.get("state", "draft"),
                    "title": doc.get("title", ""),
                })
        out.append({
            "id": exposed["id"],
            "version": exposed["version"],
            "passport_hash": exposed["passport_hash"],
            "generated_at": exposed["generated_at"],
            "status": exposed["status"],
            "status_label": exposed["status_label"],
            "warnings": len(exposed.get("warnings") or []),
            "is_latest": exposed["version"] == latest_version,
            "referenced_by_signoffs": referenced_by,
            "referenced_count": len(referenced_by),
        })
    return out


@router.get("/{pid}/passport/{version}/export")
def export_passport(
    pid: str,
    version: int,
    format: str = Query("json", description="json / md"),
):
    proc = _process_or_404(pid)
    store = PassportStore()
    snapshot = store.get_version(pid, version)
    if snapshot is None:
        raise HTTPException(404, "护照版本不存在")
    if format not in ("json", "md"):
        raise HTTPException(422, "format 仅支持 json / md")
    exposed = store.expose(snapshot, proc)
    if format == "md":
        content = store.to_markdown(exposed)
        media = "text/markdown; charset=utf-8"
        ext = "md"
    else:
        content = store.to_json(exposed)
        media = "application/json; charset=utf-8"
        ext = "json"
    filename = f"passport-{pid}-v{version}.{ext}"
    return Response(
        content=content.encode("utf-8"),
        media_type=media,
        headers={"Content-Disposition": content_disposition(filename, fallback=filename)},
    )


@router.get("/{pid}/passport/{version}")
def get_passport_version(pid: str, version: int):
    proc = _process_or_404(pid)
    store = PassportStore()
    snapshot = store.get_version(pid, version)
    if snapshot is None:
        raise HTTPException(404, "护照版本不存在")
    return store.expose(snapshot, proc)
