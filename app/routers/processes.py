"""工艺库路由：ICC 上传/替换、工艺 CRUD、归档生命周期、色域采样与列表摘要。

V0.11：DELETE 默认归档；永久清理只允许已归档工艺，且保留测量/评价/签样证据。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from ..models.schemas import ProcessArchiveRequest
from ..services.compensation import CompensationStore
from ..services.lifecycle import (
    ProcessNotArchivedError,
    archived_detail,
    dependency_report,
    is_archived,
    permanently_delete_process,
)
from ..services.processes import (
    PROCESS_MEASUREMENT_CONDITIONS,
    ProcessStore,
    gamut_summary,
    sample_process_gamut,
)

router = APIRouter(prefix="/api/processes", tags=["processes"])

process_store = ProcessStore()
_compensations = CompensationStore()


def _ensure_active(doc: dict) -> None:
    """归档工艺只读：任何会改变其证据的写操作返回 409。"""
    if is_archived(doc):
        raise HTTPException(409, detail=archived_detail())


class ProcessUpdateRequest(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=80, description="工艺名称")
    paper: str | None = Field(None, max_length=80)
    ink: str | None = Field(None, max_length=80)
    notes: str | None = Field(None, max_length=300)
    measurement_condition: str | None = Field(
        None,
        description="V0.10 测量条件：空串 / M0 / M1 / M2",
    )


def _passport_summary(pid: str) -> dict | None:
    """最轻量的护照摘要；护照服务异常不得影响工艺主数据读取。"""
    from ..services.passport import PassportStore

    try:
        doc = process_store.get(pid)
        if not doc:
            return None
        return PassportStore().summary_for_process(doc)
    except Exception:  # noqa: BLE001 —— 护照损坏/旧数据时工艺仍可用
        return None


@router.post("")
async def create_process(
    request: Request,
    name: str = Query(..., description="工艺名称"),
    paper: str = Query("", description="纸张"),
    ink: str = Query("", description="墨水"),
    notes: str = Query("", description="备注"),
    filename: str = Query("", description="ICC 原始文件名"),
    measurement_condition: str = Query("", description="测量条件：空串 / M0 / M1 / M2"),
):
    """创建工艺（body 为 ICC 文件原始字节，Content-Type: application/octet-stream）。"""
    if measurement_condition not in PROCESS_MEASUREMENT_CONDITIONS:
        raise HTTPException(422, "measurement_condition 仅支持空串 / M0 / M1 / M2")
    data = await request.body()
    if not data or len(data) < 128:
        raise HTTPException(400, "缺少有效的 ICC 文件数据（raw bytes body）")
    try:
        meta = ProcessStore.parse_meta(data)
    except Exception as e:  # noqa: BLE001 —— 无效 ICC 文件
        raise HTTPException(400, f"无法解析 ICC 文件: {e}") from e
    doc = process_store.create(
        name,
        data,
        paper,
        ink,
        notes,
        filename,
        measurement_condition,
    )
    return {**doc, "passport_summary": None}


@router.get("")
def list_processes(
    summary: bool = Query(False, description="附带色域摘要与达标率"),
    include_archived: bool = Query(False, description="是否包含已归档工艺（默认 0）"),
):
    """工艺列表。默认仅 active；summary=1 时附带色域摘要与近 20 条配方达标率。"""
    docs = process_store.list(include_archived=include_archived)
    if not summary:
        return docs
    from ..services.recipe import RecipeStore

    recipes = RecipeStore()
    out = []
    for doc in docs:
        item = {**doc}
        item["passport_summary"] = _passport_summary(doc["id"])
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
    doc = {
        **doc,
        "compensation": _compensations.get(pid),
        "passport_summary": _passport_summary(pid),
    }
    return doc


@router.patch("/{pid}")
def update_process(pid: str, req: ProcessUpdateRequest):
    """编辑工艺元数据（名称/纸张/墨水/备注，None 字段保持不变）。"""
    current = process_store.get(pid)
    if not current:
        raise HTTPException(404, "工艺不存在")
    _ensure_active(current)
    if (
        req.measurement_condition is not None
        and req.measurement_condition not in PROCESS_MEASUREMENT_CONDITIONS
    ):
        raise HTTPException(422, "measurement_condition 仅支持空串 / M0 / M1 / M2")
    doc = process_store.update(pid, **req.model_dump())
    if not doc:
        raise HTTPException(404, "工艺不存在")
    return {**doc, "passport_summary": _passport_summary(pid)}


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
    _ensure_active(doc)
    data = await request.body()
    if not data or len(data) < 128:
        raise HTTPException(400, "缺少有效的 ICC 文件数据（raw bytes body）")
    old_space = doc["icc_meta"]["device_space"]
    try:
        process_store.replace_icc(pid, data, filename)
    except Exception as e:  # noqa: BLE001 —— 无效 ICC 文件
        raise HTTPException(400, f"无法解析 ICC 文件: {e}") from e
    # ICC 更换 → 补偿模型即刻失效（新 ICC 墨量行为可能完全不同）
    _compensations.reset(pid)
    new_doc = process_store.get(pid)
    return {
        "status": "replaced",
        "process": new_doc,
        "space_changed": new_doc["icc_meta"]["device_space"] != old_space,
    }


@router.delete("/{pid}")
def delete_process(
    pid: str,
    reason: str = Query("manual_archive", description="归档原因"),
    actor: str = Query("", description="操作人"),
):
    """归档工艺（V0.11 起 DELETE 不再硬删除；证据保留、只读）。"""
    doc = process_store.get(pid)
    if not doc:
        raise HTTPException(404, "工艺不存在")
    updated = process_store.archive(pid, reason, actor)
    return {"status": "archived", "process": updated}


@router.post("/{pid}/archive")
def archive_process(pid: str, req: ProcessArchiveRequest | None = None):
    """归档工艺（可逆）。已归档时幂等返回。"""
    if not process_store.get(pid):
        raise HTTPException(404, "工艺不存在")
    request = req or ProcessArchiveRequest()
    updated = process_store.archive(pid, request.reason, request.actor)
    return {"status": "archived", "process": updated}


@router.post("/{pid}/restore")
def restore_process(pid: str):
    """恢复已归档工艺为 active。"""
    try:
        updated = process_store.restore(pid)
    except KeyError as e:
        raise HTTPException(404, "工艺不存在") from e
    return {"status": "restored", "process": updated}


@router.delete("/{pid}/permanent")
def permanent_delete_process(
    pid: str,
    confirm: str = Query("", description="必须等于工艺 id 以确认永久清理"),
):
    """永久清理已归档工艺：删除派生记录，解绑测量/评价，签样标记 orphaned。"""
    doc = process_store.get(pid)
    if not doc:
        raise HTTPException(404, "工艺不存在")
    if confirm != pid:
        raise HTTPException(422, f"确认参数不匹配：需 confirm={pid}")
    if not is_archived(doc):
        raise HTTPException(
            409,
            detail={
                "code": ProcessNotArchivedError.code,
                "message": ProcessNotArchivedError.message,
            },
        )
    try:
        result = permanently_delete_process(doc)
    except ProcessNotArchivedError as e:
        raise HTTPException(
            409,
            detail={
                "code": ProcessNotArchivedError.code,
                "message": ProcessNotArchivedError.message,
            },
        ) from e
    except KeyError as e:
        raise HTTPException(404, "工艺不存在") from e
    return result


@router.get("/{pid}/dependencies")
def process_dependencies(pid: str):
    """工艺证据依赖计数与永久清理 blockers。"""
    doc = process_store.get(pid)
    if not doc:
        raise HTTPException(404, "工艺不存在")
    return dependency_report(doc)


@router.delete("/{pid}/compensation")
def reset_compensation(pid: str):
    """重置工艺级误差补偿模型（换纸张/墨水等场景下人工干预）。"""
    doc = process_store.get(pid)
    if not doc:
        raise HTTPException(404, "工艺不存在")
    _ensure_active(doc)
    _compensations.reset(pid)
    return {"status": "reset"}


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


@router.get("/{pid}/gamut/compare")
def gamut_compare(pid: str, other: str = Query(..., description="对比工艺 ID"), levels: int = 6):
    """双工艺色域对比：xy 凸包叠加归属 + 交集多边形与近似面积。"""
    doc = process_store.get(pid)
    if not doc:
        raise HTTPException(404, "工艺不存在")
    other_doc = process_store.get(other)
    if not other_doc:
        raise HTTPException(404, "对比工艺不存在")
    if other == pid:
        raise HTTPException(400, "对比工艺不能与自身相同")
    from ..services.processes import compare_gamuts

    try:
        return compare_gamuts(doc, other_doc, levels=max(3, min(levels, 10)))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"色域对比失败: {e}") from e
