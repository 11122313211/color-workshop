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

from ..services.passport import PassportStore
from ..services.processes import ProcessStore

router = APIRouter(prefix="/api/processes", tags=["passports"])

_processes = ProcessStore()


def _process_or_404(pid: str) -> dict:
    doc = _processes.get(pid)
    if not doc:
        raise HTTPException(404, "工艺不存在")
    return doc


@router.post("/{pid}/passport")
def create_passport(pid: str):
    proc = _process_or_404(pid)
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
    out = []
    for snapshot in store.list_versions(pid):
        exposed = store.expose(snapshot, proc)
        out.append({
            "id": exposed["id"],
            "version": exposed["version"],
            "passport_hash": exposed["passport_hash"],
            "generated_at": exposed["generated_at"],
            "status": exposed["status"],
            "status_label": exposed["status_label"],
            "warnings": len(exposed.get("warnings") or []),
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
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{pid}/passport/{version}")
def get_passport_version(pid: str, version: int):
    proc = _process_or_404(pid)
    store = PassportStore()
    snapshot = store.get_version(pid, version)
    if snapshot is None:
        raise HTTPException(404, "护照版本不存在")
    return store.expose(snapshot, proc)
