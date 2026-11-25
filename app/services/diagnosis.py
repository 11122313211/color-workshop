"""规则型印刷诊断引擎（V0.6 · M2，闭环后半环：从分级数据到工艺修正）。

输入为 density.ink_quality_from_records 的聚合结果，输出结论清单
（每条含 rule/severity/title/evidence/cause/action/ref）。

常量口径见 docs/V0.6-实施规划.md §3（行业标准量化依据表）：
- ISO 12647-2:2013：TVI 中间调控制点 50%、CMY 涂布纸目标 16%、允差 ±3%
- GB/T 17934.2-2021（对应 ISO 12647-2）：灰平衡偏移精神 |a*|、|b*| ≤ 1.0
- 行业通行：反差率 K 预警下限 0.40、允差 ΔK±10%；叠印率 ≥60% 最低可接受
- 多点离散度方法：实地密度 CV>5% 提示墨层不均
- GATF：色相误差/灰度解读阈值、星标判读口诀（样张第二页）
"""

from __future__ import annotations

from dataclasses import dataclass

#: TVI 中间调控制点（%）——ISO 12647-2:2013（2004 版为 40%，40% 仍作参考点）
TVI_MID_NOMINAL = 50
#: TVI 中间调目标（%）——CMY 涂布纸基准，随纸张/加网变化时以工艺校准曲线为准
TVI_TARGET_MID = 16.0
#: TVI 中间调允差（%）
TVI_TOL = 3.0
#: 反差率 K 预警下限——胶印暗调控制经验值
K_MIN = 0.40
#: 反差率允差 ΔK（相对）
K_TOL_REL = 0.10
#: 叠印率最低可接受（%）
TRAP_MIN = 60.0
#: 灰平衡 |a*|/|b*| 上限——GB/T 17934.2 精神（ΔE≈±0.9）
GB_AB_MAX = 1.0
#: 实地密度 CV 预警——多点离散度方法通行阈值
UNIFORM_CV_WARN = 0.05
#: GATF 色相误差解读阈值（info 级）
HUE_ERROR_INFO = 0.35
#: GATF 灰度解读阈值（info 级）
GRAYNESS_INFO = 0.30

_SEVERITY_ORDER = {"ok": 0, "info": 1, "warn": 2, "err": 3}


@dataclass
class Finding:
    """一条诊断结论。evidence 证据 / cause 成因 / action 建议 / ref 依据。"""

    rule: str
    severity: str  # ok | info | warn | err
    title: str
    evidence: str = ""
    cause: str = ""
    action: str = ""
    ref: str = ""

    def as_dict(self) -> dict:
        return {
            "rule": self.rule,
            "severity": self.severity,
            "title": self.title,
            "evidence": self.evidence,
            "cause": self.cause,
            "action": self.action,
            "ref": self.ref,
        }


def _tvi_mid(points: list[dict], nominal: int = TVI_MID_NOMINAL) -> dict[str, float]:
    """取各色中间调 TVI（小数，0-1）。"""
    out: dict[str, float] = {}
    for p in points:
        if p.get("nominal") == nominal and p.get("ink") in ("C", "M", "Y", "K"):
            out[p["ink"]] = float(p.get("tvi") or 0.0)
    return out


def _rule_tvi(q: dict) -> list[Finding]:
    """R1：中间调 TVI 偏离目标（ISO 12647-2:2013）。达标时给聚合 ok 结论。"""
    mid = _tvi_mid(q.get("tvi") or [])
    out = []
    ok_inks = []
    for ink, tvi in sorted(mid.items()):
        dev = round(tvi * 100 - TVI_TARGET_MID, 1)
        if abs(dev) <= TVI_TOL:
            ok_inks.append(f"{ink} {tvi * 100:.1f}%")
            continue
        if abs(dev) > TVI_TOL * 2:
            sev = "err"
        else:
            sev = "warn"
        if dev > 0:
            cause = "印刷压力过大 / 供墨量过多 / 纸张吸墨过强导致网点铺展"
            action = "适当降低印刷压力，小幅减少各色组供墨量，复查中间调"
        else:
            cause = "油墨转移不足（压力偏小 / 墨量偏少 / 墨辊磨损）"
            action = "核查印刷压力与墨量匹配，必要时提高供墨"
        out.append(Finding(
            rule="R1",
            severity=sev,
            title=f"{ink} 中间调 TVI {tvi * 100:.1f}%，偏离目标 {dev:+.1f} 个百分点",
            evidence=f"50% 网点实测 TVI={tvi * 100:.1f}%（目标 {TVI_TARGET_MID:.0f}%±{TVI_TOL:.0f}%）",
            cause=cause,
            action=action,
            ref=f"ISO 12647-2:2013 中间调 TVI 目标 {TVI_TARGET_MID:.0f}%±{TVI_TOL:.0f}%（随纸张/加网变化以工艺曲线为准）",
        ))
    if ok_inks and not out:
        out.append(Finding(
            rule="R1",
            severity="ok",
            title=f"TVI 中间调达标（{'、'.join(ok_inks)}）",
            evidence=f"50% 网点 TVI 目标 {TVI_TARGET_MID:.0f}%±{TVI_TOL:.0f}%",
            action="维持当前压力与墨量",
            ref="ISO 12647-2:2013",
        ))
    return out


def _rule_k(q: dict) -> list[Finding]:
    """R2/R3：暗调反差不足与四色一致性。全部达标时给聚合 ok 结论。"""
    pc = q.get("print_contrast") or {}
    out = []
    ks: dict[str, float] = {}
    for ink in ("C", "M", "Y", "K"):
        entry = pc.get(ink)
        if not entry or entry.get("k") is None:
            continue
        k = float(entry["k"])
        ks[ink] = k
        if k < K_MIN:
            out.append(Finding(
                rule="R2",
                severity="warn",
                title=f"{ink} 反差率 K={k:.2f}，低于 {K_MIN:.2f} 预警线",
                evidence=f"K=(Ds−D75)/Ds={entry['d_solid']:.2f}→{entry['d_75']:.2f}",
                cause="暗调网点扩大并级，暗调层次区分能力不足",
                action="降低整机印刷压力，减少该色组供墨量，重点监控暗调",
                ref=f"行业反差率控制（K≥{K_MIN:.2f}，允差 ΔK±10%）",
            ))
    if ks and not any(f.rule == "R2" for f in out):
        out.append(Finding(
            rule="R2",
            severity="ok",
            title=f"反差率全部达标（{'、'.join(f'{i} {v:.2f}' for i, v in sorted(ks.items()))}）",
            evidence=f"K≥{K_MIN:.2f} 预警线",
            action="维持当前暗调控制，定期抽检 D75 与反差率监控漂移",
            ref=f"行业反差率控制（K≥{K_MIN:.2f}）",
        ))
    if len(ks) >= 2:
        spread = round(max(ks.values()) - min(ks.values()), 3)
        if spread > K_TOL_REL:
            lo = min(ks, key=ks.get)
            hi = max(ks, key=ks.get)
            out.append(Finding(
                rule="R3",
                severity="warn",
                title=f"四色反差率一致性差：极差 {spread:.2f}",
                evidence=" ".join(f"{ink}={k:.2f}" for ink, k in sorted(ks.items())),
                cause="各色组墨量 / 印刷压力不均",
                action=f"对偏差色组（{lo} 偏低、{hi} 偏高）单独校正",
                ref=f"行业允差 ΔK±{K_TOL_REL * 100:.0f}%",
            ))
    return out


def _rule_trapping(q: dict) -> list[Finding]:
    """R4：叠印率（行业 60% 最低可接受线）。"""
    traps = q.get("trapping") or []
    measured = [t for t in traps if t.get("t") is not None]
    if not measured:
        return []
    out = []
    failing = sorted([t for t in measured if t["t"] < TRAP_MIN], key=lambda t: t["t"])
    if failing:
        detail = "、".join(f"{t['label']}({t['first']}+{t['second']})={t['t']:.0f}%" for t in failing)
        out.append(Finding(
            rule="R4",
            severity="err",
            title=f"{len(failing)} 组叠印率低于 {TRAP_MIN:.0f}% 合格线：{detail}",
            evidence="叠印率按后印色滤色通道 T=(D₁₊₂−D₁)/D₂×100%",
            cause="后印色附着不良：油墨粘度 / 表面张力失配，或层间干燥过快产生湿拒",
            action="调整油墨配比提升表面张力匹配度；适当放缓印刷速度、延长层间短暂干燥；必要时调整色序",
            ref=f"行业叠印率合格线 ≥{TRAP_MIN:.0f}%（最低可接受）",
        ))
    else:
        best = max(t["t"] for t in measured)
        out.append(Finding(
            rule="R4",
            severity="ok",
            title=f"叠印率全部达标（{len(measured)} 组，最低 {min(t['t'] for t in measured):.0f}%，最高 {best:.0f}%）",
            evidence="、".join(f"{t['label']}={t['t']:.0f}%" for t in sorted(measured, key=lambda t: t["t"])),
            action="维持当前色序与油墨匹配状态",
            ref=f"行业叠印率合格线 ≥{TRAP_MIN:.0f}%",
        ))
    return out


def _rule_gray_balance(q: dict) -> list[Finding]:
    """R5：灰平衡偏色（GB/T 17934.2 精神）。"""
    gb = q.get("gray_balance") or []
    if not gb:
        return []
    out = []
    bad = [g for g in gb if abs(g.get("da") or 0.0) > GB_AB_MAX or abs(g.get("db") or 0.0) > GB_AB_MAX]
    if bad:
        w = max(bad, key=lambda g: g.get("mag") or 0.0)
        fixes = []
        if (w.get("da") or 0.0) > 0:
            fixes.append("a* 偏红→降低 M 量（或提高 C）")
        elif (w.get("da") or 0.0) < 0:
            fixes.append("a* 偏绿→提高 M 量（或降低 C）")
        if (w.get("db") or 0.0) > 0:
            fixes.append("b* 偏黄→降低 Y 量")
        elif (w.get("db") or 0.0) < 0:
            fixes.append("b* 偏蓝→提高 Y 量")
        out.append(Finding(
            rule="R5",
            severity="warn",
            title=f"灰平衡偏色：{len(bad)}/{len(gb)} 块超限（最大 {w['label']} a*{w['da']:+.2f} b*{w['db']:+.2f}，{w['direction']}）",
            evidence="；".join(f"{g['label']} a*{g['da']:+.2f} b*{g['db']:+.2f}" for g in bad),
            cause="CMY 配比偏离该纸张 / 墨组的灰平衡点（吸墨差异会放大偏色）",
            action="；".join(fixes) + "，微调后重测灰平衡块验证",
            ref=f"GB/T 17934.2 灰平衡控制精神（|a*|、|b*|≤{GB_AB_MAX:.1f}）",
        ))
    else:
        out.append(Finding(
            rule="R5",
            severity="ok",
            title=f"灰平衡正常（{len(gb)} 块 |a*|、|b*| ≤ {GB_AB_MAX:.1f}）",
            evidence="；".join(f"{g['label']} a*{g['da']:+.2f} b*{g['db']:+.2f}" for g in gb),
            action="维持当前 CMY 配比",
            ref=f"GB/T 17934.2 灰平衡控制精神（|a*|、|b*|≤{GB_AB_MAX:.1f}）",
        ))
    return out


def _rule_uniformity(q: dict) -> list[Finding]:
    """R6：实地均匀性（多点密度离散度）。全部均匀时给聚合 ok 结论。"""
    uni = q.get("uniformity") or []
    out = []
    ok_unis = []
    for u in uni:
        if (u.get("cv") or 0.0) > UNIFORM_CV_WARN:
            out.append(Finding(
                rule="R6",
                severity="warn",
                title=f"{u['ink']} 实地墨层不均：CV={u['cv'] * 100:.1f}%",
                evidence=f"{u['n']} 次测量 mean={u['mean']:.2f} std={u['std']:.3f}",
                cause="供墨不均 / 纸张吸墨不一致 / 墨辊匀墨不良",
                action="检查墨辊匀墨状态与纸张批次一致性",
                ref=f"多点离散度方法（CV≤{UNIFORM_CV_WARN * 100:.0f}%）",
            ))
        else:
            ok_unis.append(f"{u['ink']} CV={(u['cv'] or 0.0) * 100:.1f}%")
    if ok_unis and not out:
        out.append(Finding(
            rule="R6",
            severity="ok",
            title=f"实地墨层均匀（{'、'.join(ok_unis)}）",
            evidence=f"复测多点密度离散度 CV≤{UNIFORM_CV_WARN * 100:.0f}%",
            action="维持当前匀墨状态",
            ref="多点离散度方法",
        ))
    return out


def _rule_sufficiency(q: dict) -> list[Finding]:
    """R7：数据充分性指引（缺 75% 梯级 / 叠印块 / 灰块 / 复测）。

    会话无任何 verified 记录时不产出（前端显示方法要求占位）。
    """
    if int(q.get("verified_records") or 0) < 1:
        return []
    out = []
    pc = q.get("print_contrast") or {}
    inks_present = set((q.get("inks") or {}).keys())
    missing75 = [ink for ink in ("C", "M", "Y", "K")
                 if ink in inks_present
                 and (pc.get(ink) is None or pc[ink].get("d_75") is None)]
    if missing75:
        out.append(Finding(
            rule="R7",
            severity="info",
            title=f"{'、'.join(missing75)} 缺少 75% 梯级，无法计算反差率 K",
            evidence="K=(Ds−D75)/Ds 需要 75% 网点密度",
            action="生成 V0.6 样张（含 75% 梯级）重测",
            ref="相对反差方法论：75% 是暗调关键控制点",
        ))
    if not (q.get("trapping") or []):
        out.append(Finding(
            rule="R7",
            severity="info",
            title="缺少双色叠印块，无法计算叠印率",
            evidence="叠印率需要 R(M+Y)/G(C+Y)/B(C+M) 实地色块",
            action="生成 V0.6 样张（含叠印条）重测",
            ref="油墨叠印率方法论",
        ))
    if not (q.get("gray_balance") or []):
        out.append(Finding(
            rule="R7",
            severity="info",
            title="缺少灰平衡块，无法评价灰平衡",
            evidence="灰平衡需要 GB 前缀色块的 Lab 数据",
            action="生成 V0.6 样张（含 GB 灰平衡格，导入含 Lab 的测量文件）",
            ref="灰平衡偏移方法论（GB/T 17934.2）",
        ))
    if int(q.get("verified_records") or 0) < 2:
        out.append(Finding(
            rule="R7",
            severity="info",
            title="单次测量：无法评估复测一致性与实地均匀性",
            evidence=f"verified 记录 {q.get('verified_records', 0)} 条",
            action="启用复测模式（样张双点布局同标签成对），重测后对比",
            ref="可信测量闭环：复测一致性规则",
        ))
    return out


def _rule_gatf_color(q: dict) -> list[Finding]:
    """R8：油墨色偏解读（GATF 口径，info 级）。"""
    out = []
    for ink in ("C", "M", "Y"):
        m = (q.get("inks") or {}).get(ink)
        if not m:
            continue
        he, gr = m.get("hue_error"), m.get("grayness")
        if he is None or gr is None:
            continue
        if float(he) > HUE_ERROR_INFO or float(gr) > GRAYNESS_INFO:
            strength = m.get("strength")
            ev = f"DH={float(strength):.2f}" if strength is not None else ""
            out.append(Finding(
                rule="R8",
                severity="info",
                title=f"{ink} 油墨色偏解读：色相误差 {he * 100:.0f}% / 灰度 {gr * 100:.0f}%",
                evidence=ev,
                cause="原生油墨色偏或叠印脏化（灰度偏高提示杂质灰分）",
                action="对照工艺目标油墨核对，必要时对色/换墨",
                ref="GATF 三滤色指标口径",
            ))
    return out


def diagnose(ink_quality: dict) -> list[dict]:
    """对 ink_quality_from_records 结果执行规则诊断，返回按严重度排序的结论列表。

    判定矩阵：有度量必有显式判定（ok/warn/err），缺数据给 info 指引。
    """
    findings: list[Finding] = []
    findings.extend(_rule_tvi(ink_quality))
    findings.extend(_rule_k(ink_quality))
    findings.extend(_rule_trapping(ink_quality))
    findings.extend(_rule_gray_balance(ink_quality))
    findings.extend(_rule_uniformity(ink_quality))
    findings.extend(_rule_gatf_color(ink_quality))
    findings.extend(_rule_sufficiency(ink_quality))
    findings.sort(key=lambda f: _SEVERITY_ORDER.get(f.severity, 1))
    return [f.as_dict() for f in findings]
