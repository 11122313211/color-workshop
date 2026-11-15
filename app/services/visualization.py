"""色域可视化服务：CIE 1931 色度图、色域覆盖、ΔE 分布。

将色彩科学计算转换为可视化数据，供前端渲染。
"""

from __future__ import annotations

import numpy as np
import colour

from ..algorithms.core import _rgb_batch_to_xyz, _sample_gamut_lab, compute_gamut_volume, gamut_coverage

# 光谱轨迹计算结果缓存（CMF 数据固定，进程内只需计算一次）
_spectrum_cache: list[dict] | None = None


def cie1931_spectrum() -> list[dict]:
    """计算 CIE 1931 2° 标准观察者光谱轨迹（380-700nm @2nm）。

    科学依据：每个波长 λ 的单色刺激 XYZ = (x̄(λ), ȳ(λ), z̄(λ))（CIE 1931 2° CMF，
    单位辐射功率），色度坐标 xy = XYZ / (X+Y+Z)。CMF 取自 colour-science 内置
    的权威数据（1nm 插值），不再使用手敲近似表。

    返回:
        [{"wavelength": nm, "x": ..., "y": ..., "hex": "#rrggbb"}, ...]
        hex 为该单色光的 sRGB 近似显示色（色域外裁剪），供前端沿轨迹上色。
    """
    global _spectrum_cache
    if _spectrum_cache is not None:
        return _spectrum_cache

    cmfs = colour.MSDS_CMFS["CIE 1931 2 Degree Standard Observer"]
    wavelengths = np.arange(380, 701, 1)
    xyz = np.asarray(cmfs[wavelengths], dtype=np.float64)
    # 少量波长 CMF 和可能为 0/负（数值噪声），防除零
    totals = xyz.sum(axis=1, keepdims=True)
    totals[totals == 0] = 1e-12
    xy = xyz / totals

    # 单色光的 sRGB 近似显示色：先转 sRGB，再按最大分量归一化（保持色相、
    # 提到全饱和——标准光谱带渲染做法），最后裁剪。直接裁剪会让红/紫端过暗。
    rgb = np.asarray(colour.XYZ_to_sRGB(xyz), dtype=np.float64)
    maxc = rgb.max(axis=1, keepdims=True)
    maxc[maxc <= 1e-12] = 1.0
    rgb = np.clip(rgb / maxc, 0.0, 1.0)
    rgb8 = (rgb * 255).round().astype(int)

    locus = [
        {
            "wavelength": int(wl),
            "x": round(float(px), 4),
            "y": round(float(py), 4),
            "hex": "#{:02x}{:02x}{:02x}".format(r, g, b),
        }
        for wl, (px, py), (r, g, b) in zip(wavelengths, xy[:, :2], rgb8)
    ]
    _spectrum_cache = locus
    return locus


def spectrum_locus() -> dict:
    """光谱轨迹 + 紫线（380nm 与 700nm 端点连线闭合马蹄形）。"""
    locus = cie1931_spectrum()
    first, last = locus[0], locus[-1]
    return {
        "locus": locus,
        "purple_line": [
            [first["x"], first["y"]],
            [last["x"], last["y"]],
        ],
    }


def gamut_polygon(space: str) -> list[list[float]]:
    """
    计算指定色彩空间在 CIE 1931 xy 色度图上的色域多边形顶点。

    RGB 设备色域 = 三原色色度坐标构成的三角形（马蹄图上直线混合轨迹），
    只返回红、绿、蓝 3 个顶点；白点是三角形内的混合结果，不构成边界，
    混入多边形顶点会把三角形画成凹四边形。
    """
    standard_primaries = {
        "srgb":      [(0.640, 0.330), (0.300, 0.600), (0.150, 0.060)],
        "adobe-rgb": [(0.640, 0.330), (0.210, 0.710), (0.150, 0.060)],
        "dci-p3":    [(0.680, 0.320), (0.265, 0.690), (0.150, 0.060)],
        "rec2020":   [(0.708, 0.292), (0.170, 0.797), (0.131, 0.046)],
        "prophoto":  [(0.7347, 0.2653), (0.1596, 0.8404), (0.0366, 0.0001)],
    }
    prims = standard_primaries.get(space, standard_primaries["srgb"])
    return [[round(v, 4) for v in p] for p in prims]


def gamut_visualization_data(spaces: list[str]) -> dict:
    """生成多空间色域可视化数据。"""
    volumes = {s: round(compute_gamut_volume(s), 1) for s in spaces}
    coverages = {s: round(gamut_coverage(s, "srgb"), 1) for s in spaces}
    polygons = {s: gamut_polygon(s) for s in spaces}
    return {
        "spaces": spaces,
        "volumes": volumes,
        "coverages": coverages,
        "cie_spectrum": cie1931_spectrum(),
        "gamut_polygons": polygons,
    }


def delta_e_distribution(
    target_rgb: list[float],
    measured_points: list[list[float]],
    space: str = "srgb",
    method: str = "cie2000",
) -> dict:
    """生成 ΔE 分布数据：每个测量点与目标的色差。"""
    from ..algorithms import delta_e_from_rgb
    distribution = []
    for i, rgb in enumerate(measured_points):
        de = delta_e_from_rgb(rgb, target_rgb, space, method)
        distribution.append({
            "index": i,
            "delta_e": round(de, 4),
            "passed": de <= 2.0,
            "rgb": [round(v, 4) for v in rgb],
        })
    return {
        "target_rgb": target_rgb,
        "distribution": distribution,
        "max_delta_e": round(max(d["delta_e"] for d in distribution), 4) if distribution else 0.0,
        "pass_rate": round(
            sum(1 for d in distribution if d["passed"]) / len(distribution) * 100, 1
        ) if distribution else 0.0,
    }
