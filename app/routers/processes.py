"""工艺库路由：ICC 上传/替换、工艺 CRUD、色域采样、列表聚合摘要。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from ..services.processes import ProcessStore, gamut_summary, sample_process_gamut

router = APIRouter(prefix="/api/processes", tags=["processes"])

process_store = ProcessStore()


class ProcessUpdateRequest(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=80, description="工艺名称")
    paper: str | None = Field(None, max_length=80)
    ink: str | None = Field(None, max_length=80)
    notes: str | None = Field(None, max_length=300)


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
def list_processes(summary: bool = Query(False, description="附带色域摘要与达标率")):
    """工艺列表。summary=1 时逐工艺附带 gamut_summary（体积/覆盖率/凸包）与近 20 条配方达标率。"""
    docs = process_store.list()
    if not summary:
        return docs
    from ..services.recipe import RecipeStore

    recipes = RecipeStore()
    out = []
    for doc in docs:
        item = {**doc}
        try:
            item["gamut_summary"] = gamut_summary(doc)
        except Exception:  # noqa: BLE001 —— 色域采样失败的工艺照常返回，摘要置空
            item["gamut_summary"] = None
        hist = recipes.list(doc["id"])[:20]
        item["pass_rate"] = round(
            sum(1 for r in hist if r.get("converged")) / len(hist), 4
        ) if hist else None
        out.append(item)
    return out


@router.get("/{pid}")
def get_process(pid: str):
    doc = process_store.get(pid)
    if not doc:
        raise HTTPException(404, "工艺不存在")
    return doc


@router.patch("/{pid}")
def update_process(pid: str, req: ProcessUpdateRequest):
    """编辑工艺元数据（名称/纸张/墨水/备注，None 字段保持不变）。"""
    doc = process_store.update(pid, **req.model_dump())
    if not doc:
        raise HTTPException(404, "工艺不存在")
    return doc


@router.put("/{pid}/icc")
async def replace_icc(
    pid: str,
    request: Request,
    filename: str = Query("", description="新 ICC 原始文件名"),
):
    """替换工艺的 ICC 文件（sha 变更后色域/变换缓存自动失效）。"""
    doc = process_store.get(pid)
    if not doc:
        raise HTTPException(404, "工艺不存在")
    data = await request.body()
    if not data or len(data) < 128:
        raise HTTPException(400, "缺少有效的 ICC 文件数据（raw bytes body）")
    old_space = doc["icc_meta"]["device_space"]
    try:
        process_store.replace_icc(pid, data, filename)
    except Exception as e:  # noqa: BLE001 —— 无效 ICC 文件
        raise HTTPException(400, f"无法解析 ICC 文件: {e}") from e
    new_doc = process_store.get(pid)
    return {
        "status": "replaced",
        "process": new_doc,
        "space_changed": new_doc["icc_meta"]["device_space"] != old_space,
    }


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
