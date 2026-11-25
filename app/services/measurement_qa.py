"""测量数据可靠性校验（V0.5 可信测量闭环 · 五条规则）。

每条测量记录输出 ``reliability: verified|warning|rejected`` + ``reasons[]``，
以及 ``density_ok`` 通道标记（密度通道独立于色度通道生效，服务 M2 门禁）。

全部规则作用于**已序列化的记录 dict**（patches 为存储层形状），
保证追加导入后可对整个会话重新分级（复测一致性是会话级属性）。

规则（对应 docs/V0.5-实施规划.md §3 风险清单 R1-R8）：
1. 复测一致性 —— 复测模式下同标签跨记录 ΔE≤0.4，未开启复测声明 → warning；
2. 纸白校验 —— 纸白块 Lab 本征合理性与（可选）工艺 ICC 纸白比对；
3. 交叉校验 —— Lab↔XYZ 跨条件探测 / 光谱↔Lab 积分复算，最强转录探针；
4. 数值合理性 —— 密度/色度物理量程（负值、>2.5、超范围 Lab、负 XYZ）；
5. 同批离群 —— 梯尺趋势 3σ（MAD）离群检测。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import numpy as np

from .cgats import LAB_XYZ_REJECT, LAB_XYZ_TOL, _split_condition, integrate_lab, probe_lab_condition
from ..algorithms.core import delta_e_batch

# ---------------------------------------------------------------------------
# 阈值（可调常量）
# ---------------------------------------------------------------------------

#: 复测一致性：同标签两次测量的 ΔE2000 上限
REPEAT_MAX_DE = 0.4
#: 复测一致性（纯密度记录，V0.6）：同标签跨记录滤色密度最大绝对差——
#: 密度计工作流无 Lab，用密度重复性做复测对（仪器复测容差行业通行 ±0.02-0.05D，取宽口径）
REPEAT_MAX_DENSITY = 0.05
#: 纸白 vs 工艺 ICC 纸白的 ΔE 上限
PAPER_WHITE_MAX_DE = 6.0
#: 纸白本征合理性：L 下限 / a、b 上限（绝对口径下纸白允许轻微黄移）
PAPER_WHITE_L_MIN = 80.0
PAPER_WHITE_AB_MAX = 20.0
#: 密度物理量程
DENSITY_WARN_MAX = 2.0
DENSITY_REJECT_MAX = 2.5
#: 色度物理量程（显示发射模式允许 L 略超 100；a/b 超出任何真实材料范围即异常）
LAB_L_MAX = 101.0
LAB_AB_MAX = 150.0
#: 梯尺离群：MAD z 分数阈值
OUTLIER_Z = 3.0
#: 梯尺离群的绝对偏差下限（近平坦通道的微小抖动不触发）
OUTLIER_MIN_DEV = 2.0

WHITE_LABELS = {"w", "white", "paper", "paperwhite", "纸白", "白"}

RELIABILITY_ORDER = {"rejected": 0, "warning": 1, "verified": 2}


@dataclass
class PatchFinding:
    """单色块校验发现。"""

    reason: str
    severity: str  # "rejected" | "warning"
    density: bool = False  # 该发现是否否定密度通道


@dataclass
class RecordVerdict:
    """单记录校验结论。"""

    reliability: str = "verified"
    density_ok: bool = True
    reasons: list[str] = field(default_factory=list)

    def add(self, f: PatchFinding) -> None:
        self.reasons.append(f.reason)
        if f.density:
            self.density_ok = False
        if RELIABILITY_ORDER[f.severity] < RELIABILITY_ORDER[self.reliability]:
            self.reliability = f.severity


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def declared_condition_of(record: dict) -> tuple[str, str]:
    cond = _split_condition(record.get("source_condition") or "")
    return cond if cond else ("D50", "2")


def _is_white_patch(p: dict) -> bool:
    if str(p.get("label", "")).strip().lower() in WHITE_LABELS:
        return True
    spectral = p.get("spectral")
    # 光谱记录：高反射率且中性无明显色偏的块才视为纸白候选（避免误伤浅黄/Y100 等高亮度彩色块）
    if spectral and len(spectral) >= 4:
        vals = [float(v) for v in spectral.values()]
        if sum(vals) / len(vals) > 0.75 and min(vals) > 0.45:
            lab_ref = p.get("lab") or p.get("lab_integrated")
            if lab_ref and (abs(float(lab_ref[1])) > 20.0 or abs(float(lab_ref[2])) > 20.0):
                return False
            return True
    return False


def _effective_labs(patches: list[dict]) -> list[list[float] | None]:
    """补丁生效 Lab 列表：仪器报告值优先，光谱积分值兜底。"""
    return [p.get("lab") or p.get("lab_integrated") for p in patches]


def effective_lab(patch: dict) -> list[float] | None:
    return patch.get("lab") or patch.get("lab_integrated")


def _is_ramp_labels(patches: list[dict]) -> bool:
    """位置标签梯尺（A1..An）：≥6 块且多数标签匹配 A\\d+。"""
    if len(patches) < 6:
        return False
    hits = sum(1 for p in patches if re.match(r"^A\d+$", str(p.get("label", "")).strip()))
    return hits >= len(patches) * 0.8


# ---------------------------------------------------------------------------
# 逐块规则（3 交叉校验 / 4 数值合理性 / 2 纸白）
# ---------------------------------------------------------------------------


def check_patch(
    p: dict,
    declared: tuple[str, str],
    paper_white_lab: list[float] | None,
    kind: str = "cgats",
) -> list[PatchFinding]:
    findings: list[PatchFinding] = []
    label = str(p.get("label", "?"))
    lab = p.get("lab")
    xyz = p.get("xyz")
    densities = p.get("densities")

    # ---- 规则 4：数值合理性（色度 + 密度物理量程）----
    if xyz and any(v < 0 for v in xyz):
        findings.append(PatchFinding(f"色块 {label}：XYZ 出现负值（物理不可能，疑似发射测量未校准）", "rejected"))
    if lab:
        L, a, b = lab
        if L < 0 or L > LAB_L_MAX:
            findings.append(PatchFinding(f"色块 {label}：L*={L:.1f} 超出物理范围", "rejected"))
        if abs(a) > LAB_AB_MAX or abs(b) > LAB_AB_MAX:
            findings.append(PatchFinding(f"色块 {label}：a*/b*={a:.1f},{b:.1f} 超出真实材料范围", "rejected"))
    if densities:
        for ch, v in densities.items():
            if v < 0:
                findings.append(PatchFinding(f"色块 {label}：{ch} 密度负值（{v:.2f}）", "rejected", density=True))
            elif v > DENSITY_REJECT_MAX:
                findings.append(PatchFinding(
                    f"色块 {label}：{ch} 密度 {v:.2f} 超出量程上限 {DENSITY_REJECT_MAX}", "rejected", density=True))
            elif v > DENSITY_WARN_MAX:
                findings.append(PatchFinding(
                    f"色块 {label}：{ch} 密度 {v:.2f} 偏高（>{DENSITY_WARN_MAX}，复核通道/墨量）", "warning", density=True))
        # 实地名 C/M/Y：本通道滤色密度应为四滤色最大，否则滤色通道错位嫌疑
        name = label.strip().upper()
        if name in ("C", "M", "Y") and name in densities:
            others = {c: v for c, v in densities.items() if c != name}
            if others and densities[name] < max(others.values()):
                findings.append(PatchFinding(
                    f"色块 {label}：实地块 {name} 通道密度不是四滤色最大（滤色通道错位嫌疑）", "warning", density=True))

    # ---- 规则 3：交叉校验 ----
    # TSV（Xrite530）的 Lab 列与 XYZ 列分属不同白基/口径，历来自洽性差：
    # 密度列是独立滤色读数，故 Lab 通道失配仅降级 warning（density_ok 不受影响）；
    # CGATS 记录的主体即色度，失配照常拒绝。
    if lab and xyz:
        probe = probe_lab_condition(xyz, lab, declared)
        if probe["status"] == "mismatch" and probe["best_de"] > LAB_XYZ_REJECT and kind != "tsv":
            findings.append(PatchFinding(
                f"色块 {label}：Lab↔XYZ 交叉校验失败（全条件最优 ΔE={probe['best_de']} > {LAB_XYZ_REJECT}，疑似转录/单位错误）",
                "rejected"))
        elif probe["status"] == "mismatch":
            suffix = "（密度通道独立生效）" if kind == "tsv" else ""
            findings.append(PatchFinding(
                f"色块 {label}：Lab↔XYZ 换算偏差 ΔE={probe['best_de']}（无标准条件可解释，Lab 通道存疑）{suffix}",
                "warning"))
        elif probe["status"] == "consistent_other":
            findings.append(PatchFinding(
                f"色块 {label}：Lab 与 XYZ 仅在未声明条件（{probe['best_condition']}）下一致——测量条件未声明或漂移",
                "warning"))
    spectral = p.get("spectral")
    if spectral and lab:
        ill, obs = declared
        try:
            lab_int = integrate_lab({int(k): v for k, v in spectral.items()}, ill, obs)
        except (KeyError, ValueError) as exc:
            findings.append(PatchFinding(
                f"色块 {label}：光谱按声明条件 {ill}/{obs}° 无法积分校验（{exc}）",
                "warning"))
        else:
            de = float(delta_e_batch([lab_int], lab, "cie2000")[0])
            if de > LAB_XYZ_TOL:
                findings.append(PatchFinding(
                    f"色块 {label}：光谱积分 Lab 与仪器 Lab 不一致（ΔE={de:.2f} > {LAB_XYZ_TOL}，按声明条件 {ill}/{obs}°）",
                    "rejected"))

    # ---- 规则 2：纸白校验 ----
    if _is_white_patch(p):
        lab_ref = lab or p.get("lab_integrated")
        if lab_ref:
            L, a, b = lab_ref
            if L < PAPER_WHITE_L_MIN or abs(a) > PAPER_WHITE_AB_MAX or abs(b) > PAPER_WHITE_AB_MAX:
                findings.append(PatchFinding(
                    f"纸白块 {label}：Lab=({L:.1f},{a:.1f},{b:.1f}) 本征异常（疑似未校准/背衬错误）", "warning"))
            elif paper_white_lab is not None:
                de = float(delta_e_batch([lab_ref], paper_white_lab, "cie2000")[0])
                if de > PAPER_WHITE_MAX_DE:
                    findings.append(PatchFinding(
                        f"纸白块 {label}：与工艺 ICC 纸白偏差 ΔE={de:.2f} > {PAPER_WHITE_MAX_DE}", "warning"))

    return findings


# ---------------------------------------------------------------------------
# 记录级规则（5 同批离群）
# ---------------------------------------------------------------------------


def check_outliers(patches: list[dict]) -> list[str]:
    """梯尺（A 标签 ≥6 块）逐通道线性趋势的 MAD 离群检测。返回原因列表。"""
    if not _is_ramp_labels(patches):
        return []
    labs = [effective_lab(p) for p in patches]
    if any(l is None for l in labs):
        return []
    arr = np.asarray(labs, dtype=float)  # (n, 3)
    n = arr.shape[0]
    x = np.arange(n, dtype=float)
    reasons: list[str] = []
    for ch, name in enumerate(("L*", "a*", "b*")):
        y = arr[:, ch]
        slope, intercept = np.polyfit(x, y, 1)
        resid = y - (slope * x + intercept)
        med = float(np.median(resid))
        mad = float(np.median(np.abs(resid - med)))
        if mad < 1e-9:
            continue
        dev = np.abs(resid - med)
        z = dev / (1.4826 * mad)
        # 双条件：统计离群（z>3）且绝对偏差可见（>2），避免近平坦通道的微小抖动误报
        for i in np.where((z > OUTLIER_Z) & (dev > OUTLIER_MIN_DEV))[0]:
            reasons.append(f"梯尺离群：块 {patches[i].get('label')} {name} 偏离趋势 {z[i]:.1f}σ")
    return reasons


# ---------------------------------------------------------------------------
# 会话级规则（1 复测一致性）+ 会话重分级
# ---------------------------------------------------------------------------


def grade_session(session: dict, records: list[dict], paper_white_lab: list[float] | None = None) -> None:
    """对会话内全部记录重评分级（就地更新 reliability / density_ok / reasons）。

    两遍扫描：先做记录级校验（规则 2/3/4/5），被 rejected 的记录不参与第二遍的
    复测配对（规则 1）——rejected 数据「可查不可用」，不得污染有效记录的复测结论。
    """
    repeat_mode = session.get("repeat_mode") == "repeat"

    # ---- 第一遍：记录级（规则 3 交叉校验 / 4 数值合理性 / 2 纸白 / 5 离群）----
    verdicts: list[RecordVerdict] = []
    for rec in records:
        verdict = RecordVerdict()
        declared = declared_condition_of(rec)
        kind = rec.get("kind", "cgats")
        for pt in rec.get("patches", []):
            for f in check_patch(pt, declared, paper_white_lab, kind):
                verdict.add(f)
        for reason in check_outliers(rec.get("patches", [])):
            verdict.add(PatchFinding(reason, "warning"))
        verdicts.append(verdict)

    # ---- 第二遍：规则 1 复测一致性（会话级，rejected 不参与配对）----
    repeat_findings: dict[int, list[str]] = {}
    eligible = [i for i, v in enumerate(verdicts) if v.reliability != "rejected"]
    if repeat_mode:
        by_label: dict[str, list[tuple[int, list[float]]]] = {}
        # V0.6：纯密度记录（密度计工作流无 Lab）按密度通道做复测对
        by_label_dens: dict[str, list[tuple[int, dict]]] = {}
        for ri in eligible:
            for pt in records[ri].get("patches", []):
                lab = effective_lab(pt)
                label = str(pt.get("label", "")).strip()
                if lab is not None:
                    by_label.setdefault(label, []).append((ri, lab))
                elif pt.get("densities"):
                    by_label_dens.setdefault(label, []).append((ri, pt["densities"]))
        # 同标签只要有 ≥2 次测量（跨记录或单页双点重复），必须全部通过两两最大极差检验
        for label, entries in by_label.items():
            if len(entries) < 2:
                continue
            labs = [e[1] for e in entries]
            worst = 0.0
            for i in range(len(labs) - 1):
                pair_max = float(np.max(delta_e_batch(labs[i + 1:], labs[i], "cie2000")))
                if pair_max > worst:
                    worst = pair_max
            if worst > REPEAT_MAX_DE:
                for ri in {e[0] for e in entries}:
                    repeat_findings.setdefault(ri, []).append(
                        f"复测不一致：标签 {label} 复测最大 ΔE={worst:.2f} > {REPEAT_MAX_DE}")
        # 纯密度复测对：同标签在 Lab 配对缺席时按密度通道计算全样本极差（max - min）
        for label, entries in by_label_dens.items():
            if label in by_label or len(entries) < 2:
                continue
            channels = set(entries[0][1].keys())
            for _, dens in entries[1:]:
                channels &= set(dens.keys())
            channels = {c for c in channels if c in ("C", "M", "Y", "K")}
            if not channels:
                continue
            worst = max(
                max(float(dens[c]) for _, dens in entries) - min(float(dens[c]) for _, dens in entries)
                for c in channels
            )
            if worst > REPEAT_MAX_DENSITY:
                for ri in {e[0] for e in entries}:
                    repeat_findings.setdefault(ri, []).append(
                        f"复测不一致：标签 {label} 复测最大密度差 {worst:.2f} > {REPEAT_MAX_DENSITY}")
        # 复测模式下无任何可配对同标签（Lab 或密度）→ 尚不构成有效复测
        has_pair = (any(len(v) >= 2 for v in by_label.values())
                    or any(len(v) >= 2 for v in by_label_dens.values()))
        if not has_pair:
            for ri in eligible:
                repeat_findings.setdefault(ri, []).append(
                    "复测模式已声明，但未发现可配对的同标签测量（每个目标至少测 2 次）")
    else:
        for ri in eligible:
            repeat_findings.setdefault(ri, []).append("单次测量：未声明复测（建议每个目标至少复测 1 次）")

    # ---- 合并 ----
    for ri, rec in enumerate(records):
        verdict = verdicts[ri]
        for reason in repeat_findings.get(ri, []):
            verdict.add(PatchFinding(reason, "warning"))
        rec["reliability"] = verdict.reliability
        rec["density_ok"] = bool(verdict.density_ok)
        rec["reasons"] = verdict.reasons
