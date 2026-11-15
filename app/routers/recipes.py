"""配方预测路由：LAB→CMYK 反查与实测验证闭环。"""

from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..models.schemas import (
    RecipePredictRequest,
    RecipePredictResponse,
    RecipeResult,
    RecipeVerifyRequest,
    RecipeVerifyResponse,
)
from ..services.processes import ProcessStore
from ..services.recipe import RecipeStore, forward_query, predict_recipe, run_verification

router = APIRouter(prefix="/api/recipes", tags=["recipes"])

recipe_store = RecipeStore()
_processes = ProcessStore()


@router.post("/predict", response_model=RecipePredictResponse)
def predict(req: RecipePredictRequest):
    proc = _processes.get(req.process_id)
    if not proc:
        raise HTTPException(404, "工艺不存在，请先在工艺库中创建")

    icc_bytes = _processes.icc_bytes(req.process_id)
    device_space = proc["icc_meta"]["device_space"]

    started = time.perf_counter()
    results: list[RecipeResult] = []
    total_predictions = 0
    for t in req.targets:
        r = predict_recipe(
            icc_bytes,
            device_space,
            t.lab,
            tolerance=req.tolerance,
            method=req.method.value,
            max_rounds=req.max_rounds,
        )
        r["method"] = req.method.value
        r["tolerance"] = req.tolerance
        r["label"] = t.label
        total_predictions += r["predictions"]
        results.append(RecipeResult(**r))
        # 每个目标存一条配方历史
        recipe_store.create(proc, {**r, "method": req.method.value, "tolerance": req.tolerance}, t.label)

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return RecipePredictResponse(
        process_id=req.process_id,
        process_name=proc["name"],
        device_space=device_space,
        results=results,
        total_predictions=total_predictions,
        elapsed_ms=elapsed_ms,
    )


class ForwardQueryRequest(BaseModel):
    process_id: str
    values: list[float] = Field(..., description="墨量百分比列表（按工艺通道数，如 CMYK 4 项）")


class ForwardQueryResponse(BaseModel):
    values: dict[str, float]
    lab: list[float]
    hex: str


@router.post("/forward", response_model=ForwardQueryResponse)
def forward(req: ForwardQueryRequest):
    """正向查询：墨量配方 → 预测 Lab（与反查引擎共用同一变换）。"""
    proc = _processes.get(req.process_id)
    if not proc:
        raise HTTPException(404, "工艺不存在")
    channels = proc["icc_meta"]["channels"]
    if len(req.values) != channels:
        raise HTTPException(400, f"该工艺需要 {channels} 个通道值（{proc['icc_meta']['device_space']}）")
    icc_bytes = _processes.icc_bytes(req.process_id)
    return ForwardQueryResponse(**forward_query(icc_bytes, proc["icc_meta"]["device_space"], req.values))


@router.get("")
def list_recipes(process_id: str | None = None):
    return recipe_store.list(process_id)


@router.get("/{rid}")
def get_recipe(rid: str):
    doc = recipe_store.get(rid)
    if not doc:
        raise HTTPException(404, "配方记录不存在")
    return doc


@router.delete("/{rid}")
def delete_recipe(rid: str):
    if not recipe_store.delete(rid):
        raise HTTPException(404, "配方记录不存在")
    return {"status": "deleted"}


@router.post("/verify", response_model=RecipeVerifyResponse)
def verify(req: RecipeVerifyRequest):
    doc = recipe_store.get(req.recipe_id)
    if not doc:
        raise HTTPException(404, "配方记录不存在")

    proc = _processes.get(doc["process_id"])
    if not proc:
        raise HTTPException(404, "配方所属工艺已被删除")

    icc_bytes = _processes.icc_bytes(doc["process_id"])
    verification = run_verification(
        icc_bytes,
        doc["device_space"],
        doc,
        req.measured_lab,
        tolerance=doc.get("tolerance", 2.0),
        method=doc.get("method", "cie2000"),
    )
    recipe_store.append_verification(req.recipe_id, verification)
    return RecipeVerifyResponse(
        recipe_id=req.recipe_id,
        delta_e_measured=verification["delta_e_measured"],
        delta_e_model_error=verification["delta_e_model_error"],
        delta_e_corrected=verification["delta_e_corrected"],
        corrected_recipe=verification["corrected_recipe"],
        corrected_predicted_lab=verification["corrected_predicted_lab"],
        improved=verification["improved"],
        verification=verification,
    )
