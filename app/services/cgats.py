"""测量导出文本解析器（V0.5 可信测量闭环 · M1）。

以课程 30 个真实导出文件为**格式模板**（只取结构，不取数值）：
- CGATS.17：BEGIN_DATA_FORMAT/END_DATA_FORMAT + BEGIN_DATA/END_DATA，
  字段变体：SPECTRAL（nm380-730）/ XYZ+LAB / RGB+XYZ+LAB；
- Xrite530 TSV：``Color Name`` 表头 + Lab/XYZ/LCh/Status-T 密度列。

编码容错链：BOM 嗅探（utf-8-sig/utf-16）→ utf-8 → cp1252 → gbk；
结构异常不炸（逐行跳过并记入 notes），无法识别的输入抛 :class:`CgatsParseError`。

色度积分自实现（CIE 1931/1964 CMF × D50/D65 SPD，380-730nm/10nm 网格），
避免 colour-science 版本间 API 漂移；白点自检值与教科书一致（D50/2°≈96.42,100,82.49）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import colour
import numpy as np

# ---------------------------------------------------------------------------
# 常量与阈值
# ---------------------------------------------------------------------------

WL_GRID = np.arange(380.0, 731.0, 10.0)  # 380-730nm，10nm 步长

CMFS_2DEG = "CIE 1931 2 Degree Standard Observer"
CMFS_10DEG = "CIE 1964 10 Degree Standard Observer"

#: 交叉校验候选条件（照明体 × 视场）：声明条件 + 常见漂移目标
CONDITIONS: tuple[tuple[str, str], ...] = (("D50", "2"), ("D65", "2"), ("D50", "10"), ("D65", "10"))

#: Lab↔XYZ/Lab↔光谱 在声明条件下一致的容差（ΔE2000）
LAB_XYZ_TOL = 1.5
#: 全条件均不一致时的拒绝阈值（转录/单位级错误的探针）
LAB_XYZ_REJECT = 12.0

_CMFS_CACHE: dict[str, np.ndarray] = {}
_SPD_CACHE: dict[str, np.ndarray] = {}


class CgatsParseError(ValueError):
    """解析失败（编码不可识别 / 结构完全不符）。"""


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


@dataclass
class ParsedPatch:
    """单个色块的测量值。"""

    label: str
    lab: list[float] | None = None
    xyz: list[float] | None = None
    rgb: list[float] | None = None
    spectral: dict[int, float] | None = None
    densities: dict[str, float] | None = None
    #: 由光谱积分得到的 Lab（D50/2° 或文件声明条件），供复测比对与下游展示
    lab_integrated: list[float] | None = None


@dataclass
class ParsedFile:
    """一个测量导出文件的解析结果。"""

    kind: str  # "cgats" | "tsv"
    meta: dict[str, str] = field(default_factory=dict)
    fields: list[str] = field(default_factory=list)
    patches: list[ParsedPatch] = field(default_factory=list)
    #: 文件头声明的测量条件（如 "D50/2°"），未声明为空串
    source_condition: str = ""
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 编码与工具
# ---------------------------------------------------------------------------

_ENC_CHAIN = ("utf-8", "cp1252", "gbk")


def decode_bytes(raw: bytes) -> tuple[str, str]:
    """BOM 嗅探 + 编码回退链。返回 (text, encoding)。"""
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig"), "utf-8-sig"
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        return raw.decode("utf-16"), "utf-16"
    for enc in _ENC_CHAIN:
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    raise CgatsParseError("无法识别文本编码（已尝试 utf-8/cp1252/gbk 与 BOM 嗅探）")


def _to_float(token: str) -> float | None:
    """宽容浮点解析：容忍 -0025.44 / 100.00 / 非数返回 None。"""
    try:
        return float(token)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# CIE 积分（自实现，380-730nm/10nm 网格）
# ---------------------------------------------------------------------------


def _cmfs(observer: str) -> np.ndarray:
    if observer not in _CMFS_CACHE:
        name = CMFS_2DEG if observer == "2" else CMFS_10DEG
        cmf = colour.MSDS_CMFS[name]
        src_wl = np.asarray(cmf.wavelengths, dtype=float)
        vals = np.asarray(cmf.values, dtype=float)
        grid = np.stack([np.interp(WL_GRID, src_wl, vals[:, ch]) for ch in range(3)], axis=1)
        _CMFS_CACHE[observer] = grid
    return _CMFS_CACHE[observer]


def _spd(illuminant: str) -> np.ndarray:
    if illuminant not in _SPD_CACHE:
        sd = colour.SDS_ILLUMINANTS[illuminant]
        src_wl = np.asarray(sd.wavelengths, dtype=float)
        vals = np.asarray(sd.values, dtype=float)
        _SPD_CACHE[illuminant] = np.interp(WL_GRID, src_wl, vals)
    return _SPD_CACHE[illuminant]


def xyz_from_spectral(
    spectral: dict[int, float],
    illuminant: str = "D50",
    observer: str = "2",
) -> list[float]:
    """光谱反射率 → 三刺激值（k 归一化使照明体白点 Y=100）。"""
    wl = np.asarray(sorted(spectral), dtype=float)
    refl = np.asarray([spectral[int(w)] for w in wl], dtype=float)
    if wl.size < 4:
        raise ValueError("光谱点数不足")
    cmf = _cmfs(observer)
    spd = _spd(illuminant)
    # CMF/SPD 重采样到测量网格（测量网格可能与 CMF 原生网格不同）
    cmf_g = np.stack([np.interp(wl, WL_GRID, cmf[:, ch]) for ch in range(3)], axis=1)
    spd_g = np.interp(wl, WL_GRID, spd)
    with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
        k = 100.0 / float((spd_g * cmf_g[:, 1]).sum())
        sums = (spd_g[:, None] * refl[:, None] * cmf_g).sum(axis=0)
        xyz = k * sums
    if not np.all(np.isfinite(xyz)):
        raise ValueError("光谱积分结果非有限，请检查反射率量级与数据质量")
    return xyz.tolist()


def lab_from_xyz(xyz, white_xyz) -> list[float]:
    """CIE 1976 Lab（分段 f(t)），white_xyz 为积分归一化白点。"""
    xyz = np.maximum(np.asarray(xyz, dtype=float), 1e-12)
    wn = np.maximum(np.asarray(white_xyz, dtype=float), 1e-12)
    ratios = xyz / wn
    eps = 216.0 / 24389.0
    kap = 24389.0 / 27.0
    f = np.where(ratios > eps, np.cbrt(ratios), (kap * ratios + 16.0) / 116.0)
    return [float(116.0 * f[1] - 16.0), float(500.0 * (f[0] - f[1])), float(100.0 * (f[1] - f[2]))]


def white_xyz(illuminant: str = "D50", observer: str = "2") -> list[float]:
    """积分白点（Y≡100）。教科书自检：D50/2°≈(96.42,100,82.49)，D65/2°≈(95.05,100,108.90)。"""
    ones = {int(w): 1.0 for w in WL_GRID}
    return xyz_from_spectral(ones, illuminant, observer)


def integrate_lab(
    spectral: dict[int, float],
    illuminant: str = "D50",
    observer: str = "2",
) -> list[float]:
    """光谱 → Lab（D50/2° 等声明条件）。"""
    return lab_from_xyz(xyz_from_spectral(spectral, illuminant, observer), white_xyz(illuminant, observer))


def probe_lab_condition(
    xyz: list[float],
    reported_lab: list[float],
    declared: tuple[str, str] = ("D50", "2"),
) -> dict:
    """Lab↔XYZ 交叉校验：在候选条件中寻找一致口径。

    返回 {best_condition, best_de, declared_de, status}：
    - "consistent"：声明条件下 ΔE≤LAB_XYZ_TOL；
    - "consistent_other"：仅其它标准条件下一致（口径漂移，应补记条件）；
    - "mismatch"：全条件不一致（疑似转录错误）。
    """
    from ..algorithms.core import delta_e as _delta_e

    best_label, best_de = "", float("inf")
    declared_de = float("inf")
    for ill, obs in CONDITIONS:
        white = white_xyz(ill, obs)
        lab_c = lab_from_xyz(xyz, white)
        de = _delta_e(reported_lab, lab_c, "cie2000")
        if de < best_de:
            best_label, best_de = f"{ill}/{obs}°", de
        if (ill, obs) == declared:
            declared_de = de
    if declared_de <= LAB_XYZ_TOL:
        status = "consistent"
    elif best_de <= LAB_XYZ_TOL:
        status = "consistent_other"
    else:
        status = "mismatch"
    return {
        "best_condition": best_label,
        "best_de": round(best_de, 3),
        "declared_de": round(declared_de, 3),
        "status": status,
    }


# ---------------------------------------------------------------------------
# CGATS.17 解析
# ---------------------------------------------------------------------------

_SPECTRAL_RE = re.compile(r"^(?:NM|SPECTRAL_)(\d{3})$", re.IGNORECASE)
_DENSITY_RE = re.compile(r"^(?:D_|DENS_|CMYK_D_?)([CMYK])$", re.IGNORECASE)


def _parse_cgats(text: str) -> ParsedFile:
    out = ParsedFile(kind="cgats")
    fields: list[str] = []
    section = "header"
    declared_ill, declared_obs = "", ""
    n_fields = n_sets = None

    for raw_line in text.splitlines():
        line = raw_line.rstrip("\r\n")
        upper = line.strip().upper()

        if section == "header":
            if upper.startswith("BEGIN_DATA_FORMAT"):
                section = "format"
            elif "\t" in line:
                key, _, value = line.partition("\t")
                key = key.strip().upper()
                value = value.split("#", 1)[0].strip().strip('"')
                if key == "KEYWORD":
                    continue
                if key == "ILLUMINATION_NAME":
                    declared_ill = value.strip().upper()
                elif key == "OBSERVER_ANGLE":
                    declared_obs = value.strip().rstrip("°").strip()
                elif key in ("NUMBER_OF_FIELDS", "NUMBER_OF_SETS"):
                    n = _to_float(value)
                    if key == "NUMBER_OF_FIELDS":
                        n_fields = int(n) if n is not None else None
                    else:
                        n_sets = int(n) if n is not None else None
                out.meta[key] = value
            continue

        if section == "format":
            if upper.startswith("END_DATA_FORMAT"):
                section = "await_data"
            else:
                fields.extend(line.split())
            continue

        if section == "await_data":
            if upper.startswith("NUMBER_OF_SETS"):
                n = _to_float(line.split()[-1]) if line.split() else None
                n_sets = int(n) if n is not None else n_sets
                out.meta["NUMBER_OF_SETS"] = line.split()[-1] if line.split() else ""
            elif upper.startswith("BEGIN_DATA"):
                section = "data"
            continue

        # section == "data"
        if upper.startswith("END_DATA"):
            section = "done"
            continue
        tokens = line.split()
        if not tokens:
            continue
        if fields and len(tokens) != len(fields):
            out.notes.append(f"行字段数不符（{len(tokens)}/{len(fields)}），跳过：{line[:40]}")
            continue
        patch = ParsedPatch(label=(tokens[fields.index("SAMPLE_NAME")] if "SAMPLE_NAME" in fields else tokens[0]))
        for idx, name in enumerate(fields):
            up = name.upper()
            if up in ("SAMPLE_NAME",):
                continue
            val = _to_float(tokens[idx])
            if up == "LAB_L" and val is not None:
                patch.lab = [val, 0.0, 0.0]
            elif up in ("LAB_A", "LAB_B") and val is not None and patch.lab is not None:
                patch.lab[1 if up == "LAB_A" else 2] = val
            elif up == "XYZ_X" and val is not None:
                patch.xyz = [val, 0.0, 0.0]
            elif up in ("XYZ_Y", "XYZ_Z") and val is not None and patch.xyz is not None:
                patch.xyz[1 if up == "XYZ_Y" else 2] = val
            elif up == "RGB_R" and val is not None:
                patch.rgb = [val, 0.0, 0.0]
            elif up in ("RGB_G", "RGB_B") and val is not None and patch.rgb is not None:
                patch.rgb[1 if up == "RGB_G" else 2] = val
            else:
                m = _SPECTRAL_RE.match(up)
                if m and val is not None:
                    if patch.spectral is None:
                        patch.spectral = {}
                    patch.spectral[int(m.group(1))] = val
                    continue
                m = _DENSITY_RE.match(up)
                if m and val is not None:
                    if patch.densities is None:
                        patch.densities = {}
                    patch.densities[m.group(1).upper()] = val
        out.patches.append(patch)

    out.fields = fields
    if declared_ill and declared_obs:
        out.source_condition = f"{declared_ill}/{declared_obs}°"
    elif declared_ill:
        out.source_condition = declared_ill
    if n_fields is not None and fields and n_fields != len(fields):
        out.notes.append(f"NUMBER_OF_FIELDS={n_fields} 与实际字段数 {len(fields)} 不符")
    if n_sets is not None and n_sets != len(out.patches):
        out.notes.append(f"NUMBER_OF_SETS={n_sets} 与实际行数 {len(out.patches)} 不符")
    return out


# ---------------------------------------------------------------------------
# Xrite530 TSV 解析（Color Name 表头）
# ---------------------------------------------------------------------------


def _parse_tsv(text: str) -> ParsedFile:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        raise CgatsParseError("空文件")
    header = lines[0].split("\t")
    if "Color Name" not in header[0]:
        raise CgatsParseError("TSV 表头缺少 Color Name 列")
    if not any("STATUS" in h.upper() for h in header):
        raise CgatsParseError("TSV 表头缺少 Status 密度列")

    out = ParsedFile(kind="tsv")
    out.fields = header
    # 列布局：锚定标记列，标记后 3 列为该组数据（Lab/XYZ），LCh 3 列忽略，
    # "Status T C" 起连续 4 列为 C/M/Y/K 滤色密度
    col_lab = col_xyz = col_den = None
    for i, h in enumerate(header):
        hu = h.strip().upper()
        if hu.startswith("CIE LAB") and col_lab is None:
            col_lab = i
        elif hu.startswith("CIE XYZ") and col_xyz is None:
            col_xyz = i
        elif hu.startswith("STATUS") and col_den is None:
            col_den = i

    for line in lines[1:]:
        tokens = line.split("\t")
        tokens = [t.strip() for t in tokens]
        while tokens and tokens[-1] == "":
            tokens.pop()
        if not tokens:
            continue
        patch = ParsedPatch(label=tokens[0])
        if col_den is not None:
            den = {}
            for ch, off in zip("CMYK", range(4)):
                v = _to_float(tokens[col_den + off]) if col_den + off < len(tokens) else None
                if v is not None:
                    den[ch] = v
            if den:
                patch.densities = den
        if col_lab is not None:
            lab = [_to_float(tokens[col_lab + j]) if col_lab + j < len(tokens) else None for j in range(3)]
            if all(v is not None for v in lab):
                patch.lab = [float(v) for v in lab]
        if col_xyz is not None:
            xyz = [_to_float(tokens[col_xyz + j]) if col_xyz + j < len(tokens) else None for j in range(3)]
            if all(v is not None for v in xyz):
                patch.xyz = [float(v) for v in xyz]
        out.patches.append(patch)
    return out


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def parse_measurement_text(raw: bytes) -> ParsedFile:
    """解析测量导出文本（自动识别 CGATS.17 / Xrite530 TSV）。"""
    text, enc = decode_bytes(raw)
    if re.search(r"BEGIN_DATA_FORMAT", text, re.IGNORECASE):
        parsed = _parse_cgats(text)
    else:
        first = next((ln for ln in text.splitlines() if ln.strip()), "")
        if "Color Name" in first:
            parsed = _parse_tsv(text)
        else:
            raise CgatsParseError("无法识别的结构：既非 CGATS.17 也非 Color Name TSV")
    parsed.meta["_encoding"] = enc
    if parsed.kind == "cgats":
        for p in parsed.patches:
            if p.spectral and p.lab is None:
                ill, obs = _split_condition(parsed.source_condition) or ("D50", "2")
                p.lab_integrated = integrate_lab(p.spectral, ill, obs)
    return parsed


def _split_condition(condition: str) -> tuple[str, str] | None:
    m = re.match(r"^([A-Za-z0-9]+)/(\d+)", condition)
    return (m.group(1), m.group(2)) if m else None
