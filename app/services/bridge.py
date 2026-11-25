"""跨项目桥接服务：PrintGuard Pro 预检报告消费、工艺护照指纹门禁、专色色域/ΔE 核验。

职责（P0-#2：打通 PrintGuard Pro ↔ 色彩工坊 color-workshop）：
1. **工艺护照指纹门禁（``evaluate_passport_gate``）**：
   文件预检（图像/PDF/PrintGuard 报告）时校验目标工艺的护照指纹与 ICC SHA-256，
   当文件嵌入 ICC 与工艺护照 ICC 不一致、护照失效（stale/invalid/incomplete）或
   指纹不匹配时显式拦截放行（``allowed=False, verdict="rejected"``）。
2. **消费 PrintGuard Pro 机读 JSON 报告（``consume_printguard_report``）**：
   直接解析 PrintGuard 导出的 ``PrintGuard_Preflight_*.json``（含 ``preflight_audit``、
   ``human_explanations``、``process_passport``、``spot_colors``），映射为标准
   ``PreflightReport`` 结构并串联工艺护照门禁与专色色域核验。
3. **Illustrator 桥接专色色域与 ΔE 查询（``check_spot_colors``）**：
   对 Illustrator 提取的专色清单（支持名称、显式 Lab / CMYK / RGB / Hex），
   识别技术刀线/印后专色（如 CutContour/烫金/光油），对其余印刷专色调用本仓真实
   ``lcms2`` 工艺 ICC 反查引擎计算最优模拟分色值与真实 CIEDE2000（ΔE₀₀），
   判定是否在当前工艺色域内。
"""

from __future__ import annotations

import math
import re
from typing import Any

from ..algorithms.core import convert_color
from .compensation import CompensationStore
from .lifecycle import is_archived
from .passport import PassportStore
from .recipe import forward_query, predict_recipe

#: 技术/印后非四色模拟专色（刀模、压痕、烫金、局部光油等，不按四色色域误报）
_TECHNICAL_SPOT_RE = re.compile(
    r"(cut\s*_?contour|dieline|die\b|crease|fold|score|perf|kiss\s*cut|"
    r"varnish|spot\s*uv|uv\b|foil|emboss|deboss|white\s*ink|glue|knockout|"
    r"刀线|刀模|裁切|压痕|折线|折痕|烫金|烫银|光油|击凸|压凹|白墨|粘口)",
    re.IGNORECASE,
)

#: 金属专色（无法由四色油墨物理模拟金属光泽，单独提醒）
_METALLIC_SPOT_RE = re.compile(
    r"(pantone\s*(87[1-7]|80[1-9]|8[1-6]\d)\s*c?|metallic|gold|silver|铜金|青金|专金|专银)",
    re.IGNORECASE,
)

#: 常用 Pantone Coated 工程参考近似 Lab（D50/2°，仅供色域初筛参考，非官方授权色库）
_REFERENCE_SPOT_LABS: dict[str, tuple[float, float, float]] = {
    "PANTONE 485 C": (48.0, 68.0, 56.0),
    "PANTONE 186 C": (44.0, 66.0, 38.0),
    "PANTONE 185 C": (47.0, 69.0, 44.0),
    "PANTONE 032 C": (52.0, 68.0, 41.0),
    "PANTONE 021 C": (60.0, 64.0, 74.0),
    "PANTONE 151 C": (64.0, 54.0, 76.0),
    "PANTONE 165 C": (59.0, 61.0, 66.0),
    "PANTONE 109 C": (84.0, 5.0, 94.0),
    "PANTONE 116 C": (82.0, 12.0, 92.0),
    "PANTONE 123 C": (79.0, 18.0, 82.0),
    "PANTONE 348 C": (46.0, -56.0, 29.0),
    "PANTONE 355 C": (51.0, -64.0, 36.0),
    "PANTONE 375 C": (74.0, -45.0, 66.0),
    "PANTONE 3272 C": (58.0, -48.0, -4.0),
    "PANTONE 300 C": (42.0, -10.0, -54.0),
    "PANTONE 286 C": (33.0, 12.0, -60.0),
    "PANTONE 293 C": (35.0, 4.0, -58.0),
    "PANTONE 2597 C": (28.0, 45.0, -48.0),
    "PANTONE 2685 C": (21.0, 34.0, -46.0),
    "PANTONE 219 C": (49.0, 72.0, -6.0),
    "PANTONE REFLEX BLUE C": (19.0, 26.0, -66.0),
    "PANTONE PROCESS BLUE C": (50.0, -24.0, -48.0),
    "PANTONE RHODAMINE RED C": (50.0, 76.0, -14.0),
    "PANTONE WARM RED C": (54.0, 66.0, 48.0),
    "PANTONE BLACK C": (18.0, 0.5, 1.0),
    "PANTONE COOL GRAY 11 C": (37.0, -0.5, -2.5),
    "PANTONE COOL GRAY 7 C": (62.0, -0.5, -2.0),
}


def _normalize_spot_key(name: str) -> str:
    cleaned = re.sub(r"\s+", " ", str(name or "").strip().upper())
    # 兼容 "PMS 485 C" / "PANTONE 485C"
    cleaned = re.sub(r"^PMS\s+", "PANTONE ", cleaned)
    cleaned = re.sub(r"(\d)C$", r"\1 C", cleaned)
    return cleaned


def _icc_hash_matches(a: str, b: str) -> bool:
    """比较 ICC SHA-256 指纹：支持 64 位完整匹配与 16 位前缀摘要匹配。"""
    sa = str(a or "").strip().lower()
    sb = str(b or "").strip().lower()
    if not sa or not sb:
        return False
    if sa == sb:
        return True
    shorter, longer = (sa, sb) if len(sa) <= len(sb) else (sb, sa)
    return len(shorter) >= 16 and longer.startswith(shorter)


def _hex_to_rgb(hex_str: str) -> list[float] | None:
    s = str(hex_str or "").strip().lstrip("#")
    if len(s) == 3:
        s = "".join(ch * 2 for ch in s)
    if len(s) != 6 or not re.fullmatch(r"[0-9a-fA-F]{6}", s):
        return None
    return [float(int(s[0:2], 16)), float(int(s[2:4], 16)), float(int(s[4:6], 16))]


def evaluate_passport_gate(
    process_doc: dict | None,
    *,
    file_icc_sha256: str = "",
    passport_hash: str = "",
    expected_icc_sha256: str = "",
    require_file_icc: bool = True,
) -> dict:
    """文件预检时的「工艺护照指纹」门禁校验。

    分级风格与五规则测量门禁对齐：``verified``（放行）/ ``warning`` / ``rejected``（拦截）。
    任何以下情况均拦截放行（``allowed=False, verdict="rejected"``）：
    - 工艺不存在或已归档；
    - 工艺尚未生成护照，或传入的 ``passport_hash`` 与当前最新护照不一致；
    - 护照动态状态非 ``valid``（``stale`` / ``invalid`` / ``incomplete``）；
    - 当前工艺 ICC 与护照快照 ICC 不一致；
    - 文件嵌入 ICC / OutputIntent（或外部报告声明 ICC）缺失或与护照 ICC 不一致。
    """
    file_sha = str(file_icc_sha256 or "").strip().lower()
    req_passport_hash = str(passport_hash or "").strip().lower()
    ext_icc_sha = str(expected_icc_sha256 or "").strip().lower()

    if process_doc is None:
        return {
            "checked": True,
            "allowed": False,
            "verdict": "rejected",
            "reason": "process_not_found",
            "message": "目标工艺不存在，工艺护照门禁拦截放行",
            "process_id": "",
            "passport_id": None,
            "passport_version": None,
            "passport_status": None,
            "passport_hash": None,
            "expected_passport_hash": req_passport_hash or None,
            "passport_icc_sha256": None,
            "process_icc_sha256": None,
            "file_icc_sha256": file_sha or ext_icc_sha or None,
            "reasons": ["目标工艺不存在，无法校验工艺护照指纹"],
        }

    pid = str(process_doc.get("id", ""))
    process_icc_sha = str(process_doc.get("icc_sha256", "") or "").strip().lower()

    if is_archived(process_doc):
        return {
            "checked": True,
            "allowed": False,
            "verdict": "rejected",
            "reason": "process_archived",
            "message": "目标工艺已归档（只读），禁止放行",
            "process_id": pid,
            "passport_id": None,
            "passport_version": None,
            "passport_status": None,
            "passport_hash": None,
            "expected_passport_hash": req_passport_hash or None,
            "passport_icc_sha256": None,
            "process_icc_sha256": process_icc_sha or None,
            "file_icc_sha256": file_sha or ext_icc_sha or None,
            "reasons": ["目标工艺已归档，不可用于新文件放行"],
        }

    store = PassportStore()
    latest = store.latest_snapshot(pid)
    if latest is None:
        return {
            "checked": True,
            "allowed": False,
            "verdict": "rejected",
            "reason": "no_passport",
            "message": "目标工艺尚未生成工艺护照，门禁拦截放行（请先在色彩工坊生成有效工艺护照）",
            "process_id": pid,
            "passport_id": None,
            "passport_version": None,
            "passport_status": None,
            "passport_hash": None,
            "expected_passport_hash": req_passport_hash or None,
            "passport_icc_sha256": None,
            "process_icc_sha256": process_icc_sha or None,
            "file_icc_sha256": file_sha or ext_icc_sha or None,
            "reasons": ["目标工艺尚未生成工艺护照"],
        }

    exposed = store.expose(latest, process_doc)
    latest_hash = str(exposed.get("passport_hash", "") or "").strip().lower()
    passport_status = str(exposed.get("status", "") or "")
    passport_icc_sha = str(((exposed.get("evidence") or {}).get("icc") or {}).get("sha256", "") or "").strip().lower()

    effective_file_icc = file_sha or ext_icc_sha
    reasons: list[str] = []
    primary_reason = "passport_verified"

    def _reject(code: str, msg: str) -> None:
        nonlocal primary_reason
        if primary_reason == "passport_verified":
            primary_reason = code
        reasons.append(msg)

    # 1. 外部传入的护照指纹与当前最新护照比对
    if req_passport_hash and not _icc_hash_matches(req_passport_hash, latest_hash):
        _reject(
            "passport_hash_mismatch",
            f"工艺护照指纹不一致：提交指纹 {req_passport_hash[:12]} ≠ 工艺当前最新护照 {latest_hash[:12]}",
        )

    # 2. 护照动态状态核验（valid 才放行；stale/invalid/incomplete 拦截）
    if passport_status != "valid":
        detail_msgs = "；".join(
            str(r.get("message", "")) for r in (exposed.get("status_reasons") or []) if r.get("message")
        )
        _reject(
            f"passport_{passport_status}",
            f"工艺护照状态为 {exposed.get('status_label', passport_status)}（{passport_status}），不可放行"
            + (f"：{detail_msgs}" if detail_msgs else ""),
        )

    # 3. 工艺当前 ICC 与护照快照 ICC 一致性核验
    if not passport_icc_sha or not _icc_hash_matches(process_icc_sha, passport_icc_sha):
        _reject(
            "process_icc_mismatch",
            f"工艺当前 ICC（{process_icc_sha[:12] or '缺失'}）与护照快照 ICC（{passport_icc_sha[:12] or '缺失'}）不一致",
        )

    # 4. 文件嵌入 ICC / OutputIntent / 外部声明 ICC 与护照 ICC 一致性核验
    if effective_file_icc:
        if not _icc_hash_matches(effective_file_icc, passport_icc_sha):
            _reject(
                "icc_passport_mismatch",
                f"文件 ICC 指纹（{effective_file_icc[:12]}）与工艺护照 ICC 指纹（{passport_icc_sha[:12]}）不一致，拦截放行",
            )
    elif require_file_icc:
        _reject(
            "file_icc_missing",
            "文件未嵌入 ICC / OutputIntent（或未携带 ICC 指纹），无法证明与工艺护照一致，拦截放行",
        )

    allowed = len(reasons) == 0
    return {
        "checked": True,
        "allowed": allowed,
        "verdict": "verified" if allowed else "rejected",
        "reason": primary_reason,
        "message": (
            f"工艺护照指纹门禁通过（v{exposed['version']} · ICC {passport_icc_sha[:12]} 一致）"
            if allowed
            else reasons[0]
        ),
        "process_id": pid,
        "process_name": process_doc.get("name", ""),
        "passport_id": exposed.get("id"),
        "passport_version": exposed.get("version"),
        "passport_status": passport_status,
        "passport_status_label": exposed.get("status_label", ""),
        "passport_hash": latest_hash,
        "expected_passport_hash": req_passport_hash or None,
        "passport_icc_sha256": passport_icc_sha or None,
        "process_icc_sha256": process_icc_sha or None,
        "file_icc_sha256": effective_file_icc or None,
        "reasons": reasons,
    }


def check_spot_colors(
    process_doc: dict,
    icc_bytes: bytes,
    spots: list[str | dict[str, Any]],
    *,
    tolerance: float = 2.0,
    max_rounds: int = 4,
    device_bias: list[float] | None = None,
) -> dict:
    """检查专色清单是否在指定工艺色域内，并计算真实 CIEDE2000（ΔE₀₀）与推荐四色分色值。"""
    device_space = str((process_doc.get("icc_meta") or {}).get("device_space", "CMYK")).upper()
    pid = str(process_doc.get("id", ""))
    if device_bias is None and pid and process_doc.get("icc_sha256"):
        device_bias = CompensationStore().bias_for_predict(pid, process_doc["icc_sha256"])

    passport_summary = PassportStore().summary_for_process(process_doc)

    items: list[dict] = []
    in_gamut_count = 0
    out_of_gamut_count = 0
    borderline_count = 0
    technical_count = 0
    unknown_count = 0
    max_delta_e: float | None = None

    for raw in spots or []:
        if isinstance(raw, str):
            name = raw.strip()
            spec: dict[str, Any] = {"name": name}
        elif isinstance(raw, dict):
            name = str(raw.get("name") or raw.get("label") or "").strip()
            spec = raw
        else:
            continue
        if not name:
            continue

        # 1. 技术/刀模/印后非模拟专色
        if _TECHNICAL_SPOT_RE.search(name):
            technical_count += 1
            items.append({
                "name": name,
                "category": "technical",
                "source": "name_rule",
                "target_lab": None,
                "predicted_lab": None,
                "recommended_recipe": None,
                "delta_e": None,
                "tolerance": float(tolerance),
                "in_gamut": None,
                "risk": "technical",
                "verdict": "verified",
                "message": "刀模/印后工艺专色（非四色模拟油墨），需保持独立专色图层与叠印属性",
            })
            continue

        is_metallic = bool(_METALLIC_SPOT_RE.search(name))

        # 2. 解析目标 Lab（优先级：显式 Lab → 工程参考色表 → 显式 CMYK → 显式 RGB/Hex）
        target_lab: list[float] | None = None
        source = "unknown"

        raw_lab = spec.get("lab")
        if isinstance(raw_lab, (list, tuple)) and len(raw_lab) == 3:
            try:
                vals = [float(v) for v in raw_lab]
                if all(math.isfinite(v) for v in vals):
                    target_lab = vals
                    source = "explicit_lab"
            except (TypeError, ValueError):
                target_lab = None

        if target_lab is None:
            norm = _normalize_spot_key(name)
            if norm in _REFERENCE_SPOT_LABS:
                target_lab = list(_REFERENCE_SPOT_LABS[norm])
                source = "reference_table"

        if target_lab is None and isinstance(spec.get("cmyk"), (list, tuple)) and len(spec["cmyk"]) == 4:
            if device_space == "CMYK":
                try:
                    cmyk_vals = [float(v) for v in spec["cmyk"]]
                    if all(math.isfinite(v) for v in cmyk_vals):
                        fwd = forward_query(icc_bytes, device_space, cmyk_vals)
                        target_lab = [float(v) for v in fwd["lab"]]
                        source = "device_cmyk"
                except Exception:  # noqa: BLE001
                    target_lab = None

        if target_lab is None:
            rgb_vals: list[float] | None = None
            if isinstance(spec.get("rgb"), (list, tuple)) and len(spec["rgb"]) == 3:
                try:
                    cand = [float(v) for v in spec["rgb"]]
                    if all(math.isfinite(v) for v in cand):
                        rgb_vals = cand
                except (TypeError, ValueError):
                    rgb_vals = None
            elif spec.get("hex"):
                rgb_vals = _hex_to_rgb(str(spec["hex"]))
            if rgb_vals is not None:
                lab = convert_color(tuple(rgb_vals), "srgb", "lab")
                target_lab = [round(float(lab[0]), 2), round(float(lab[1]), 2), round(float(lab[2]), 2)]
                source = "converted_srgb"

        if is_metallic and target_lab is None:
            out_of_gamut_count += 1
            items.append({
                "name": name,
                "category": "metallic",
                "source": "name_rule",
                "target_lab": None,
                "predicted_lab": None,
                "recommended_recipe": None,
                "delta_e": None,
                "tolerance": float(tolerance),
                "in_gamut": False,
                "risk": "out_of_gamut",
                "verdict": "warning",
                "message": "金属专色无法由 CMYK 四色模拟金属光泽，必须配置独立金属专色墨路或烫印工艺",
            })
            continue

        if target_lab is None:
            unknown_count += 1
            items.append({
                "name": name,
                "category": "ink",
                "source": "unknown",
                "target_lab": None,
                "predicted_lab": None,
                "recommended_recipe": None,
                "delta_e": None,
                "tolerance": float(tolerance),
                "in_gamut": None,
                "risk": "unknown",
                "verdict": "warning",
                "message": "未提供专色 Lab/RGB/CMYK 色值且不在内置常用参考表中，无法计算 ΔE₀₀；请从 Illustrator 导出色值或测得 Lab 后复查",
            })
            continue

        # 3. 经工艺 ICC 真实反查计算最小 ΔE₀₀ 与推荐分色设备值
        pred = predict_recipe(
            icc_bytes,
            device_space,
            tuple(target_lab),
            tolerance=tolerance,
            method="cie2000",
            max_rounds=max_rounds,
            device_bias=device_bias,
        )
        de = round(float(pred["delta_e"]), 2)
        if max_delta_e is None or de > max_delta_e:
            max_delta_e = de

        in_gamut = de <= tolerance
        if in_gamut:
            in_gamut_count += 1
            risk = "in_gamut"
            verdict = "verified"
            msg = f"在当前工艺色域内（ΔE₀₀ = {de:.2f} ≤ {tolerance:.1f}），可用四色分色模拟"
        elif de <= max(tolerance + 1.5, 3.5):
            borderline_count += 1
            risk = "borderline"
            verdict = "warning"
            msg = f"色域边缘临界（ΔE₀₀ = {de:.2f} > {tolerance:.1f}），四色模拟有轻微偏色，建议客户打样确认"
        else:
            out_of_gamut_count += 1
            risk = "out_of_gamut"
            verdict = "rejected"
            msg = f"超出当前工艺色域（最优四色模拟 ΔE₀₀ = {de:.2f} > {tolerance:.1f}），建议保留真实专色墨路或由客户书面签样确认偏色"

        if source == "reference_table":
            msg += "（目标 Lab 来自工程常用参考近似值，严格合同色请以实测 Lab 为准）"

        items.append({
            "name": name,
            "category": "metallic" if is_metallic else "ink",
            "source": source,
            "target_lab": [round(float(v), 2) for v in target_lab],
            "predicted_lab": [round(float(v), 2) for v in pred["predicted_lab"]],
            "recommended_recipe": pred["recipe"],
            "delta_e": de,
            "tolerance": float(tolerance),
            "in_gamut": in_gamut,
            "risk": risk,
            "verdict": verdict,
            "message": msg,
        })

    if out_of_gamut_count > 0:
        overall_verdict = "rejected"
    elif borderline_count > 0 or unknown_count > 0:
        overall_verdict = "warning"
    else:
        overall_verdict = "verified"

    return {
        "process_id": pid,
        "process_name": process_doc.get("name", ""),
        "device_space": device_space,
        "icc_sha256": process_doc.get("icc_sha256", ""),
        "passport_summary": passport_summary,
        "tolerance": float(tolerance),
        "verdict": overall_verdict,
        "summary": {
            "total": len(items),
            "in_gamut": in_gamut_count,
            "borderline": borderline_count,
            "out_of_gamut": out_of_gamut_count,
            "technical": technical_count,
            "unknown": unknown_count,
            "max_delta_e": max_delta_e,
        },
        "items": items,
        "note": "内置 Pantone 参考表为工程初筛近似 Lab（非官方授权色库），精确合同专色请以 Illustrator 实测 Lab 或分光光度计测得值为准。",
    }


def consume_printguard_report(
    report_json: dict,
    *,
    filename: str = "",
    process_doc: dict | None = None,
    icc_bytes: bytes | None = None,
    passport_hash: str = "",
    expected_icc_sha256: str = "",
    spot_colors: list[str | dict[str, Any]] | None = None,
    tolerance: float = 2.0,
) -> dict:
    """直接消费 PrintGuard Pro 导出的机读 JSON 预检报告，并执行工艺护照门禁与专色核验。"""
    if not isinstance(report_json, dict) or not isinstance(report_json.get("preflight_audit"), dict):
        raise ValueError("无效的 PrintGuard 机读预检报告：缺少 preflight_audit 对象")

    audit = report_json.get("preflight_audit") or {}
    human = report_json.get("human_explanations") if isinstance(report_json.get("human_explanations"), dict) else None
    embedded_pp = report_json.get("process_passport") if isinstance(report_json.get("process_passport"), dict) else {}

    issues: list[dict] = []
    blockers: list[str] = []

    def _add(level: str, msg: str, *, blocker: bool = False) -> None:
        issues.append({"level": level, "message": msg})
        if blocker:
            blockers.append(msg)

    # 1. 消费 PrintGuard 核心预检字段
    if audit.get("rgb_cleared") is False:
        _add("error", "PrintGuard 预检阻断：存在未转换的 RGB 图像资产，需按目标工艺 ICC 转 CMYK 并确认打样", blocker=True)
    if audit.get("overprint_black_active") is False:
        _add("warn", "PrintGuard 预检警示：K100 纯黑小字/条码处于镂空状态，建议开启纯黑直压（Overprint）")
    if audit.get("fonts_outlined") is False:
        _add("error", "PrintGuard 预检阻断：文档存在未转曲活动文字，发排 RIP 存在缺字替字风险", blocker=True)
    if audit.get("glue_tab_cleared") is False and str(report_json.get("box_preset", "")).lower() != "fefco0427":
        _add("warn", "PrintGuard 预检警示：15mm 糊盒粘口位残留油墨，建议执行脱墨留白")

    tac_peak = audit.get("tac_peak_measured")
    tac_limit = audit.get("tac_limit_percent")
    if isinstance(tac_peak, (int, float)) and isinstance(tac_limit, (int, float)):
        if tac_peak > tac_limit:
            _add("error", f"PrintGuard 预检阻断：实测 TAC 总墨量峰值 {tac_peak}% 超过限额 {tac_limit}%", blocker=True)
        else:
            _add("info", f"PrintGuard 实测 TAC 峰值 {tac_peak}% / 限额 {tac_limit}%（达标）")

    barcode = audit.get("barcode_audit") if isinstance(audit.get("barcode_audit"), dict) else {}
    if barcode.get("quiet_zone_compliant") is False:
        _add("warn", "PrintGuard 预检警示：GS1 条码静区不足，需设计师调整版面留足空白保护区")

    de_audit = audit.get("ciede2000_audit") if isinstance(audit.get("ciede2000_audit"), dict) else {}
    de_rating = str(de_audit.get("tolerance_rating", "")).upper()
    de_peak = de_audit.get("delta_e_peak")
    if de_rating == "FAIL":
        _add("error", f"PrintGuard 预检阻断：CIEDE2000 色差峰值 ΔE₀₀ = {de_peak} 超出工业允收红线（>2.50），必须人工/客户确认", blocker=True)
    elif de_rating == "WARN":
        _add("warn", f"PrintGuard 预检警示：CIEDE2000 色差峰值 ΔE₀₀ = {de_peak} 处于商业允收临界（1.50~2.50），建议客户确认打样")
    elif de_rating == "PASS":
        _add("info", f"PrintGuard CIEDE2000 色差峰值 ΔE₀₀ = {de_peak}（优秀）")

    # 2. 工艺护照指纹门禁（当提供 process_doc 或报告自身携带 process_passport 时触发）
    eff_passport_hash = str(passport_hash or embedded_pp.get("passport_hash") or "").strip()
    eff_icc_sha = str(expected_icc_sha256 or embedded_pp.get("icc_sha256") or "").strip()
    passport_gate = None
    if process_doc is not None or eff_passport_hash or eff_icc_sha:
        passport_gate = evaluate_passport_gate(
            process_doc,
            file_icc_sha256=eff_icc_sha,
            passport_hash=eff_passport_hash,
            require_file_icc=True,
        )
        if not passport_gate["allowed"]:
            _add("error", f"工艺护照门禁拦截（{passport_gate['reason']}）：{passport_gate['message']}", blocker=True)
        else:
            _add("info", passport_gate["message"])

    # 3. 专色清单色域核验（合并请求入参与报告自带 spot_colors）
    merged_spots = spot_colors if spot_colors is not None else (
        report_json.get("spot_colors")
        or report_json.get("spot_swatches")
        or []
    )
    spot_check = None
    if merged_spots and process_doc is not None and icc_bytes:
        spot_check = check_spot_colors(process_doc, icc_bytes, merged_spots, tolerance=tolerance)
        s_sum = spot_check["summary"]
        if s_sum["out_of_gamut"] > 0:
            _add(
                "warn",
                f"专色色域核验：{s_sum['out_of_gamut']} 个专色超出当前工艺色域（最大 ΔE₀₀ = {s_sum['max_delta_e']}），建议保留独立专色墨路或客户签样确认",
            )
        elif s_sum["borderline"] > 0:
            _add(
                "warn",
                f"专色色域核验：{s_sum['borderline']} 个专色处于色域边缘（最大 ΔE₀₀ = {s_sum['max_delta_e']}），建议打样确认",
            )
        elif s_sum["in_gamut"] > 0:
            _add(
                "info",
                f"专色色域核验：{s_sum['in_gamut']} 个印刷专色全部在当前工艺色域内（最大 ΔE₀₀ = {s_sum['max_delta_e']}）",
            )

    status = "error" if any(i["level"] == "error" for i in issues) else (
        "warn" if any(i["level"] == "warn" for i in issues) else "pass"
    )
    release_allowed = (
        len(blockers) == 0
        and (passport_gate["allowed"] if passport_gate is not None else True)
    )
    release_verdict = (
        "rejected"
        if not release_allowed
        else ("warning" if status == "warn" else "verified")
    )

    return {
        "filename": filename or f"printguard_{report_json.get('box_preset', 'report')}.json",
        "format": "PrintGuard-JSON",
        "mode": str(report_json.get("standard") or "ISO 12647-2 / Fogra39"),
        "size": [0, 0],
        "has_embedded_icc": bool(eff_icc_sha),
        "embedded_icc": {"sha256": eff_icc_sha} if eff_icc_sha else {},
        "issues": issues,
        "status": status,
        "passport_gate": passport_gate,
        "spot_check": spot_check,
        "printguard_summary": {
            "application": report_json.get("application", "PrintGuard Pro"),
            "timestamp": report_json.get("timestamp", ""),
            "box_preset": report_json.get("box_preset", ""),
            "release_allowed": release_allowed,
            "release_verdict": release_verdict,
            "blockers": blockers,
            "human_explanations_counts": (human or {}).get("counts"),
            "human_summary_text": (human or {}).get("summary_text", ""),
        },
    }
