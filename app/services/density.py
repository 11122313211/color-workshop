"""密度与油墨质量指标（V0.5 可信测量闭环 · M2，V0.6 指标补全，GATF 公式）。

输入为 M1 可靠性管线输出的**已序列化记录**（仅消费 reliability=verified 且
density_ok=True 的记录——分级数据流向：QC 指标不消费 warning/rejected）。

指标（工业通行 GATF 口径）：
- 色强度 DH = max(Dc, Dm, Dy)（滤色密度最大值）
- 色相误差 = (DM − DL) / (DH − DL) × 100%
- 灰度 = DL / DH × 100%
- 色效率 = (1 − (DM + DL) / 2DH) × 100%
- 色纯度（行业别名） = (DH − DL) / DH × 100%
- Murray–Davies 网点面积率 α = (1 − 10^−Dt) / (1 − 10^−Ds) × 100%
- TVI（网点增大）= α实测 − 名义网点率

V0.6 补全（方法论源自印刷质量检测实验，常量口径见 diagnosis.py）：
- 相对反差 K = (Ds − D75) / Ds（行业标准口径，0-1）
- 油墨叠印率 T = (D₁₊₂ − D₁) / D₂ × 100%（第二印色滤色通道读数）
- 灰平衡偏移：中性灰块 a*/b* 相对零点的偏移量与偏色方向
- 实地均匀性：复测会话同标签实地密度 mean/std/CV（样本标准差）
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import re

from .measurement_qa import WHITE_LABELS

# 滤色通道与颜色的对应：实地块 C 的本通道滤色为 C
CHANNEL_KEYS = ("C", "M", "Y")
INK_KEYS = ("C", "M", "Y", "K")  # V0.6：黑版纳入 K 值/TVI 计算口径

# 叠印配对 → (先印色, 后印色)：标准色序 K→C→M→Y（高粘度先印，透明黄最后印）
# 叠印率按**后印色的补色滤色通道**读数：R(M+Y)/G(C+Y) 用 Y 滤色，B(C+M) 用 M 滤色
OVERPRINT_PAIRS = {
    "R": ("M", "Y"),
    "G": ("C", "Y"),
    "B": ("C", "M"),
}


@dataclass
class InkMetrics:
    """单色油墨四指标。"""

    label: str
    channel: str  # 本通道（实地块 C/M/Y）
    strength: float       # 色强度 DH
    hue_error: float      # 色相误差 0-1
    grayness: float       # 灰度 0-1
    efficiency: float     # 色效率 0-1
    purity: float         # 色纯度 0-1（行业别名口径）
    densities: dict       # 四滤色密度


def ink_metrics(label: str, densities: dict) -> InkMetrics | None:
    """由一条色块的四滤色密度计算油墨指标。

    densities: {"C": 1.55, "M": 0.43, "Y": 0.17, "K": 0.78}（K 不参与指标）。
    H/L/M 取 C/M/Y 三滤色的最大/最小/中间值。
    """
    vals = [densities.get(c) for c in CHANNEL_KEYS]
    if any(v is None for v in vals):
        return None
    dc, dm, dy = (float(v) for v in vals)
    if not all(math.isfinite(v) for v in (dc, dm, dy)) or any(v <= 0 for v in (dc, dm, dy)):
        return None
    dk = float(densities.get("K", 0.0) or 0.0)
    if not math.isfinite(dk):
        dk = 0.0
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
        densities={"C": dc, "M": dm, "Y": dy, "K": dk},
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
# V0.6 指标补全：反差率 / 叠印率 / 灰平衡 / 均匀性（纯函数）
# ---------------------------------------------------------------------------

def print_contrast(d_solid, d_75) -> float | None:
    """相对反差 K = (Ds − D75) / Ds（行业标准口径）。

    K 越大暗调层次区分能力越强；负值说明 D75 ≥ Ds（暗调异常，交由诊断规则判读）。
    实地缺失或 ≤0 时返回 None。
    """
    if d_solid is None or d_75 is None:
        return None
    d_solid = float(d_solid)
    d_75 = float(d_75)
    if d_solid <= 0 or d_75 < 0:
        return None
    return round((d_solid - d_75) / d_solid, 4)


def trapping_rate(d_over, d_first, d_second) -> float | None:
    """油墨叠印率 T = (D₁₊₂ − D₁) / D₂ × 100（同一滤色通道读数）。

    d_over 叠印色块密度、d_first 先印色单色密度、d_second 后印色单色密度，
    三者均取后印色的补色滤色通道。后印色密度 ≤0 时返回 None。
    """
    if d_over is None or d_first is None or d_second is None:
        return None
    d_over, d_first, d_second = float(d_over), float(d_first), float(d_second)
    if d_second <= 0:
        return None
    return round((d_over - d_first) / d_second * 100.0, 1)


def gray_balance_direction(a: float, b: float) -> str:
    """灰平衡偏色方向：a*>0 红 / a*<0 绿；b*>0 黄 / b*<0 蓝。"""
    parts = []
    if a > 0:
        parts.append("偏红")
    elif a < 0:
        parts.append("偏绿")
    if b > 0:
        parts.append("偏黄")
    elif b < 0:
        parts.append("偏蓝")
    return "、".join(parts) if parts else "中性"


def density_stats(values: list[float]) -> dict | None:
    """实地均匀性统计：样本标准差（n−1）与变异系数 CV=std/mean。少于 2 个值返回 None。"""
    vals = [float(v) for v in values if v is not None and v >= 0]
    n = len(vals)
    if n < 2:
        return None
    mean = sum(vals) / n
    if mean <= 0:
        return None
    std = (sum((v - mean) ** 2 for v in vals) / (n - 1)) ** 0.5
    return {"n": n, "mean": round(mean, 4), "std": round(std, 4), "cv": round(std / mean, 4)}


# ---------------------------------------------------------------------------
# 会话级聚合（只消费 verified）
# ---------------------------------------------------------------------------

# V0.6：黑版纳入实地/网点解析（k75 参与 K 值计算）；模块级预编译保证线程安全
_SOLID_RE = re.compile(r"^([KCMY])$", re.IGNORECASE)
_TINT_RE = re.compile(r"^([KCMY])(\d{1,3})$", re.IGNORECASE)
_GB_RE = re.compile(r"^GB", re.IGNORECASE)


def parse_ink_label(label: str) -> tuple[str, int | None]:
    """解析油墨标签：K/C/M/Y 实地 → (channel, None)；k60/c75/m30/C100 网点 → (channel, nominal)。"""
    s = str(label).strip()
    m = _TINT_RE.match(s)
    if m:
        nominal = int(m.group(2))
        if 0 < nominal <= 100:
            return m.group(1).upper(), nominal
        return "", None
    m = _SOLID_RE.match(s)
    if m:
        return m.group(1).upper(), None
    return "", None


def is_gray_balance_label(label: str) -> bool:
    """灰平衡块标签（GB 前缀，如样张的 GB1/GB2/GB3）。"""
    return bool(_GB_RE.match(str(label).strip()))


def _mean_densities(dens_list: list[dict]) -> dict[str, float]:
    out: dict[str, float] = {}
    for ch in ("C", "M", "Y", "K"):
        vals = [
            float(d[ch]) for d in dens_list
            if d.get(ch) is not None and math.isfinite(float(d[ch]))
        ]
        if vals:
            out[ch] = sum(vals) / len(vals)
    return out


def ink_quality_from_records(records: list[dict]) -> dict:
    """从已分级记录聚合油墨 QC 指标（**仅 verified 且 density_ok**）。

    返回 {inks: {C/M/Y/K: metrics, K 的色相类指标为 None}, overprints: [...],
          tvi: [...], print_contrast: {ink: {d_solid, d_75, k}},
          trapping: [{label, first, second, filter, d_over, d_first, d_second, t, ok}],
          gray_balance: [{label, lab, da, db, mag, direction}],
          uniformity: [{ink, n, mean, std, cv}],
          verified_records, skipped_records, source_records: [filename]}。
    """
    verified = [r for r in records if r.get("reliability") == "verified" and r.get("density_ok")]
    skipped = len(records) - len(verified)

    solid_acc: dict[str, list[dict]] = {}
    solid_label: dict[str, str] = {}
    tint_acc: dict[tuple[str, int], list[dict]] = {}
    sources: list[str] = []
    # 叠印色按标签聚合（复测对取均值密度，避免同标签多记录重复输出）
    over_acc: dict[str, list[dict]] = {}
    # 灰平衡按标签聚合（复测对取均值，n=测量次数）
    gray_acc: dict[str, dict] = {}
    # 均匀性采样：各色实地密度（跨 verified 记录，同标签多次测量）
    uniform_samples: dict[str, list[float]] = {}

    for rec in verified:
        sources.append(rec.get("filename", "?"))
        for pt in rec.get("patches", []):
            label = str(pt.get("label", ""))
            lab = pt.get("lab")
            dens = pt.get("densities")
            # V0.6：灰平衡块（GB 前缀）按 a*/b* 偏移评价——纯 Lab 记录（无密度）也参与
            if is_gray_balance_label(label) and lab:
                try:
                    a = float(lab[1])
                    b = float(lab[2])
                except (TypeError, ValueError, IndexError):
                    a = b = None
                if a is not None and b is not None and math.isfinite(a) and math.isfinite(b):
                    acc = gray_acc.setdefault(label, {"l": [], "a": [], "b": []})
                    try:
                        lv = float(lab[0])
                        if math.isfinite(lv):
                            acc["l"].append(lv)
                    except (TypeError, ValueError):
                        pass
                    acc["a"].append(a)
                    acc["b"].append(b)
            if not dens:
                continue
            channel, nominal = parse_ink_label(label)
            if channel == "K" and nominal is None:
                dk = dens.get("K")
                if dk is not None and math.isfinite(float(dk)) and float(dk) > 0:
                    solid_acc.setdefault("K", []).append(dens)
                    solid_label.setdefault("K", label)
                    uniform_samples.setdefault("K", []).append(float(dk))
                continue
            if nominal is None and channel:
                m = ink_metrics(label, dens)
                if m is not None:
                    solid_acc.setdefault(channel, []).append(dens)
                    solid_label.setdefault(channel, label)
                    uniform_samples.setdefault(channel, []).append(float(m.densities.get(channel, 0.0) or 0.0))
                continue
            if nominal is not None and channel:
                tint_acc.setdefault((channel, nominal), []).append(dens)
                continue
            low = label.strip().lower()
            if low not in WHITE_LABELS and label.strip().upper() not in ("W", "K") and not is_gray_balance_label(label):
                if ink_metrics(label, dens) is not None:
                    over_acc.setdefault(label.strip().upper(), []).append(
                        {c: float(v) for c, v in dens.items() if v is not None and math.isfinite(float(v))}
                    )

    # 若某通道无纯字母实地（如校准版标签 C100/K100），以 100% 梯级兜底作为实地
    for ch in INK_KEYS:
        if ch not in solid_acc and (ch, 100) in tint_acc:
            solid_acc[ch] = list(tint_acc[(ch, 100)])
            solid_label[ch] = f"{ch}100"
            for d in solid_acc[ch]:
                v = d.get(ch)
                if v is not None and math.isfinite(float(v)) and float(v) > 0:
                    uniform_samples.setdefault(ch, []).append(float(v))

    # 实地按通道聚合均值，确保 K 值/TVI/叠印率与复测均值口径一致
    inks: dict[str, dict] = {}
    solids: dict[str, dict] = {}
    for ch, dens_list in solid_acc.items():
        mean_d = _mean_densities(dens_list)
        lbl = solid_label.get(ch, ch)
        if ch == "K":
            dk = mean_d.get("K", 0.0)
            if dk > 0:
                payload = {
                    "label": lbl,
                    "strength": round(dk, 4),
                    "hue_error": None,
                    "grayness": None,
                    "efficiency": None,
                    "purity": None,
                    "densities": {
                        "C": mean_d.get("C", 0.0),
                        "M": mean_d.get("M", 0.0),
                        "Y": mean_d.get("Y", 0.0),
                        "K": dk,
                    },
                }
                inks["K"] = payload
                solids["K"] = {**payload, "_channel": "K"}
        else:
            m = ink_metrics(lbl, mean_d)
            if m is not None:
                payload = {
                    "label": lbl,
                    "strength": m.strength,
                    "hue_error": m.hue_error,
                    "grayness": m.grayness,
                    "efficiency": m.efficiency,
                    "purity": m.purity,
                    "densities": m.densities,
                }
                inks[ch] = payload
                solids[ch] = {**payload, "_channel": ch}

    # 梯级按 (channel, nominal) 聚合均值，消除复测产生重复 TVI 点
    tints: dict[str, list[tuple[int, dict]]] = {}
    for (ch, nominal), dens_list in sorted(tint_acc.items()):
        tints.setdefault(ch, []).append((nominal, _mean_densities(dens_list)))

    tvi_points: list[dict] = []
    for channel, payload in solids.items():
        tvi_points.extend(tvi_for_ink(payload, tints.get(channel, [])))
    tvi_points.sort(key=lambda p: (p["ink"], p["nominal"]))

    # 叠印色聚合输出：均值密度 → GATF 指标（一条记录一条输出）
    overprints: list[dict] = []
    for label in sorted(over_acc):
        dens_list = over_acc[label]
        mean_dens = {
            c: sum(float(d.get(c, 0.0) or 0.0) for d in dens_list) / len(dens_list)
            for c in ("C", "M", "Y", "K") if any(c in d for d in dens_list)
        }
        m = ink_metrics(label, mean_dens)
        if m is None:
            continue
        overprints.append({
            "label": label,
            "strength": m.strength,
            "hue_error": m.hue_error,
            "grayness": m.grayness,
            "efficiency": m.efficiency,
            "purity": m.purity,
            "densities": m.densities,
        })

    # 相对反差 K：实地密度 + 75% 网点密度（多点取均值）
    print_contrast_out: dict[str, dict] = {}
    for ink in INK_KEYS:
        solid = solids.get(ink)
        if not solid:
            continue
        d75_vals = [
            (dens or {}).get(ink)
            for nominal, dens in tints.get(ink, [])
            if nominal == 75 and (dens or {}).get(ink) is not None
        ]
        d75_vals = [float(v) for v in d75_vals if v > 0]
        d_solid = float((solid.get("densities") or {}).get(ink) or 0.0)
        if d_solid <= 0:
            continue
        d_75 = round(sum(d75_vals) / len(d75_vals), 4) if d75_vals else None
        k = print_contrast(d_solid, d_75)
        # d_75 缺失时也输出（k=None），供诊断规则探测「缺 75% 梯级」
        print_contrast_out[ink] = {"d_solid": round(d_solid, 4), "d_75": d_75, "k": k}

    # 叠印率：R/G/B 配对，按后印色滤色通道读数
    trapping_out: list[dict] = []
    for over in overprints:
        pair = OVERPRINT_PAIRS.get(str(over.get("label", "")).strip().upper())
        if not pair:
            continue
        first, second = pair
        solid_first, solid_second = solids.get(first), solids.get(second)
        if not solid_first or not solid_second:
            continue
        d_over = (over.get("densities") or {}).get(second)
        d_first = (solid_first.get("densities") or {}).get(second)
        d_second = (solid_second.get("densities") or {}).get(second)
        t = trapping_rate(d_over, d_first, d_second)
        trapping_out.append({
            "label": over.get("label"),
            "first": first,
            "second": second,
            "filter": second,
            "d_over": None if d_over is None else round(float(d_over), 4),
            "d_first": None if d_first is None else round(float(d_first), 4),
            "d_second": None if d_second is None else round(float(d_second), 4),
            "t": t,
            "ok": t is not None and t >= 60.0,
        })

    # 实地均匀性：同色实地跨记录 ≥2 次 → 密度离散度
    uniformity_out: list[dict] = []
    for ink in INK_KEYS:
        st = density_stats(uniform_samples.get(ink, []))
        if st:
            uniformity_out.append({"ink": ink, **st})

    # 灰平衡聚合输出（复测对取均值）
    gray_balance = []
    for label in sorted(gray_acc):
        acc = gray_acc[label]
        n = len(acc["a"])
        a = sum(acc["a"]) / n
        b = sum(acc["b"]) / n
        l_mean = sum(acc["l"]) / len(acc["l"]) if acc["l"] else 0.0
        gray_balance.append({
            "label": label,
            "lab": [round(l_mean, 3), round(a, 3), round(b, 3)],
            "da": round(a, 3),
            "db": round(b, 3),
            "mag": round(math.hypot(a, b), 3),
            "direction": gray_balance_direction(a, b),
            "n": n,
        })

    return {
        "inks": {k: inks[k] for k in INK_KEYS if k in inks},
        "overprints": overprints,
        "tvi": tvi_points,
        "print_contrast": print_contrast_out,
        "trapping": trapping_out,
        "gray_balance": gray_balance,
        "uniformity": uniformity_out,
        "verified_records": len(verified),
        "skipped_records": skipped,
        "source_records": sources,
    }
