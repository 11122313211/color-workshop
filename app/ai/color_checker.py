"""AI 层：色卡检测、ΔE 预测、闭环迭代引擎。

MVP 阶段采用传统 CV（颜色量化 + 连通区域分析）实现色卡检测，
对标准色卡（ColorChecker 等）鲁棒，符合"物理先验优先"原则。
ONNX Runtime 接口预留，后续可替换为训练好的深度学习模型。
"""

from __future__ import annotations

import base64
import io
from dataclasses import dataclass

import numpy as np
from PIL import Image
from scipy import ndimage

from ..algorithms import convert_color, delta_e


# ---------------------------------------------------------------------------
# 色卡检测
# ---------------------------------------------------------------------------

@dataclass
class PatchDetection:
    """单个色块检测结果。"""
    index: int
    x: float
    y: float
    width: float
    height: float
    rgb: list[float]
    lab: list[float]


def detect_color_checker(
    image_bytes: bytes,
    expected_patches: int = 24,
    space: str = "srgb",
) -> tuple[list[PatchDetection], bool, float]:
    """
    检测图像中的色卡色块。

    实现：RGB 颜色量化 + scipy 连通区域分析，找出实心色块区域。
    不过滤灰阶色块（ColorChecker 含灰阶），通过填充率和形状验证排除误检。

    参数:
        image_bytes: 图像字节
        expected_patches: 预期色块数量
        space: 色彩空间
    返回:
        (色块列表, 是否检测到, 质量评分 0-100)
    """
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    arr = np.asarray(img, dtype=np.float64) / 255.0
    h, w = arr.shape[0], arr.shape[1]
    total_px = h * w

    # 1. RGB 颜色量化：每通道 8 级，共 512 桶（粒度适中，色块边界清晰）
    bins = 8
    quant = (arr * bins).astype(int)
    quant = np.clip(quant, 0, bins - 1)

    # 2. 将三维索引映射为一维，统计各颜色桶像素数
    flat = quant.reshape(-1, 3)
    keys = flat[:, 0] * (bins ** 2) + flat[:, 1] * bins + flat[:, 2]
    unique_keys, counts = np.unique(keys, return_counts=True)

    # 3. 筛选：保留像素数足够的颜色桶，排除背景色（覆盖图像比例过大的桶）
    min_pixels = max(total_px * 0.002, 25)
    max_pixels = total_px * 0.35  # 超过 35% 的桶视为背景，排除
    candidate_keys = [
        k for k, c in zip(unique_keys, counts)
        if c >= min_pixels and c <= max_pixels
    ]
    if not candidate_keys:
        return [], False, 0.0

    # 4. 对每个候选颜色桶做连通区域分析，找出实心色块
    patches = []
    seen_regions: list[tuple[int, int, int, int]] = []

    for key in candidate_keys:
        # 从 key 反推 RGB 分箱索引
        b3 = key % bins
        b2 = (key // bins) % bins
        b1 = key // (bins ** 2)

        # 该颜色桶的掩码
        mask = (quant[:, :, 0] == b1) & (quant[:, :, 1] == b2) & (quant[:, :, 2] == b3)
        if mask.sum() < min_pixels:
            continue

        # 连通区域分析
        labeled, num_regions = ndimage.label(mask)
        if num_regions == 0:
            continue

        # 找最大的连通区域
        region_sizes = ndimage.sum(mask, labeled, range(1, num_regions + 1))
        largest_idx = int(np.argmax(region_sizes)) + 1
        region_mask = labeled == largest_idx
        region_area = int(region_mask.sum())

        # 验证填充率：实心色块填充率应 > 0.4
        ys, xs = np.where(region_mask)
        x0, x1 = xs.min(), xs.max()
        y0, y1 = ys.min(), ys.max()
        box_area = (x1 - x0 + 1) * (y1 - y0 + 1)
        fill_ratio = region_area / max(box_area, 1)
        if fill_ratio < 0.4:
            continue

        # 验证形状：避免过于细长
        aspect = max(x1 - x0, y1 - y0) / max(min(x1 - x0, y1 - y0), 1)
        if aspect > 4.0:
            continue

        # 避免与已检测区域重叠
        bbox = (x0, y0, x1, y1)
        overlap = False
        for (px0, py0, px1, py1) in seen_regions:
            ix = max(0, min(x1, px1) - max(x0, px0))
            iy = max(0, min(y1, py1) - max(y0, py0))
            if ix > 0 and iy > 0:
                if (ix * iy) / max(region_area, 1) > 0.5:
                    overlap = True
                    break
        if overlap:
            continue

        # 取区域内像素均值作为代表色
        region_pixels = arr[ys, xs]
        avg_rgb = region_pixels.mean(axis=0)

        patches.append(PatchDetection(
            index=len(patches),
            x=(x0 + x1) / 2 / w,
            y=(y0 + y1) / 2 / h,
            width=(x1 - x0 + 1) / w,
            height=(y1 - y0 + 1) / h,
            rgb=[float(avg_rgb[0]), float(avg_rgb[1]), float(avg_rgb[2])],
            lab=convert_color(list(avg_rgb), space, "lab"),
        ))
        seen_regions.append(bbox)

    # 5. 按从左到右、从上到下排序
    patches.sort(key=lambda p: (round(p.y, 2), p.x))
    for i, p in enumerate(patches):
        p.index = i

    detected = len(patches) >= min(expected_patches, 4)
    quality = min(100.0, len(patches) / expected_patches * 100.0) if expected_patches > 0 else 0.0
    return patches, detected, round(quality, 1)


def generate_calibration_profile(
    patches: list[PatchDetection],
    reference_rgb: list[list[float]] | None = None,
    space: str = "srgb",
) -> dict:
    """
    基于色卡检测结果生成标定校正矩阵。

    参考色卡已知各色块参考 RGB，计算测量值与参考值的校正矩阵。
    MVP 阶段返回基于色差加权的校正矩阵（线性近似）。
    """
    if not patches:
        return {"matrix": [[1, 0, 0], [0, 1, 0], [0, 0, 1]], "quality": 0.0}

    if reference_rgb is None:
        return {
            "matrix": [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
            "quality": 0.5,
            "note": "无参考色卡值，仅记录测量数据",
        }

    # 构建测量->参考的线性校正（最小二乘）
    measured = np.array([p.rgb for p in patches])
    target = np.array(reference_rgb[:len(patches)])
    M = np.linalg.lstsq(measured, target, rcond=None)[0].T
    return {"matrix": M.tolist(), "quality": 0.9, "patches": len(patches)}


# ---------------------------------------------------------------------------
# ΔE 预测与闭环迭代
# ---------------------------------------------------------------------------

def predict_delta_e_after_correction(
    measured_rgb: list[float],
    target_rgb: list[float],
    correction_strength: float,
    space: str = "srgb",
    method: str = "cie2000",
) -> float:
    """
    预测施加校正后的 ΔE。

    校正强度 0-1，表示向目标颜色靠拢的比例。
    预测值 = 原始 ΔE * (1 - 校正强度 * 0.85)（近似模型，留 15% 残余误差）。
    """
    de_before = delta_e_from_rgb(measured_rgb, target_rgb, space, method)
    predicted = de_before * (1.0 - correction_strength * 0.85)
    return max(0.0, predicted)


def closed_loop_correct(
    initial_measured_rgb: list[float],
    target_rgb: list[float],
    space: str = "srgb",
    method: str = "cie2000",
    tolerance: float = 2.0,
    max_iterations: int = 10,
) -> dict:
    """
    闭环校正迭代引擎：测—校—验—再校，自动迭代直至 ΔE 达标。

    每次迭代：
      1. 计算当前测量与目标的 ΔE
      2. 若达标则停止
      3. 否则生成校正量（向目标方向移动，步长自适应递减）
      4. 模拟校正后测量值
    返回迭代轨迹与最终收敛状态。
    """
    measured = list(initial_measured_rgb)
    iterations = []
    converged = False

    for i in range(1, max_iterations + 1):
        de = delta_e_from_rgb(measured, target_rgb, space, method)
        converged = de <= tolerance

        iterations.append({
            "iteration": i,
            "measured_rgb": [round(v, 4) for v in measured],
            "delta_e": round(de, 4),
            "correction_applied": [0.0, 0.0, 0.0],
            "converged": converged,
        })

        if converged:
            break

        # 自适应步长：越接近目标步长越小
        step = 0.85 * (1.0 - i / max(max_iterations, 1))
        correction = [(t - m) * step for t, m in zip(target_rgb, measured)]
        measured = [round(m + c, 4) for m, c in zip(measured, correction)]

    return {
        "converged": converged,
        "iterations": iterations,
        "final_delta_e": round(delta_e_from_rgb(measured, target_rgb, space, method), 4),
        "iterations_used": len(iterations),
        "final_measured_rgb": [round(v, 4) for v in measured],
    }


def delta_e_from_rgb(
    rgb1, rgb2, space: str = "srgb", method: str = "cie2000",
) -> float:
    """便捷色差计算（委托给算法层）。"""
    return delta_e(
        convert_color(rgb1, space, "lab"),
        convert_color(rgb2, space, "lab"),
        method=method,
    )


def decode_base64_image(b64: str) -> bytes:
    """解码 base64 图像。"""
    if "," in b64 and b64.startswith("data:"):
        b64 = b64.split(",", 1)[1]
    return base64.b64decode(b64)
