"""QC 测量可靠性闭环路由（V0.5 · M1，V0.6 诊断/对比/评价扩展）。

会话登记 → 导入解析 → 五条可靠性规则分级 → 分级数据流向。
- POST /api/qc/measurements/import   导入测量文件（raw bytes，一次一个文件）
- GET  /api/qc/measurements          会话列表 / 指定会话详情（?session_id=）
- DELETE /api/qc/measurements        删除会话 / 删除单条记录（?session_id=&record_id=）
- GET  /api/qc/ink-quality           油墨 QC 指标 + 诊断结论（?session_id=）
- GET  /api/qc/compare               双会话指标 Δ 表
- GET  /api/qc/trend                 工艺绑定会话时间序列
- POST /api/qc/evaluations           创建主观评价（GB/T 34053 方法论）
- POST /api/qc/evaluations/entries   录入评价人打分（JSON body）
- GET  /api/qc/evaluations           评价列表 / 详情（含统计）
- DELETE /api/qc/evaluations         删除评价
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from ..services.measurements import MEASUREMENT_CONDITIONS, MeasurementStore
from ..services.processes import ProcessStore

router = APIRouter(prefix="/api/qc", tags=["qc"])

measurements = MeasurementStore()
_processes = ProcessStore()


def _process_or_none(process_id: str) -> dict | None:
    if not process_id:
        return None
    doc = _processes.get(process_id)
    if doc is None:
        raise HTTPException(404, "绑定的工艺不存在")
    return doc


@router.post("/measurements/import")
async def import_measurement(
    request: Request,
    filename: str = Query("", description="测量文件名"),
    session_id: str = Query("", description="已有会话 id（留空则先创建会话）"),
    instrument: str = Query("", description="仪器（新建会话时必填）"),
    condition: str = Query("M1", description="测量条件 M0/M1/M2"),
    calibrated_at: str = Query("", description="校准时间 ISO 字符串"),
    backing: str = Query("", description="背衬（黑/白）"),
    repeat_mode: str = Query("", description="single=单次测量，repeat=复测组；留空沿用会话设置（新建会话默认 single）"),
    process_id: str = Query("", description="绑定工艺（用于纸白比对）"),
    notes: str = Query("", description="备注"),
):
    """导入测量导出文件（body 为文件原始字节）。

    首次导入可留空 session_id，响应携带会话 id 供后续文件追加；
    每次导入后对整个会话重新分级（复测一致性/离群是会话级属性）。
    """
    data = await request.body()
    if not data:
        raise HTTPException(400, "缺少测量文件内容（raw bytes body）")
    if len(data) > 2_000_000:
        raise HTTPException(413, "测量文件过大（>2MB）")

    proc_doc = _process_or_none(process_id)
    if session_id:
        session = measurements.get_session(session_id)
        if session is None:
            raise HTTPException(404, "测量会话不存在")
        # 追加导入时允许更新复测声明（会话级属性，改后整体重分级）
        if repeat_mode and repeat_mode != session.get("repeat_mode"):
            measurements.update_session(session_id, repeat_mode=repeat_mode)
    else:
        if not instrument.strip():
            raise HTTPException(400, "新建会话需提供 instrument（仪器登记）")
        if not calibrated_at.strip():
            raise HTTPException(400, "新建会话需提供 calibrated_at（校准确认，方法闭环第 2 步）")
        session = measurements.create_session(
            instrument, condition, calibrated_at, backing, repeat_mode or "single", process_id, notes)
        session_id = session["id"]

    from ..services.cgats import CgatsParseError

    try:
        record = measurements.add_record(session_id, filename, data, proc_doc)
    except CgatsParseError as e:
        raise HTTPException(422, f"测量文件解析失败: {e}") from e
    session = measurements.get_session(session_id)
    return {
        "session": session,
        "record": {
            "id": record["id"],
            "filename": record["filename"],
            "kind": record["kind"],
            "patch_count": record["patch_count"],
            "reliability": record["reliability"],
            "density_ok": record["density_ok"],
            "reasons": record["reasons"],
        },
    }


@router.get("/measurements")
def list_measurements(
    session_id: str = Query("", description="留空返回会话列表；传入返回会话详情（含记录）"),
    reliability: str = Query("", description="按可靠性过滤记录：verified/warning/rejected"),
):
    if session_id:
        session = measurements.get_session(session_id)
        if session is None:
            raise HTTPException(404, "测量会话不存在")
        records = list(session.get("records", {}).values())
        if reliability:
            records = [r for r in records if r["reliability"] == reliability]
        out = {k: v for k, v in session.items() if k != "records"}
        out["records"] = records
        return out
    return measurements.list_sessions()


@router.get("/ink-quality")
def ink_quality(
    session_id: str = Query(..., description="测量会话 id（仅消费 verified 记录）"),
):
    """油墨质量四指标 + TVI（GATF 公式；只消费 verified 且密度通道可用的记录）。

    warning/rejected 记录被排除并计数返回——数据未达 verified 时前端显示方法要求占位。
    """
    session = measurements.get_session(session_id)
    if session is None:
        raise HTTPException(404, "测量会话不存在")
    from ..services.density import ink_quality_from_records

    records = list(session.get("records", {}).values())
    result = ink_quality_from_records(records)
    result["session_id"] = session_id
    result["session"] = {k: session.get(k) for k in ("instrument", "condition", "calibrated_at", "repeat_mode")}

    # V0.6：规则型诊断结论（R1-R8，行业标准口径）
    from ..services.diagnosis import diagnose

    result["diagnosis"] = diagnose(result)
    return result


@router.get("/compare")
def compare_sessions(
    session_id_a: str = Query(..., description="主会话 id"),
    session_id_b: str = Query(..., description="对比会话 id"),
):
    """双会话 QC 指标 Δ 表（delta = 主会话 − 对比会话）。

    覆盖反差率 K、中间调 TVI、色强度、灰度与叠印率，供趋势排查与复测回归。
    """
    from ..services.density import ink_quality_from_records
    from ..services.qc_compare import compare_sessions

    def _ink(sid: str) -> dict:
        s = measurements.get_session(sid)
        if s is None:
            raise HTTPException(404, f"测量会话不存在: {sid}")
        return ink_quality_from_records(list(s.get("records", {}).values()))

    result = compare_sessions(_ink(session_id_a), _ink(session_id_b))
    result["session_a"] = session_id_a
    result["session_b"] = session_id_b
    return result


@router.get("/trend")
def qc_trend(
    process_id: str = Query(..., description="工艺 id"),
):
    """工艺绑定的测量会话时间序列（每会话现场计算指标，不落库）。

    落地「定期抽检 75% 网点密度与反差率，监控暗调层次变化」的运营闭环。
    """
    from ..services.qc_compare import trend_for_process

    proc = _processes.get(process_id)
    if proc is None:
        raise HTTPException(404, "工艺不存在")
    return {"process": {"id": proc["id"], "name": proc.get("name", "")},
            "points": trend_for_process(measurements, process_id)}


@router.get("/evaluations/templates")
def evaluation_templates():
    """主观评价模板与打分制说明（供前端构建打分表单）。"""
    from ..services.evaluations import SCORE_SCALE, SCORE_STD_WARN, TEMPLATES

    return {"templates": TEMPLATES, "score_scale": SCORE_SCALE, "std_warn": SCORE_STD_WARN}


@router.post("/evaluations")
def create_evaluation(
    template: str = Query(..., description="评价模板 book/paper_pack/plastic_pack"),
    title: str = Query("", description="评价标题（留空用模板名）"),
    process_id: str = Query("", description="绑定工艺（可选）"),
):
    """创建主观评价检查单（GB/T 34053 方法论：多评价人独立 5 级打分）。"""
    from ..services.evaluations import EvaluationStore

    proc_doc = _process_or_none(process_id) if process_id else None
    store = EvaluationStore()
    try:
        doc = store.create_evaluation(template, title, process_id)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    doc["process"] = {"id": proc_doc["id"], "name": proc_doc.get("name", "")} if proc_doc else None
    return doc


@router.post("/evaluations/entries")
async def add_evaluation_entry(
    request: Request,
    evaluation_id: str = Query(..., description="评价 id"),
    evaluator: str = Query(..., description="评价人姓名"),
):
    """录入一位评价人的打分（JSON body：{指标: 1-5, ...}）。"""
    from ..services.evaluations import EvaluationStore

    try:
        scores = await request.json()
    except Exception as e:
        raise HTTPException(422, "打分 body 需为 JSON 对象 {指标: 1-5}") from e
    if not isinstance(scores, dict):
        raise HTTPException(422, "打分 body 需为 JSON 对象 {指标: 1-5}")
    store = EvaluationStore()
    try:
        doc = store.add_entry(evaluation_id, evaluator, scores)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(422, str(e)) from e

    from ..services.evaluations import evaluation_stats

    return {**doc, "stats": evaluation_stats(doc)}


@router.get("/evaluations")
def list_evaluations(
    evaluation_id: str = Query("", description="留空返回列表；传入返回详情（含统计）"),
):
    from ..services.evaluations import EvaluationStore, evaluation_stats

    store = EvaluationStore()
    if evaluation_id:
        doc = store.get(evaluation_id)
        if doc is None:
            raise HTTPException(404, "评价不存在")
        return {**doc, "stats": evaluation_stats(doc)}
    out = []
    for doc in store.list_evaluations():
        out.append({"id": doc["id"], "template": doc["template"], "template_name": doc["template_name"],
                    "title": doc["title"], "process_id": doc.get("process_id", ""),
                    "created_at": doc.get("created_at", ""), "entries": len(doc.get("entries") or []),
                    "stats": evaluation_stats(doc)})
    return out


@router.delete("/evaluations")
def delete_evaluation(
    evaluation_id: str = Query(..., description="评价 id"),
):
    from ..services.evaluations import EvaluationStore

    if not EvaluationStore().delete_evaluation(evaluation_id):
        raise HTTPException(404, "评价不存在")
    return {"status": "deleted"}


@router.post("/testform")
def make_testform(
    process_id: str = Query(..., description="工艺 id（页眉标注工艺名）"),
    dpi: int = Query(300, ge=150, le=600, description="输出分辨率"),
):
    """生成 A4 CMYK 验证样张（设备值直出 + 双点重复布局）。

    返回 PDF（打印用）+ TIFF（工艺流程回传）+ PNG 预览 + 双点配对坐标表。
    """
    proc = _processes.get(process_id)
    if proc is None:
        raise HTTPException(404, "工艺不存在")
    from ..services.testform import generate_testform

    result = generate_testform(process_name=proc.get("name", ""), dpi=dpi)
    result["process"] = {"id": proc["id"], "name": proc.get("name", "")}
    return result


@router.delete("/measurements")
def delete_measurement(
    session_id: str = Query(..., description="会话 id"),
    record_id: str = Query("", description="留空删除整个会话；传入仅删除单条记录"),
    process_id: str = Query("", description="绑定工艺（重分级用）"),
):
    proc_doc = _process_or_none(process_id)
    if record_id:
        if not measurements.delete_record(session_id, record_id, proc_doc):
            raise HTTPException(404, "测量记录不存在")
        session = measurements.get_session(session_id)
        return {"status": "deleted", "session": session}
    if not measurements.delete_session(session_id):
        raise HTTPException(404, "测量会话不存在")
    return {"status": "deleted"}
