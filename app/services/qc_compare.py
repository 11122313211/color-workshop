"""会话对比与趋势（V0.6 · M3，闭环后半环：复测监控的时间轴）。

- compare_sessions(ink_a, ink_b)：双会话指标 Δ 表（Δ = 主会话 A − 对比会话 B）
- trend_for_process(store, process_id)：按工艺聚合会话时间序列（每会话现场
  计算 ink_quality，不落库）——落地「定期抽检 75% 密度与反差率，监控暗调
  层次变化」的运营闭环。

指标口径与 density.py / diagnosis.py 一致；只消费 verified 记录。
"""

from __future__ import annotations

from .density import ink_quality_from_records

#: TVI 中间调控制点（%），与 diagnosis.TVI_MID_NOMINAL 对齐
TVI_MID_NOMINAL = 50


def _mid_tvi(q: dict) -> dict[str, float]:
    """各色中间调 TVI（百分数值）。"""
    out: dict[str, float] = {}
    for p in q.get("tvi") or []:
        if p.get("nominal") == TVI_MID_NOMINAL:
            out[p["ink"]] = round(float(p.get("tvi") or 0.0) * 100, 2)
    return out


def _metric(q: dict) -> dict:
    """从 ink_quality 结果提取对比所需的指标切片。"""
    ks = {ink: (v or {}).get("k") for ink, v in (q.get("print_contrast") or {}).items()}
    tvi_mid = _mid_tvi(q)
    strength = {ink: (v or {}).get("strength") for ink, v in (q.get("inks") or {}).items()}
    grayness = {ink: (v or {}).get("grayness") for ink, v in (q.get("inks") or {}).items()}
    traps = {t.get("label"): t.get("t") for t in (q.get("trapping") or []) if t.get("t") is not None}
    return {"k": ks, "tvi_mid": tvi_mid, "strength": strength,
            "grayness": grayness, "trapping": traps}


def compare_sessions(ink_a: dict, ink_b: dict) -> dict:
    """双会话指标对比。delta = A − B（主会话相对对比会话的变化）。"""
    ma, mb = _metric(ink_a), _metric(ink_b)
    deltas: dict[str, dict] = {}
    for group in ("k", "tvi_mid", "strength", "grayness", "trapping"):
        ga, gb = ma.get(group) or {}, mb.get(group) or {}
        for key in sorted(set(ga) | set(gb)):
            va, vb = ga.get(key), gb.get(key)
            delta = None if (va is None or vb is None) else round(float(va) - float(vb), 4)
            deltas.setdefault(group, {})[key] = {"a": va, "b": vb, "delta": delta}
    return {"deltas": deltas}


def trend_for_process(measurements_store, process_id: str) -> list[dict]:
    """工艺绑定的会话时间序列（按创建时间升序；每会话现场计算指标）。"""
    points = []
    for session in measurements_store.list_sessions():
        if session.get("process_id") != process_id:
            continue
        sid = session["id"]
        records = (
            list(session["records"].values())
            if isinstance(session.get("records"), dict)
            else measurements_store.records(sid)
        )
        q = ink_quality_from_records(records)
        metric = _metric(q)
        trap_vals = [t for t in metric["trapping"].values() if t is not None]
        points.append({
            "session_id": sid,
            "created_at": session.get("created_at", ""),
            "instrument": session.get("instrument", ""),
            "verified": q.get("verified_records", 0),
            "k": metric["k"],
            "tvi_mid": metric["tvi_mid"],
            "trap_min": round(min(trap_vals), 1) if trap_vals else None,
        })
    points.sort(key=lambda p: p["created_at"])
    return points
