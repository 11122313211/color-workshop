"""工艺库路由：ICC 上传、工艺 CRUD、色域采样。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Query
from fastapi.responses import Response

from ..services.processes import ProcessStore, sample_process_gamut

router = APIRouter(prefix="/api/processes", tags=["processes"])

process_store = ProcessStore()


@router.post("")
async def create_process(
    request: Request,
    name: str = Query(..., description="工艺名称"),
    paper: str = Query("", description="纸张"),
    ink: str = Query("", description="墨水"),
    notes: str = Query("", description="备注"),
    filename: str = Query("", description="ICC 原始文件名"),
):
    """创建工艺（body 为 ICC 文件原始字节，Content-Type: application/octet-stream）。"""
    data = await request.body()
    if not data or len(data) < 128:
        raise HTTPException(400, "缺少有效的 ICC 文件数据（raw bytes body）")
    try:
        meta = ProcessStore.parse_meta(data)
    except Exception as e:  # noqa: BLE001 —— 无效 ICC 文件
        raise HTTPException(400, f"无法解析 ICC 文件: {e}") from e
    return process_store.create(name, data, paper, ink, notes, filename)


@router.get("")
def list_processes():
    return process_store.list()


@router.get("/{pid}")
def get_process(pid: str):
    doc = process_store.get(pid)
    if not doc:
        raise HTTPException(404, "工艺不存在")
    return doc


@router.delete("/{pid}")
def delete_process(pid: str):
    if not process_store.delete(pid):
        raise HTTPException(404, "工艺不存在")
    # 级联清理该工艺的配方记录
    from ..services.recipe import RecipeStore

    RecipeStore().remove_process(pid)
    return {"status": "deleted"}


@router.get("/{pid}/icc")
def download_icc(pid: str):
    doc = process_store.get(pid)
    if not doc:
        raise HTTPException(404, "工艺不存在")
    data = process_store.icc_bytes(pid)
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{doc["icc_original_name"]}"'
        },
    )


@router.get("/{pid}/gamut")
def process_gamut(pid: str, levels: int = 6):
    doc = process_store.get(pid)
    if not doc:
        raise HTTPException(404, "工艺不存在")
    try:
        return sample_process_gamut(doc, levels=max(3, min(levels, 10)))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"色域采样失败: {e}") from e
