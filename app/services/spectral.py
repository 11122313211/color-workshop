"""光谱同色异谱风险分析（V0.10）。

本模块只消费测量会话中的光谱反射率，复用 ``cgats`` 的 CIE 积分与白点，
在 D50/D65/A/CIE F11 四个 2° 条件下计算 Lab 与 CIEDE2000 色差。

重要边界：
- 输出是工程同色异谱风险指标，不等同 CIE 15 特殊同色异谱指数 MI；
- 允许荧光增白纸反射率 >1（OBA），但 <0 或 >1.5 给出数据质量警告；
- 不同样本的波长网格各自独立积分，不强行重采样到同一网格。
"""

from __future__ import annotations

import math
from typing import Iterable

from ..algorithms.core import delta_e
from .cgats import WL_GRID, integrate_lab

DEFAULT_CONDITIONS = ("D50", "D65", "A", "FL11")
SPECTRAL_MIN_WL = int(WL_GRID[0])
SPECTRAL_MAX_WL = int(WL_GRID[-1])

CONDITION_META = {
    "D50": {"label": "D50 / 2°", "illuminant": "D50"},
    "D65": {"label": "D65 / 2°", "illuminant": "D65"},
    "A": {"label": "A / 2°", "illuminant": "A"},
    "FL11": {"label": "CIE F11 / 2°", "illuminant": "FL11"},
}

#: 默认风险阈值：≤1.0 low，≤2.0 medium，>2.0 high
RISK_LOW_MAX = 1.0
RISK_MEDIUM_MAX = 2.0
RISK_LABELS = {"low": "低", "medium": "中", "high": "高", "none": "无可比光谱"}

DISCLAIMER = "工程同色异谱风险指标，不等同 CIE 15 特殊同色异谱指数 MI"

_RELIABILITY_RANK = {"verified": 2, "warning": 1, "rejected": 0}


def risk_for_delta_e(value: float | None) -> str:
    """阈值边界：1.0 仍为 low，2.0 仍为 medium，超过 2.0 为 high。"""
    if value is None:
        return "none"
    if value <= RISK_LOW_MAX:
        return "low"
    if value <= RISK_MEDIUM_MAX:
        return "medium"
    return "high"


def normalize_conditions(conditions: Iterable[str] | None) -> tuple[str, ...]:
    out: list[str] = []
    source = DEFAULT_CONDITIONS if conditions is None else conditions
    for raw in source:
        key = str(raw).strip().upper()
        if key not in CONDITION_META:
            raise ValueError(f"不支持的光源条件：{raw}（仅支持 D50/D65/A/FL11）")
        if key not in out:
            out.append(key)
    if not out:
        raise ValueError("至少选择一个光源条件")
    return tuple(out)


def normalize_spectral(spectral: dict | None) -> dict[int, float]:
    """键统一为 int 波长，值统一为有限浮点；不修改/重采样波长网格。"""
    if not isinstance(spectral, dict):
        raise ValueError("光谱数据需为 {波长: 反射率} 对象")
    out: dict[int, float] = {}
    for raw_w, raw_v in spectral.items():
        try:
            wavelength = int(round(float(raw_w)))
            value = float(raw_v)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"无效光谱点：{raw_w!r} -> {raw_v!r}") from exc
        if not math.isfinite(value):
            raise ValueError(f"波长 {wavelength}nm 的反射率不是有限数")
        if wavelength < SPECTRAL_MIN_WL or wavelength > SPECTRAL_MAX_WL:
            raise ValueError(
                f"波长 {wavelength}nm 超出 CGATS 积分网格 "
                f"{SPECTRAL_MIN_WL}–{SPECTRAL_MAX_WL}nm"
            )
        out[wavelength] = value
    if len(out) < 4:
        raise ValueError("有效光谱点少于 4 个，无法积分")
    return dict(sorted(out.items()))


def _curve_warnings(spectral: dict[int, float], label: str) -> list[str]:
    warnings: list[str] = []
    bad = [
        (w, v)
        for w, v in spectral.items()
        if v < 0 or v > 1.5
    ]
    if bad:
        sample = ", ".join(f"{w}nm={v:.3f}" for w, v in bad[:3])
        warnings.append(
            f"{label}存在超出 [0, 1.5] 的反射率（{sample}"
            f"{' 等' if len(bad) > 3 else ''}）；已保留读数但请核查 OBA/仪器校准"
        )
    elif any(v > 1.0 for v in spectral.values()):
        warnings.append(
            f"{label}含反射率 >1.0 的 OBA 荧光增白响应；按原始光谱积分，不裁切"
        )
    if min(spectral) > 400 or max(spectral) < 700:
        warnings.append(f"{label}波长范围 {min(spectral)}–{max(spectral)}nm 未覆盖完整可见光")
    return warnings


def _condition_labs(spectral: dict[int, float], condition: str) -> list[float]:
    values = integrate_lab(spectral, condition, "2")
    if not all(math.isfinite(float(value)) for value in values):
        raise ValueError("光谱积分结果非有限，请检查反射率量级与数据质量")
    return [round(float(value), 4) for value in values]


def metamerism(
    spectral_a: dict | None,
    spectral_b: dict | None,
    *,
    conditions: Iterable[str] | None = None,
    label_a: str = "",
    label_b: str = "",
) -> dict:
    """两个光谱样本在多光源下的 Lab/ΔE00 与同色异谱风险。"""
    conds = normalize_conditions(conditions)
    a = normalize_spectral(spectral_a)
    b = normalize_spectral(spectral_b)
    warnings: list[str] = []
    warnings.extend(_curve_warnings(a, label_a or "样本 A"))
    warnings.extend(_curve_warnings(b, label_b or "样本 B"))
    if tuple(a) != tuple(b):
        warnings.append("两个样本波长网格不同；各自独立积分，未做重采样")

    rows = []
    for condition in conds:
        meta = CONDITION_META[condition]
        lab_a = _condition_labs(a, condition)
        lab_b = _condition_labs(b, condition)
        de = round(delta_e(lab_a, lab_b, "cie2000"), 4)
        if not math.isfinite(float(de)):
            raise ValueError("光谱色差结果非有限，请检查反射率量级与数据质量")
        rows.append({
            "condition": condition,
            "label": meta["label"],
            "illuminant": meta["illuminant"],
            "observer": "2",
            "lab_a": lab_a,
            "lab_b": lab_b,
            "delta_e": de,
            "risk": risk_for_delta_e(de),
        })

    worst = max(rows, key=lambda row: row["delta_e"])
    risk = risk_for_delta_e(worst["delta_e"])
    return {
        "label_a": label_a,
        "label_b": label_b,
        "conditions": rows,
        "max_delta_e": worst["delta_e"],
        "worst_condition": worst["condition"],
        "risk": risk,
        "risk_label": RISK_LABELS[risk],
        "warnings": list(dict.fromkeys(warnings)),
        "disclaimer": DISCLAIMER,
    }


def _session_patches(session: dict) -> dict[str, dict]:
    """会话内按 patch label 选一份光谱：verified > warning > rejected，后导入优先。"""
    chosen: dict[str, dict] = {}
    records = sorted(
        session.get("records", {}).values(),
        key=lambda rec: (
            _RELIABILITY_RANK.get(rec.get("reliability", "warning"), 1),
            rec.get("imported_at", ""),
        ),
    )
    for record in records:
        if record.get("reliability") == "rejected":
            continue
        for patch in record.get("patches", []):
            spectral = patch.get("spectral")
            if not spectral:
                continue
            label = str(patch.get("label") or "").strip() or "未命名色块"
            try:
                normalized = normalize_spectral(spectral)
            except ValueError:
                continue
            chosen[label] = {
                "label": label,
                "spectral": normalized,
                "reliability": record.get("reliability", "warning"),
                "record_id": record.get("id", ""),
                "source_condition": record.get("source_condition", ""),
            }
    return chosen


def _session_brief(session: dict, patches: dict[str, dict] | None = None) -> dict:
    patches = patches if patches is not None else _session_patches(session)
    wavelengths = [w for p in patches.values() for w in p["spectral"]]
    stats = session.get("stats") or {}
    return {
        "id": session.get("id", ""),
        "instrument": session.get("instrument", ""),
        "condition": session.get("condition", ""),
        "process_id": session.get("process_id", ""),
        "created_at": session.get("created_at", ""),
        "spectral_patches": len(patches),
        "verified_records": int(stats.get("verified", 0) or 0),
        "wavelength_min": min(wavelengths) if wavelengths else None,
        "wavelength_max": max(wavelengths) if wavelengths else None,
        "available": bool(patches),
    }


def _shared_labels(a: dict[str, dict], b: dict[str, dict]) -> list[str]:
    return sorted(set(a) & set(b), key=lambda value: (value.lower(), value))


def session_summary(session: dict, all_sessions: list[dict] | None = None) -> dict:
    patches = _session_patches(session)
    brief = _session_brief(session, patches)
    warnings: list[str] = []
    if not patches:
        warnings.append("该会话没有可用光谱数据")
    else:
        for label, patch in list(patches.items())[:12]:
            warnings.extend(_curve_warnings(patch["spectral"], f"色块 {label}"))

    comparable_ids: list[str] = []
    if patches and all_sessions:
        for other in all_sessions:
            if other.get("id") == session.get("id"):
                continue
            if _shared_labels(patches, _session_patches(other)):
                comparable_ids.append(other.get("id", ""))
    return {
        **brief,
        "comparable_sessions": len(comparable_ids),
        "comparable_session_ids": comparable_ids,
        "warnings": list(dict.fromkeys(warnings)),
        "disclaimer": DISCLAIMER,
    }


def compare_sessions(
    session_a: dict,
    session_b: dict,
    *,
    top_n: int = 12,
    conditions: Iterable[str] | None = None,
) -> dict:
    """按 patch label 匹配两会话光谱，输出最大风险与 top 差异。"""
    pa = _session_patches(session_a)
    pb = _session_patches(session_b)
    labels = _shared_labels(pa, pb)
    warnings: list[str] = []
    if not labels:
        warnings.append("两个会话没有共同 patch label 的可用光谱数据")

    rows = []
    for label in labels:
        item = metamerism(
            pa[label]["spectral"],
            pb[label]["spectral"],
            conditions=conditions,
            label_a=label,
            label_b=label,
        )
        rows.append({
            **item,
            "label": label,
            "spectral_a": {str(k): round(v, 6) for k, v in pa[label]["spectral"].items()},
            "spectral_b": {str(k): round(v, 6) for k, v in pb[label]["spectral"].items()},
            "reliability_a": pa[label]["reliability"],
            "reliability_b": pb[label]["reliability"],
        })

    rows.sort(key=lambda row: (-row["max_delta_e"], row["label"].lower()))
    max_row = rows[0] if rows else None
    for row in rows[:top_n]:
        warnings.extend(row.get("warnings") or [])
    risk = max_row["risk"] if max_row else "none"
    return {
        "session_a": _session_brief(session_a, pa),
        "session_b": _session_brief(session_b, pb),
        "matched": len(rows),
        "top": rows[:top_n],
        "max_delta_e": max_row["max_delta_e"] if max_row else None,
        "worst_condition": max_row["worst_condition"] if max_row else "",
        "risk": risk,
        "risk_label": RISK_LABELS[risk],
        "warnings": list(dict.fromkeys(warnings)),
        "disclaimer": DISCLAIMER,
    }


def process_spectral_overview(process_id: str, sessions: list[dict]) -> dict:
    """护照光谱段：仅统计绑定该工艺的会话；不伪造缺失数据。"""
    relevant = [s for s in sessions if s.get("process_id") == process_id]
    indexes = {s.get("id", ""): _session_patches(s) for s in relevant}
    with_data = {sid: idx for sid, idx in indexes.items() if idx}
    all_wavelengths = [
        w
        for idx in with_data.values()
        for patch in idx.values()
        for w in patch["spectral"]
    ]

    comparable_sessions: set[str] = set()
    worst_de: float | None = None
    worst_condition = ""
    worst_risk = "none"
    pair_warnings: list[str] = []
    session_ids = sorted(with_data)
    # 一次性按 (sid, label, condition) 预计算 Lab，避免 O(S²) 重复解析会话与重复光谱积分
    lab_cache: dict[tuple[str, str], dict[str, list[float]]] = {}

    def _get_labs(sid: str, label: str) -> dict[str, list[float]]:
        key = (sid, label)
        cached = lab_cache.get(key)
        if cached is None:
            sp = with_data[sid][label]["spectral"]
            cached = {cond: _condition_labs(sp, cond) for cond in DEFAULT_CONDITIONS}
            lab_cache[key] = cached
        return cached

    for i, sid_a in enumerate(session_ids):
        pa = with_data[sid_a]
        for sid_b in session_ids[i + 1:]:
            pb = with_data[sid_b]
            shared = _shared_labels(pa, pb)
            if not shared:
                continue
            comparable_sessions.update((sid_a, sid_b))
            for label in shared:
                if tuple(pa[label]["spectral"]) != tuple(pb[label]["spectral"]):
                    pair_warnings.append("两个样本波长网格不同；各自独立积分，未做重采样")
                labs_a = _get_labs(sid_a, label)
                labs_b = _get_labs(sid_b, label)
                for cond in DEFAULT_CONDITIONS:
                    de = round(delta_e(labs_a[cond], labs_b[cond], "cie2000"), 4)
                    if not math.isfinite(float(de)):
                        continue
                    if worst_de is None or de > worst_de:
                        worst_de = de
                        worst_condition = cond
                        worst_risk = risk_for_delta_e(de)

    warnings: list[str] = []
    if not with_data:
        warnings.append("无光谱测量数据，护照未伪造光谱证据")
    elif not comparable_sessions:
        warnings.append("有光谱数据但没有可比较的同工艺会话对")
    for idx in with_data.values():
        for label, patch in list(idx.items())[:12]:
            warnings.extend(_curve_warnings(patch["spectral"], f"色块 {label}"))
    warnings.extend(pair_warnings)

    return {
        "available": bool(with_data),
        "spectral_patches": sum(len(idx) for idx in with_data.values()),
        "wavelength_min": min(all_wavelengths) if all_wavelengths else None,
        "wavelength_max": max(all_wavelengths) if all_wavelengths else None,
        "comparable_sessions": len(comparable_sessions),
        "comparable_session_ids": sorted(comparable_sessions),
        "max_risk": worst_risk if comparable_sessions else "none",
        "max_delta_e": worst_de,
        "worst_condition": worst_condition,
        "warnings": list(dict.fromkeys(warnings)),
        "disclaimer": DISCLAIMER,
    }
