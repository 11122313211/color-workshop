"""密度与油墨质量指标（V0.5 可信测量闭环 · M2，GATF 公式）。

输入为 M1 可靠性管线输出的**已序列化记录**（仅消费 reliability=verified 且
density_ok=True 的记录——分级数据流向：QC 指标不消费 warning/rejected）。

指标（课程实践公式，工业通行口径）：
- 色强度 DH = max(Dc, Dm, Dy)（滤色密度最大值）
- 色相误差 = (DM − DL) / (DH − DL) × 100%
- 灰度 = DL / DH × 100%
- 色效率 = (1 − (DM + DL) / 2DH) × 100%
- 色纯度（课程别名） = (DH − DL) / DH × 100%
- Murray–Davies 网点面积率 α = (1 − 10^−Dt) / (1 − 10^−Ds) × 100%
- TVI（网点增大）= α实测 − 名义网点率
"""

from __future__ import annotations

from dataclasses import dataclass

# 滤色通道与颜色的对应：实地块 C 的本通道滤色为 C
CHANNEL_KEYS = ("C", "M", "Y")


@dataclass
class InkMetrics:
    """单色油墨四指标。"""

    label: str
    channel: str  # 本通道（实地块 C/M/Y）
    strength: float       # 色强度 DH
    hue_error: float      # 色相误差 0-1
    grayness: float       # 灰度 0-1
    efficiency: float     # 色效率 0-1
    purity: float         # 色纯度 0-1（课程别名口径）
    densities: dict       # 四滤色密度


def ink_metrics(label: str, densities: dict) -> InkMetrics | None:
    """由一条色块的四滤色密度计算油墨指标。

    densities: {"C": 1.55, "M": 0.43, "Y": 0.17, "K": 0.78}（K 不参与指标）。
    H/L/M 取 C/M/Y 三滤色的最大/最小/中间值。
    """
    vals = [densities.get(c) for c in CHANNEL_KEYS]
    if any(v is None for v in vals) or any(v <= 0 for v in vals):
        return None
    dc, dm, dy = (float(v) for v in vals)
    dh, dl = max(dc, dm, dy), min(dc, dm, dy)
    dm_mid = dc + dm + dy - dh - dl  # 中间值
    if dh <= 0 or dh <= dl:
        return None
    return InkMetrics(
        label=label,
        channel=label.strip().upper(),
        strength=round(dh, 4),
        hue_error=round((dm_mid - dl) / (dh - dl), 4),
        grayness=round(dl / dh, 4),
        efficiency=round(1 - (dm_mid + dl) / (2 * dh), 4),
        purity=round((dh - dl) / dh, 4),
        densities={"C": dc, "M": dm, "Y": dy, "K": float(densities.get("K", 0.0))},
    )


def murray_davies_dot_area(d_tint: float, d_solid: float) -> float:
    """Murray–Davies 相对网点面积率（0-1）。

    α = (1 − 10^−Dt) / (1 − 10^−Ds)
    """
    if d_solid <= 0 or d_tint < 0:
        raise ValueError("密度输入非法")
    denom = 1 - 10 ** (-d_solid)
    if denom <= 0:
        raise ValueError("实地密度异常")
    return (1 - 10 ** (-d_tint)) / denom


def tvi_for_ink(solid: dict, tints: list[tuple[int, dict]]) -> list[dict]:
    """按名义网点率配对 TVI。

    tints: [(nominal%, tint densities dict)]，取本通道滤色密度。
    返回 [{ink, nominal, dot_area, tvi}]。
    """
    channel = solid.get("_channel")
    solid_d = (solid.get("densities") or {}).get(channel)
    if not solid_d or solid_d <= 0:
        return []
    out = []
    for nominal, dens in tints:
        dt = (dens or {}).get(channel)
        if dt is None or dt < 0:
            continue
        area = murray_davies_dot_area(dt, solid_d)
        out.append({
            "ink": channel,
            "nominal": nominal,
            "dot_area": round(area, 4),
            "tvi": round(area - nominal / 100.0, 4),
        })
    return out


# ---------------------------------------------------------------------------
# 会话级聚合（只消费 verified）
# ---------------------------------------------------------------------------

_SOLID_RE = None  # 延迟编译
_TINT_RE = None


def _compile_patterns():
    global _SOLID_RE, _TINT_RE
    import re

    if _SOLID_RE is None:
        _SOLID_RE = re.compile(r"^([CMY])$", re.IGNORECASE)
        _TINT_RE = re.compile(r"^([CMY])(\d{1,3})$", re.IGNORECASE)


def parse_ink_label(label: str) -> tuple[str, int | None]:
    """解析油墨标签：C/M/Y 实地 → (channel, None)；c60/m30/y40 网点 → (channel, nominal)。"""
    _compile_patterns()
    s = str(label).strip()
    m = _TINT_RE.match(s)
    if m:
        return m.group(1).upper(), int(m.group(2))
    m = _SOLID_RE.match(s)
    if m:
        return m.group(1).upper(), None
    return "", None


def ink_quality_from_records(records: list[dict]) -> dict:
    """从已分级记录聚合油墨 QC 指标（**仅 verified 且 density_ok**）。

    返回 {inks: {C/M/Y: metrics}, overprints: [...], tvi: [...],
          verified_records, skipped_records, source_records: [filename]}。
    """
    _compile_patterns()
    verified = [r for r in records if r.get("reliability") == "verified" and r.get("density_ok")]
    skipped = len(records) - len(verified)

    inks: dict[str, dict] = {}
    overprints: list[dict] = []
    solids: dict[str, dict] = {}
    tints: dict[str, list[tuple[int, dict]]] = {}
    sources: list[str] = []

    for rec in verified:
        sources.append(rec.get("filename", "?"))
        for pt in rec.get("patches", []):
            dens = pt.get("densities")
            if not dens:
                continue
            label = str(pt.get("label", ""))
            channel, nominal = parse_ink_label(label)
            m = ink_metrics(label, dens)
            if m is None:
                continue
            payload = {
                "label": label,
                "strength": m.strength,
                "hue_error": m.hue_error,
                "grayness": m.grayness,
                "efficiency": m.efficiency,
                "purity": m.purity,
                "densities": m.densities,
            }
            if nominal is None and channel:
                inks[channel] = payload
                solids[channel] = {**payload, "_channel": channel}
            elif nominal is not None:
                tints.setdefault(channel, []).append((nominal, dens))
            elif label.strip().upper() not in ("W", "K"):
                # 叠印色（R/G/B/10M10Y 等）按自身三滤色算指标
                overprints.append(payload)

    tvi_points: list[dict] = []
    for channel, payload in solids.items():
        tvi_points.extend(tvi_for_ink(payload, tints.get(channel, [])))
    tvi_points.sort(key=lambda p: (p["ink"], p["nominal"]))

    return {
        "inks": {k: inks[k] for k in ("C", "M", "Y") if k in inks},
        "overprints": overprints,
        "tvi": tvi_points,
        "verified_records": len(verified),
        "skipped_records": skipped,
        "source_records": sources,
    }
