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
from ..services.compensation import CompensationStore
from ..services.processes import ProcessStore
from ..services.recipe import (
    CHANNEL_NAMES,
    RecipeStore,
    forward_query,
    predict_recipe,
    run_verification,
)

router = APIRouter(prefix="/api/recipes", tags=["recipes"])

recipe_store = RecipeStore()
_processes = ProcessStore()
_compensations = CompensationStore()


@router.post("/predict", response_model=RecipePredictResponse)
def predict(req: RecipePredictRequest):
    proc = _processes.get(req.process_id)
    if not proc:
        raise HTTPException(404, "工艺不存在，请先在工艺库中创建")

    icc_bytes = _processes.icc_bytes(req.process_id)
    device_space = proc["icc_meta"]["device_space"]

    # 工艺级补偿模型：sha 匹配且样本达标时作用于种子
    bias = _compensations.bias_for_predict(req.process_id, proc["icc_sha256"])
    comp_doc = _compensations.get(req.process_id)
    comp_n = int(comp_doc["n"]) if comp_doc else 0

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
            device_bias=bias,
        )
        r["method"] = req.method.value
        r["tolerance"] = req.tolerance
        r["label"] = t.label
        r["compensation"] = {"n": comp_n, "applied": bias is not None}
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


def _percentile95(vals: list[float]) -> float | None:
    if not vals:
        return None
    s = sorted(vals)
    k = 0.95 * (len(s) - 1)
    f = int(k)
    c = min(f + 1, len(s) - 1)
    return round(s[f] + (k - f) * (s[c] - s[f]), 3)


def _agg_rows(rows: list[dict]) -> dict:
    """对一组配方记录聚合 n / mean / p95 / 达标率。rows 为时间降序。"""
    des = [float(d["delta_e"]) for d in rows]
    n = len(rows)
    return {
        "n": n,
        "mean_de": round(sum(des) / n, 3) if n else None,
        "p95_de": _percentile95(des),
        "pass_rate": round(sum(1 for d in rows if d.get("converged")) / n, 4) if n else None,
    }


@router.get("/stats")
def recipes_stats(process_id: str | None = None):
    """ΔE 监控聚合：总量/达标率/均值/P95 + 时间序列 + 分工艺漂移告警。"""
    docs = recipe_store.list(process_id)  # 时间降序
    total = len(docs)
    overall = _agg_rows(docs)
    series = [
        {
            "id": d["id"],
            "created_at": d.get("created_at", ""),
            "delta_e": d["delta_e"],
            "converged": bool(d.get("converged")),
            "process_id": d.get("process_id"),
            "process_name": d.get("process_name", ""),
            "label": d.get("label", ""),
            "tolerance": d.get("tolerance", 2.0),
        }
        for d in reversed(docs)  # 时间升序，前端画趋势线
    ]

    # 分工艺聚合与漂移检测（最近 5 条 vs 前 5 条）
    by_proc: dict[str, list[dict]] = {}
    for d in docs:
        by_proc.setdefault(d.get("process_id", ""), []).append(d)
    processes = []
    flags = []
    for pid, rows in by_proc.items():
        agg = _agg_rows(rows)
        agg.update(
            process_id=pid,
            process_name=rows[0].get("process_name", ""),
            drift=None,
            drift_flag=False,
        )
        if agg["n"] >= 10:
            tol = max(float(r.get("tolerance", 2.0)) for r in rows)
            last5 = [float(r["delta_e"]) for r in rows[:5]]
            prev5 = [float(r["delta_e"]) for r in rows[5:10]]
            drift = round(sum(last5) / 5 - sum(prev5) / 5, 3)
            agg["drift"] = drift
            drop = (agg["pass_rate"] or 0) - (
                round(sum(1 for r in rows[5:10] if r.get("converged")) / 5, 4)
            )
            if drift > max(0.3, 0.25 * tol) or drop > 0.25:
                agg["drift_flag"] = True
                flags.append(
                    {
                        "process_id": pid,
                        "process_name": agg["process_name"],
                        "severity": "warn",
                        "message": f"ΔE 漂移 +{drift}（近 5 次均值 ↑），建议复检工艺状态",
                    }
                )
        processes.append(agg)
    processes.sort(key=lambda x: -x["n"])

    return {
        "total": total,
        "converged_rate": overall["pass_rate"],
        "mean_de": overall["mean_de"],
        "p95_de": overall["p95_de"],
        "series": series,
        "processes": processes,
        "flags": flags,
    }


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

    # 工艺级补偿模型：修正有效时把「修正配方 − 原配方」累积进设备空间偏置
    if verification["improved"]:
        channels = proc["icc_meta"]["channels"]
        names = CHANNEL_NAMES.get(channels, [])
        if names:
            delta_device = [
                (verification["corrected_recipe"].get(n, 0.0) - doc["recipe"].get(n, 0.0)) / 100.0
                for n in names
            ]
            _compensations.accumulate(doc["process_id"], proc["icc_sha256"], delta_device)

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
