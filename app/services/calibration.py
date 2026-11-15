"""印刷校准闭环（V0.7）：双基准差距报告 → 墨量建议/校正曲线 → 补偿联动 → 特性数据导出。

双基准：
- 工艺 ICC 预期 Lab（模型误差口径 → 驱动墨量修正与校正曲线）；
- ISO 12647-2:2013 涂布纸（type 1/2）固体 Lab 参考值（绝对达标口径，仅涂布）。

修正方法学（ISO 10128:2009 校正曲线法的 Lab 口径变体）：
- 曲线：有效网点反演——实测色块 Lab 沿单色轴反查 ICC 等效设备值 e(t)
  （「设备印 t 相当于 ICC 里的 e(t)」），校正曲线 t→e⁻¹(t)；
- 实地：模型误差补偿 p' = 2·预期 − 实测（与 recipe.run_verification 同式）
  后沿单色轴反查修正设备值 → 逐通道墨量建议（写入补偿模型）。

数据政策：行业常量均以 #: 注明出处；不使用任何真实课程测量数据。
"""

from __future__ import annotations

import numpy as np

from ..algorithms.core import delta_e, delta_e_batch, delta_e_components
from .measurement_qa import effective_lab
from .measurements import process_doc_icc_bytes
from .recipe import _build_forward
from .testform import CAL_INDEX_MAP, CAL_PATCHES

# ---------------------------------------------------------------------------
# 行业常量（出处见 docs/V0.7-实施规划.md §3）
# ---------------------------------------------------------------------------

#: ISO 12647-2:2013 涂布纸（type 1/2）固体 Lab 参考值，D50/2°；
#: 非涂布（type 4/5）公开来源数值无法交叉确认，按数据政策不收录（iso 列返回 None）
ISO_SOLIDS_COATED = {
    "C": (55.0, -37.0, -50.0),
    "M": (48.0, 74.0, -3.0),
    "Y": (89.0, -5.0, 93.0),
    "K": (16.0, 0.0, 0.0),
    "CMY": (23.0, 0.0, 0.0),
}
#: ISO 12647-2:2013 固体允差（ΔE00）：CMY ≤3.5、K ≤5.0
ISO_TOL_SOLID = {"C": 3.5, "M": 3.5, "Y": 3.5, "K": 5.0}
#: ISO 12647-7:2016 控制条允差精神：单体 ≤5.0、平均 ≤2.5（ICC 数据集口径）
WEDGE_DE_MAX = 5.0
WEDGE_DE_AVG = 2.5
#: 反查可达性提示线：模型侧反查后仍达不到的 ΔE 视为超出墨量可修正范围
BEYOND_RANGE_DE = 10.0
#: 单色轴反查网格：0.25% 步长
_AXIS_GRID_N = 401

#: 纸张字段 → ISO 纸类映射（匹配任一关键词即归类）
PAPER_COATED_KEYS = ("铜版", "涂布", "哑粉", "coated", "art")
PAPER_UNCOATED_KEYS = ("胶版", "非涂布", "轻型", "蒙肯", "uncoated", "offset", "news")

OVERALL_LABELS = {"ok": "达标", "suggest": "建议校准", "needed": "需校准", "n/a": "无校准数据"}


def paper_class(paper: str) -> str | None:
    """工艺 paper 字段 → "coated" / "uncoated" / None（无法归类）。"""
    text = (paper or "").lower()
    if any(k in text for k in PAPER_COATED_KEYS):
        return "coated"
    if any(k in text for k in PAPER_UNCOATED_KEYS):
        return "uncoated"
    return None


def _iso_reference(pc: str | None) -> dict[str, tuple[float, float, float]] | None:
    """按纸类返回固体参考值表；uncoated/未分类返回 None（数据政策：不收录未确认数值）。"""
    if pc == "coated":
        return ISO_SOLIDS_COATED
    return None


# ---------------------------------------------------------------------------
# 参考值（M1）：样张色块 → 预期 Lab（ICC 批量正查）+ ISO 参考列
# ---------------------------------------------------------------------------

def build_reference(process_doc: dict) -> dict:
    """校准版 28 色块的预期值参考表。

    expected_lab 来自工艺 ICC 批量正查（相对色度 intent，与配方预测同一正向模型）；
    iso_ref_lab 仅涂布纸的 C/M/Y/K/CMY 实地有色值。
    """
    forward = _build_forward(process_doc_icc_bytes(process_doc), 4)
    values = np.asarray([p["device"] for p in CAL_PATCHES], dtype=np.float64) / 100.0
    labs = forward(values)
    pc = paper_class(process_doc.get("paper", ""))
    iso = _iso_reference(pc)
    patches = []
    for p, lab in zip(CAL_PATCHES, labs):
        # ISO 参考表只覆盖固体（C100/M100/Y100/K100）与 CMY 三色叠印——
        # 梯级无 Lab 参考（ISO 12647-2 对网点梯级定义的是 TVI 曲线，不是绝对 Lab）
        iso_key = _iso_solid_key(p["label"])
        patches.append({
            "index": p["index"],
            "label": p["label"],
            "device": p["device"],
            "expected_lab": [round(float(v), 2) for v in lab],
            "iso_ref_lab": (list(iso[iso_key]) if iso and iso_key and iso_key in iso else None),
        })
    return {
        "paper_class": pc,
        "iso_available": iso is not None,
        "icc_sha256": process_doc.get("icc_sha256", ""),
        "patches": patches,
        # 预期值参考表（设备值 + ICC 预期 Lab）——测量时对照 / 外部软件核对用
        "reference_cgats": _cgats_text(
            f"ColorWorkshop calibration reference (ICC expected Lab) {process_doc.get('name', '')}",
            [(p["index"], p["label"], [float(v) for v in p["device"]], p["expected_lab"])
             for p in patches],
        ),
    }


def _iso_solid_key(label: str) -> str | None:
    """色块标签 → ISO 固体参考键：仅 100% 固体（C100→C）与 CMY 叠印，其余 None。"""
    if label == "CMY":
        return "CMY"
    for name in "CMYK":
        if label == f"{name}100":
            return name
    return None


# ---------------------------------------------------------------------------
# 反查工具：单色轴最近 Lab 搜索
# ---------------------------------------------------------------------------

def _axis_search(forward, ch: int, target_lab) -> tuple[float, float]:
    """沿第 ch 通道单色轴（其余通道 0）细网格搜索，返回 (设备值%, ΔE00)。"""
    grid = np.linspace(0.0, 1.0, _AXIS_GRID_N)
    vals = np.zeros((_AXIS_GRID_N, 4))
    vals[:, ch] = grid
    des = delta_e_batch(forward(vals), np.asarray(target_lab, dtype=np.float64))
    i = int(np.argmin(des))
    return float(grid[i]) * 100.0, float(des[i])


def _clip_lab(lab) -> list[float]:
    return [float(np.clip(lab[0], 0.0, 100.0)),
            float(np.clip(lab[1], -128.0, 128.0)),
            float(np.clip(lab[2], -128.0, 128.0))]


# ---------------------------------------------------------------------------
# 校准报告（M2）：逐 patch 双基准 Δ + 逐通道建议 + 校正曲线 + 导出文本
# ---------------------------------------------------------------------------

def _norm_label(raw: str) -> str:
    """测量标签归一：P 编号与色块标签等价，大小写不敏感。"""
    text = str(raw or "").strip().upper()
    return CAL_INDEX_MAP.get(text, text)


def _aggregate_measured(session: dict) -> dict[str, list[list[float]]]:
    """verified 记录按校准标签聚合实测 Lab（双点重复即跨记录均值）。"""
    acc: dict[str, list[list[float]]] = {}
    for rec in (session or {}).get("records", {}).values():
        if rec.get("reliability") != "verified":
            continue
        for pt in rec.get("patches", []):
            label = _norm_label(pt.get("label"))
            lab = effective_lab(pt)
            if lab:
                acc.setdefault(label, []).append([float(v) for v in lab])
    return acc


def calibration_report(process_doc: dict, session: dict) -> dict:
    """双基准校准报告。

    overall 判定（docs §3.4）：任一固体超 ISO 允差或 max(ΔE_icc)>5.0 → needed；
    avg(ΔE_icc)>2.5 → suggest；命中 <4 patch → n/a；否则 ok。
    """
    forward = _build_forward(process_doc_icc_bytes(process_doc), 4)
    ref = build_reference(process_doc)
    expected_by_label = {p["label"]: p["expected_lab"] for p in ref["patches"]}
    iso_by_label = {p["label"]: p["iso_ref_lab"] for p in ref["patches"] if p["iso_ref_lab"]}
    known = {p["label"]: p for p in CAL_PATCHES}

    acc = _aggregate_measured(session)
    matched = {k: v for k, v in acc.items() if k in known}

    rows = []
    for p in CAL_PATCHES:
        labs = matched.get(p["label"])
        if not labs:
            continue
        measured = np.mean(np.asarray(labs), dtype=np.float64, axis=0)
        expected = expected_by_label[p["label"]]
        iso_lab = iso_by_label.get(p["label"])
        iso_tol = (ISO_TOL_SOLID.get(_iso_solid_key(p["label"]) or "", WEDGE_DE_MAX)
                   if iso_lab else None)
        row = {
            "index": p["index"],
            "label": p["label"],
            "device": p["device"],
            "n": len(labs),
            "measured_lab": [round(float(v), 2) for v in measured],
            "expected_lab": expected,
            "iso_ref_lab": iso_lab,
            "de_expected": round(float(delta_e(measured, expected)), 2),
            "comps": delta_e_components(expected, measured),
            "iso_tol": iso_tol if iso_lab else None,
            "de_iso": (round(float(delta_e(measured, iso_lab)), 2) if iso_lab else None),
            "pass_iso": (bool(delta_e(measured, iso_lab) <= iso_tol) if iso_lab else None),
        }
        rows.append(row)

    de_icc = [r["de_expected"] for r in rows]
    solid_iso_fail = [r["label"] for r in rows if r["pass_iso"] is False]
    max_de = max(de_icc) if de_icc else None
    avg_de = round(float(np.mean(de_icc)), 2) if de_icc else None
    if len(rows) < 4:
        overall = "n/a"
    elif solid_iso_fail or (max_de is not None and max_de > WEDGE_DE_MAX):
        overall = "needed"
    elif avg_de is not None and avg_de > WEDGE_DE_AVG:
        overall = "suggest"
    else:
        overall = "ok"

    report = {
        "paper_class": ref["paper_class"],
        "iso_available": ref["iso_available"],
        "patch_count_total": len(CAL_PATCHES),
        "matched": len(rows),
        "overall": overall,
        "overall_label": OVERALL_LABELS[overall],
        "summary": {
            "max_de_expected": max_de,
            "avg_de_expected": avg_de,
            "wedge_max_ok": bool(max_de is not None and max_de <= WEDGE_DE_MAX),
            "wedge_avg_ok": bool(avg_de is not None and avg_de <= WEDGE_DE_AVG),
            "solid_iso_fail": solid_iso_fail,
        },
        "patches": rows,
    }

    report["ink_suggestions"] = _ink_suggestions(forward, matched, expected_by_label)
    report["curves"] = _correction_curves(forward, matched)
    report["exports"] = _build_exports(process_doc, report, expected_by_label, matched)
    return report


def _ink_suggestions(forward, matched: dict, expected_by_label: dict) -> list[dict]:
    """逐通道墨量建议：由各自 100% 固体的模型误差补偿反查（单色轴）。"""
    out = []
    for ch, name in enumerate("CMYK"):
        label = f"{name}100"
        labs = matched.get(label)
        if not labs:
            out.append({"channel": name, "available": False, "delta_pct": None,
                        "reason": "缺 100% 固体实测"})
            continue
        measured = np.mean(np.asarray(labs), dtype=np.float64, axis=0)
        expected = np.asarray(expected_by_label[label], dtype=np.float64)
        adjusted = _clip_lab(2.0 * expected - measured)
        v_opt, ach = _axis_search(forward, ch, adjusted)
        delta = v_opt - 100.0
        out.append({
            "channel": name,
            "available": True,
            "measured_lab": [round(float(v), 2) for v in measured],
            "expected_lab": [round(float(v), 2) for v in expected],
            "adjusted_target_lab": [round(v, 2) for v in adjusted],
            "corrected_pct": round(v_opt, 1),
            "delta_pct": round(delta, 1),
            "achievable_de": round(ach, 2),
            "beyond_range": bool(ach > BEYOND_RANGE_DE),
        })
    return out


def _correction_curves(forward, matched: dict) -> dict[str, list[dict]]:
    """校正曲线（ISO 10128 校正曲线法，Lab 有效网点反演口径）。

    梯级 t 的有效网点 e(t)=轴反查(实测 Lab)；校正后设备值 t_corr = e⁻¹(t)；
    端点 0→0、100→100 固定（实地偏差交给墨量建议，不进曲线）。
    """
    curves: dict[str, list[dict]] = {}
    tint_steps = (10, 25, 50, 75)
    for ch, name in enumerate("CMYK"):
        eff: list[tuple[float, float]] = []
        for t in tint_steps:
            labs = matched.get(f"{name}{t}")
            if not labs:
                continue
            measured = np.mean(np.asarray(labs), dtype=np.float64, axis=0)
            e_val, _ = _axis_search(forward, ch, measured)
            eff.append((float(t), float(np.clip(e_val, 0.0, 100.0))))
        if not eff:
            continue
        # 反演：发送 e⁻¹(t) 使实际输出落在 ICC 预期上——在名义网点 t 处取逆
        # （x=有效网点，须严格递增；重复有效网点取首个名义值）
        by_e: dict[float, float] = {}
        for t, e in eff:
            by_e.setdefault(round(e, 2), t)
        pairs = sorted(by_e.items())
        es = [0.0] + [e for e, _ in pairs] + [100.0]
        ts = [0.0] + [t for _, t in pairs] + [100.0]
        entries = [{"t": 0.0, "effective": None, "corrected": 0.0}]
        for t, e in eff:
            t_corr = float(np.clip(np.interp(t, es, ts), 0.0, 100.0))
            entries.append({"t": float(t), "effective": round(e, 1),
                            "corrected": round(t_corr, 1)})
        entries.append({"t": 100.0, "effective": None, "corrected": 100.0})
        curves[name] = entries
    return curves


def _corrected_device(device: list[int], curves: dict, bias_pct: list[float]) -> list[float]:
    """校正后设备值：实地 100 → 100+bias；中间调 → 曲线插值；0 → 0。"""
    out = []
    for ch_i, v in enumerate(device):
        name = "CMYK"[ch_i]
        if v <= 0:
            out.append(0.0)
        elif v >= 100:
            out.append(round(float(np.clip(100.0 + bias_pct[ch_i], 0.0, 100.0)), 1))
        else:
            entries = curves.get(name) or []
            xs = [e["t"] for e in entries]
            ys = [e["corrected"] for e in entries]
            out.append(round(float(np.interp(v, xs, ys)), 1) if entries else float(v))
    return out


# ---------------------------------------------------------------------------
# 导出文本（服务端组装，前端 downloadDataUri 直接下载）
# ---------------------------------------------------------------------------

def _cgats_text(title: str, rows: list[tuple[str, str, list[float], list[float]]]) -> str:
    """CGATS.17 兼容文本（设备四列 + Lab 三列——ArgyllCMS txt2ti3 / 专业打样软件可直接读取）。"""
    lines = [
        f'CTITL "{title}"',
        "NUMBER_OF_FIELDS\t9",
        "BEGIN_DATA_FORMAT",
        "SAMPLE_ID\tSAMPLE_NAME\tCMYK_C\tCMYK_M\tCMYK_Y\tCMYK_K\tLAB_L\tLAB_A\tLAB_B",
        "END_DATA_FORMAT",
        f"NUMBER_OF_SAMPLES\t{len(rows)}",
        "BEGIN_DATA",
    ]
    for sid, name, device, lab in rows:
        dev = "\t".join(f"{v:.1f}" for v in device)
        lines.append(f"{sid}\t{name}\t{dev}\t{lab[0]:.2f}\t{lab[1]:.2f}\t{lab[2]:.2f}")
    lines.append("END_DATA")
    return "\n".join(lines) + "\n"


def _build_exports(process_doc: dict, report: dict, expected_by_label: dict,
                   matched: dict) -> dict[str, str]:
    """三个导出文本：校正曲线 CSV / 实测特性数据 CGATS / 校正后特性数据 CGATS。"""
    from datetime import date

    curves = report.get("curves") or {}
    suggestions = report.get("ink_suggestions") or []
    bias_pct = [float(s.get("delta_pct") or 0.0) for s in suggestions]

    # 1) 校正曲线 CSV
    csv_lines = ["channel,t_nominal,t_effective,t_corrected"]
    for name in "CMYK":
        for e in curves.get(name) or []:
            eff = "" if e["effective"] is None else e["effective"]
            csv_lines.append(f"{name},{e['t']},{eff},{e['corrected']}")
    curves_csv = "\n".join(csv_lines) + "\n"

    # 2) 实测特性数据（设备名义值 + 实测 Lab → 外部专业软件建「当前印刷条件」ICC）
    known = {p["label"]: p for p in CAL_PATCHES}
    meas_rows = [(p["index"], p["label"], [float(v) for v in known[p["label"]]["device"]],
                  p["measured_lab"]) for p in report["patches"]]
    characterization = _cgats_text(
        f"ColorWorkshop measured characterization {process_doc.get('name', '')} {date.today().isoformat()}",
        meas_rows) if meas_rows else ""

    # 3) 校正后特性数据（校正后设备值 + 预期 Lab——「校准后设备」一阶近似，标注需复测验证）
    corr_rows = []
    for p in report["patches"]:
        device = known[p["label"]]["device"]
        corr_rows.append((p["index"], p["label"],
                          _corrected_device(device, curves, bias_pct),
                          expected_by_label[p["label"]]))
    corrected = _cgats_text(
        f"ColorWorkshop corrected characterization (approx, re-verify) {process_doc.get('name', '')} {date.today().isoformat()}",
        corr_rows) if corr_rows else ""

    return {
        "curves_csv": curves_csv,
        "characterization_cgats": characterization,
        "corrected_cgats": corrected,
        "corrected_devices": [
            {"index": sid, "label": name, "device_corrected": [round(v, 1) for v in dev]}
            for sid, name, dev, _ in corr_rows
        ],
    }


# ---------------------------------------------------------------------------
# 应用（M2）：建议写入补偿模型（预测立即联动）
# ---------------------------------------------------------------------------

def apply_suggestions(process_doc: dict, report: dict) -> dict:
    """把 ink_suggestions 的逐通道 delta 写入补偿模型（直接叠加，见 apply_calibration）。"""
    from .compensation import CompensationStore

    suggestions = report.get("ink_suggestions") or []
    delta = [float(s.get("delta_pct") or 0.0) / 100.0 for s in suggestions]
    return CompensationStore().apply_calibration(
        process_doc["id"], process_doc.get("icc_sha256", ""), delta)
