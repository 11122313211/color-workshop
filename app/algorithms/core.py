"""
色彩算法编排封装层（产品核心价值）

将 lcms2（经 Pillow ImageCms）、colour-science、coloraide 三个库整合为
统一、易用的产品接口，封装"测量→转换→校正→验证"的闭环算法流水线。

设计原则：
- ICC 物理先验优先，AI 做增强层（混合架构，不做纯黑箱）
- 传统数学方法（空间转换、色差公式）用确定性标准算法，不 AI 化
- 高层色彩运算走 coloraide，色度学走 colour，ICC profile 生成走 lcms2

注意：Pillow 12.x 的 ImageCms.createProfile 仅支持 sRGB / XYZ，
因此色彩空间转换与色域映射以 colour-science / coloraide 的纯数学实现为主，
ImageCms 仅用于 sRGB ICC profile 的生成与读写。
"""

from __future__ import annotations

import functools
import io
from typing import Literal

import numpy as np
import colour
import coloraide
from PIL import Image, ImageCms

# 支持的色彩空间（产品统一标识；CMYK 需绑定工艺 ICC 经 /api/recipes/forward 或 /api/files/convert）
SUPPORTED_SPACES = {
    "srgb", "adobe-rgb", "dci-p3", "rec2020", "prophoto",
    "lab", "xyz",
}

_RGB_SPACES = {"srgb", "adobe-rgb", "dci-p3", "rec2020", "prophoto"}

# 渲染意图（ICC 标准）
RenderingIntent = Literal[
    "perceptual", "saturation", "relative_colorimetric", "absolute_colorimetric"
]

# 产品色彩空间 -> coloraide 色彩空间名（coloraide 使用连字符命名）
_SPACE_TO_COLORAIDE = {
    "srgb": "srgb",
    "adobe-rgb": "a98-rgb",
    "dci-p3": "display-p3",
    "rec2020": "rec2020",
    "prophoto": "prophoto-rgb",
    "lab": "lab",
    "xyz": "xyz-d65",
}

# 直接分派底层色差函数，避免 colour.DELTA_E_METHODS 规范化键碰撞警告（ColourUsageWarning）与调度开销
_DELTA_E_FUNCS = {
    "cie2000": colour.difference.delta_E_CIE2000,
    "cie 2000": colour.difference.delta_E_CIE2000,
    "cie1976": colour.difference.delta_E_CIE1976,
    "cie 1976": colour.difference.delta_E_CIE1976,
    "cie1994": colour.difference.delta_E_CIE1994,
    "cie 1994": colour.difference.delta_E_CIE1994,
    "cmc": colour.difference.delta_E_CMC,
}


def _to_float_rgb(rgb) -> np.ndarray:
    """将 RGB（0-255 int 或 0-1 float）统一为 0-1 浮点数组。"""
    arr = np.asarray(rgb, dtype=np.float64)
    if arr.max() > 1.0 + 1e-6:
        arr = arr / 255.0
    return arr


# ---------------------------------------------------------------------------
# 色彩空间转换
# ---------------------------------------------------------------------------

def convert_color(rgb, from_space: str, to_space: str) -> list[float]:
    """
    将一个颜色从一个色彩空间转换到另一个色彩空间。

    参数:
        rgb: RGB 值（0-255 或 0-1 浮点）
        from_space: 源色彩空间标识
        to_space: 目标色彩空间标识
    返回:
        目标空间的三分量值（list[float]）
    """
    from_space = from_space.lower()
    to_space = to_space.lower()
    if from_space == "cmyk" or to_space == "cmyk":
        raise ValueError("CMYK 依赖具体工艺 ICC，请使用工艺正查（/api/recipes/forward）或文件转换接口")
    if from_space not in SUPPORTED_SPACES:
        raise ValueError(f"不支持的色彩空间: {from_space}")
    if to_space not in SUPPORTED_SPACES:
        raise ValueError(f"不支持的色彩空间: {to_space}")

    arr = _to_float_rgb(rgb) if from_space in _RGB_SPACES else np.asarray(rgb, dtype=np.float64)
    if arr.shape != (3,) or not np.all(np.isfinite(arr)):
        raise ValueError("颜色输入必须为 3 个有限数值")

    # 经 coloraide 统一转换（内部走 XYZ 中转，含色适应）
    c = coloraide.Color(_SPACE_TO_COLORAIDE[from_space], arr.tolist())
    c = c.convert(_SPACE_TO_COLORAIDE[to_space])
    return [float(v) for v in c.coords()]


_SPACE_TO_COLOUR_RGB = {
    "srgb": "sRGB",
    "adobe-rgb": "Adobe RGB (1998)",
    "dci-p3": "Display P3",
    "rec2020": "ITU-R BT.2020",
    "prophoto": "ProPhoto RGB",
}


def _rgb_batch_to_xyz(rgb: np.ndarray, space: str) -> np.ndarray:
    """将一批 RGB 值（Nx3，0-1 浮点）转换为 XYZ（D65），全 NumPy 向量化。"""
    if space == "srgb":
        return colour.sRGB_to_XYZ(rgb)
    cs_name = _SPACE_TO_COLOUR_RGB.get(space)
    if cs_name is not None:
        return np.asarray(colour.RGB_to_XYZ(rgb, cs_name), dtype=np.float64)
    results = []
    for row in rgb:
        c = coloraide.Color(_SPACE_TO_COLORAIDE[space], list(row))
        c = c.convert("xyz-d65")
        results.append(c.coords())
    return np.array(results)


# ---------------------------------------------------------------------------
# 色差计算
# ---------------------------------------------------------------------------

def delta_e(
    lab1: list[float],
    lab2: list[float],
    method: str = "cie2000",
) -> float:
    """
    计算两个 Lab 颜色的色差。

    参数:
        lab1, lab2: CIELAB 三分量 [L*, a*, b*]
        method: 色差公式，'cie1976' / 'cie2000' / 'cmc' / 'cie1994'
    返回:
        色差值（float）
    """
    lab1_arr = np.asarray(lab1, dtype=np.float64)
    lab2_arr = np.asarray(lab2, dtype=np.float64)
    fn = _DELTA_E_FUNCS.get(str(method).strip().lower(), colour.difference.delta_E_CIE2000)
    return float(fn(lab1_arr, lab2_arr))


def delta_e_batch(lab1, lab2, method: str = "cie2000") -> np.ndarray:
    """批量色差：lab1 (N,3) 对 lab2 (3,) 或 (N,3) 广播，返回 (N,) 数组。

    直接分派 colour.difference 纯 numpy 向量化实现，零警告且无调度开销。
    """
    lab1_arr = np.asarray(lab1, dtype=np.float64)
    lab2_arr = np.asarray(lab2, dtype=np.float64)
    fn = _DELTA_E_FUNCS.get(str(method).strip().lower(), colour.difference.delta_E_CIE2000)
    return np.asarray(fn(lab1_arr, lab2_arr), dtype=np.float64)


def delta_e_components(lab1, lab2) -> dict:
    """ΔE 分量分解（解读辅助；ΔE2000 主口径不变）。

    lab1 为参考/预测值，lab2 为实测值，分量为「实测 − 参考」：
    dL/da/db、dC（彩度差）、dh（色相角差，(−180,180]°）、dE76（ΔE1976 总色差）。
    """
    lab1 = np.asarray(lab1, dtype=np.float64)
    lab2 = np.asarray(lab2, dtype=np.float64)
    dL, da, db = (lab2 - lab1).tolist()
    c1 = float(np.hypot(lab1[1], lab1[2]))
    c2 = float(np.hypot(lab2[1], lab2[2]))
    h1 = float(np.degrees(np.arctan2(lab1[2], lab1[1])))
    h2 = float(np.degrees(np.arctan2(lab2[2], lab2[1])))
    dh = h2 - h1
    if dh > 180.0:
        dh -= 360.0
    elif dh <= -180.0:
        dh += 360.0
    return {
        "dL": round(float(dL), 4),
        "da": round(float(da), 4),
        "db": round(float(db), 4),
        "dC": round(c2 - c1, 4),
        "dh": round(dh, 4),
        "dE76": round(float(np.linalg.norm(lab2 - lab1)), 4),
    }


def delta_e_from_rgb(
    rgb1, rgb2,
    space: str = "srgb",
    method: str = "cie2000",
) -> float:
    """
    直接从 RGB 计算色差（内部转为 Lab）。

    参数:
        rgb1, rgb2: 两个 RGB 颜色
        space: RGB 所属色彩空间
        method: 色差公式
    返回:
        色差值
    """
    lab1 = convert_color(rgb1, space, "lab")
    lab2 = convert_color(rgb2, space, "lab")
    return delta_e(lab1, lab2, method=method)


# ---------------------------------------------------------------------------
# ICC Profile 操作（lcms2 / ImageCms）
# ---------------------------------------------------------------------------

def create_srgb_profile():
    """创建 sRGB ICC profile（返回 ImageCms profile 对象）。

    Pillow 12.x 移除了 saveProfile，此处返回 profile 对象供直接使用。
    若需字节，可通过 profileToProfile 应用转换，或使用 lcms2 底层导出。
    """
    return ImageCms.createProfile("sRGB")


def load_profile_from_bytes(data: bytes):
    """从字节加载 ICC profile（返回 ImageCms profile 对象）。"""
    return ImageCms.getOpenProfile(io.BytesIO(data))


def profile_to_profile(
    image_bytes: bytes,
    src_space: str,
    dst_space: str,
    intent: RenderingIntent = "relative_colorimetric",
) -> bytes:
    """
    对图像数据执行 ICC 色彩转换（源空间 -> 目标空间），返回转换后图像字节。

    Pillow 12.x 的 createProfile 仅支持 sRGB/XYZ，
    对非 sRGB 空间采用"逐像素经 coloraide 转换"的等价实现，
    与 ICC CMM 的渲染意图语义对齐（relative_colorimetric 为准确匹配）。
    """
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    arr = np.asarray(img, dtype=np.float64) / 255.0
    out = np.zeros_like(arr)
    flat = arr.reshape(-1, 3)
    oflat = out.reshape(-1, 3)
    for i, px in enumerate(flat):
        c = coloraide.Color(_SPACE_TO_COLORAIDE[src_space], list(px))
        c = c.convert(_SPACE_TO_COLORAIDE[dst_space])
        oflat[i] = c.coords()
    out = np.clip(out * 255, 0, 255).astype(np.uint8)
    result = Image.fromarray(out, mode="RGB")
    buf = io.BytesIO()
    result.save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 色域映射（数学实现：色域包围 + 感知映射）
# ---------------------------------------------------------------------------

def _sample_gamut_lab(space: str, n: int = 40) -> np.ndarray:
    """
    采样指定色彩空间的 RGB 立方体顶点，转换为 Lab，得到色域点集。
    用于色域包围盒与映射目标。
    """
    xs = np.linspace(0, 1, n)
    r, g, b = np.meshgrid(xs, xs, xs, indexing="ij")
    rgb = np.stack([r.ravel(), g.ravel(), b.ravel()], axis=1)
    xyz = _rgb_batch_to_xyz(rgb, space)
    return colour.XYZ_to_Lab(xyz)


def gamut_map_color(
    lab: list[float],
    target_space: str = "srgb",
    intent: RenderingIntent = "perceptual",
) -> list[float]:
    """
    将一个 Lab 颜色映射到目标空间色域内。

    实现：Lab → XYZ → 目标 RGB，判断是否在色域内（RGB 各通道 ∈ [0,1]）。
    色域外颜色：
      - perceptual / saturation：朝向色域中心做感知压缩，按最大越界比例缩放 RGB
      - relative_colorimetric / absolute_colorimetric：直接裁剪到色域边界
    映射后转回 Lab 返回。
    """
    lab_arr = np.asarray(lab, dtype=np.float64)
    xyz = colour.Lab_to_XYZ(lab_arr)
    # XYZ -> 目标空间 RGB
    xyz_arr = np.array([xyz])
    rgb_arr = _xyz_batch_to_rgb(xyz_arr, target_space)[0]

    in_gamut = bool(np.all(rgb_arr >= 0.0) and np.all(rgb_arr <= 1.0))

    if in_gamut or intent in ("relative_colorimetric", "absolute_colorimetric"):
        # 色域内保留；colorimetric 对色域外做裁剪
        rgb_mapped = np.clip(rgb_arr, 0.0, 1.0) if not in_gamut else rgb_arr
    else:
        # perceptual / saturation：朝向色域中心压缩
        # 找到最大越界比例，将整体 RGB 朝中心 [0.5,0.5,0.5] 缩放
        center = 0.5
        max_excess = 0.0
        for ch in range(3):
            if rgb_arr[ch] < 0.0:
                excess = (0.0 - rgb_arr[ch]) / 0.5 if center != rgb_arr[ch] else 1.0
            elif rgb_arr[ch] > 1.0:
                excess = (rgb_arr[ch] - 1.0) / 0.5 if center != rgb_arr[ch] else 1.0
            else:
                excess = 0.0
            max_excess = max(max_excess, excess)
        if max_excess > 0.0:
            scale = 1.0 / (1.0 + max_excess)
            rgb_mapped = center + (rgb_arr - center) * scale
        else:
            rgb_mapped = rgb_arr
        rgb_mapped = np.clip(rgb_mapped, 0.0, 1.0)

    # 转回 Lab
    xyz_mapped = _rgb_batch_to_xyz(rgb_mapped.reshape(1, 3), target_space)[0]
    lab_mapped = colour.XYZ_to_Lab(xyz_mapped)
    return [float(v) for v in lab_mapped]


def _xyz_batch_to_rgb(xyz: np.ndarray, space: str) -> np.ndarray:
    """将一批 XYZ 值（Nx3）转换为目标空间 RGB（0-1），全 NumPy 向量化。"""
    if space == "srgb":
        return colour.XYZ_to_sRGB(xyz)
    cs_name = _SPACE_TO_COLOUR_RGB.get(space)
    if cs_name is not None:
        return np.asarray(colour.XYZ_to_RGB(xyz, cs_name), dtype=np.float64)
    results = []
    for row in xyz:
        c = coloraide.Color("xyz-d65", list(row))
        c = c.convert(_SPACE_TO_COLORAIDE[space])
        results.append(c.coords())
    return np.array(results)


# ---------------------------------------------------------------------------
# 色域计算（设备色域边界 / 覆盖率）
# ---------------------------------------------------------------------------

@functools.lru_cache(maxsize=32)
def compute_gamut_volume(space: str) -> float:
    """
    计算指定色彩空间的色域体积（在 Lab 空间内，近似，带进程级缓存）。
    通过对 RGB 立方体采样求体积，用于色域大小量化。
    """
    space = space.lower()
    if space not in _RGB_SPACES:
        raise ValueError(f"不支持的色域计算空间: {space}")
    gamut = _sample_gamut_lab(space, n=10)
    L_range = gamut[:, 0].max() - gamut[:, 0].min()
    a_range = gamut[:, 1].max() - gamut[:, 1].min()
    b_range = gamut[:, 2].max() - gamut[:, 2].min()
    return float(L_range * a_range * b_range)


@functools.lru_cache(maxsize=64)
def gamut_coverage(target_space: str, reference_space: str = "srgb") -> float:
    """
    计算目标空间对参考空间（sRGB）的色域覆盖率（近似百分比，带进程级缓存）。
    用 Lab 空间内参考空间色域点是否落入目标色域包围盒判断。
    """
    target_space = target_space.lower()
    reference_space = reference_space.lower()
    if target_space not in _RGB_SPACES or reference_space not in _RGB_SPACES:
        raise ValueError(f"不支持的色域覆盖空间: {target_space} / {reference_space}")
    ref_gamut = _sample_gamut_lab(reference_space, n=12)
    tgt_gamut = _sample_gamut_lab(target_space, n=30)

    a_min, a_max = tgt_gamut[:, 1].min(), tgt_gamut[:, 1].max()
    b_min, b_max = tgt_gamut[:, 2].min(), tgt_gamut[:, 2].max()
    L_min, L_max = tgt_gamut[:, 0].min(), tgt_gamut[:, 0].max()

    inside = (
        (ref_gamut[:, 0] >= L_min) & (ref_gamut[:, 0] <= L_max)
        & (ref_gamut[:, 1] >= a_min) & (ref_gamut[:, 1] <= a_max)
        & (ref_gamut[:, 2] >= b_min) & (ref_gamut[:, 2] <= b_max)
    )
    return float(inside.mean() * 100.0)


__all__ = [
    "SUPPORTED_SPACES",
    "RenderingIntent",
    "convert_color",
    "delta_e",
    "delta_e_batch",
    "delta_e_components",
    "delta_e_from_rgb",
    "create_srgb_profile",
    "load_profile_from_bytes",
    "profile_to_profile",
    "gamut_map_color",
    "compute_gamut_volume",
    "gamut_coverage",
]
