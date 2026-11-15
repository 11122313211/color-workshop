"""LAB→CMYK 配方反查引擎（产品旗舰能力）。

业务链路：目标 LAB（客户色样/色卡标准值）→ 基于工艺 ICC 反查最优墨量配方 →
打印 → 分光光度计实测 → 实测值回填修正（闭环迭代）。

实现：
- 正向预测：ImageCms 构建 设备空间→LAB 变换，一次批量像素变换 = 一批正向预测
- 反查：粗查表（设备空间网格采样，缓存复用）→ Lab 最近邻种子 →
  局部迭代精搜（坐标下降 + 步长细化，大量正向预测直至 ΔE 达标）
- 实测验证闭环：模型误差补偿（p' = t - (m - p)）后再次反查

精度说明：ImageCms LAB 输出为 8-bit 量化（L 步长 ≈ 0.39，a/b 步长 ≈ 1.0），
预测 ΔE 的不确定度约 ±0.4；ΔE 低于 0.5 时提前停止精搜，避免无效预测。
"""

from __future__ import annotations

import hashlib
import io
import uuid
from datetime import datetime
from typing import Callable

import numpy as np
from PIL import Image, ImageCms

from ..algorithms.core import delta_e, delta_e_batch
from .filedb import JsonStore
from .processes import DATA_DIR, SPACE_CHANNELS

# 每通道名（配方展示）
CHANNEL_NAMES = {4: ["C", "M", "Y", "K"], 3: ["R", "G", "B"], 1: ["K"]}

# 设备通道数 -> lcms 输入签名候选（GRAY 必须 "L"：buildTransform 接受 "GRAY"
# 但 applyTransform 时图像模式恒为 "L" 必然 mismatch）
_IN_MODE_CANDIDATES = {1: ["L", "GRAY"], 3: ["RGB"], 4: ["CMYK"]}

# 粗查表网格密度（每通道档位数）：17 档 = 6.25% 步长
LUT_LEVELS = 17

# 8-bit LAB 量化地板：ΔE 低于此值停止精搜（继续搜索无意义）
DE_FLOOR = 0.5

# 进程内缓存：按 profile 内容哈希复用变换与粗查表
_transforms: dict[str, ImageCms.ImageCmsTransform] = {}
_luts: dict[str, tuple[np.ndarray, np.ndarray]] = {}


def _sha(icc_bytes: bytes) -> str:
    return hashlib.sha256(icc_bytes).hexdigest()[:16]


def _image_mode(channels: int) -> str:
    """设备通道数 -> Pillow 图像模式（构建输入图像用）。"""
    if channels == 4:
        return "CMYK"
    if channels == 3:
        return "RGB"
    return "L"


def _build_forward(icc_bytes: bytes, channels: int) -> Callable:
    """构建正向评估器 forward(values(NxC, 0-1)) -> Lab(Nx3)。

    变换对象与粗查表按 profile 内容哈希缓存，同一工艺重复预测零开销。
    """
    key = _sha(icc_bytes)
    if key not in _transforms:
        prof_dev = ImageCms.getOpenProfile(io.BytesIO(icc_bytes))
        prof_lab = ImageCms.createProfile("LAB")
        mode = _image_mode(channels)
        last_err: Exception | None = None
        xform = None
        for in_mode in _IN_MODE_CANDIDATES.get(channels, ["RGB"]):
            try:
                xform = ImageCms.buildTransform(
                    prof_dev, prof_lab, in_mode, "LAB",
                    renderingIntent=ImageCms.Intent.RELATIVE_COLORIMETRIC,
                )
                break
            except Exception as e:  # noqa: BLE001 —— 逐候选尝试签名
                last_err = e
        if xform is None:
            raise ValueError(f"无法为该 ICC 构建 {mode}→LAB 变换: {last_err}")
        _transforms[key] = xform

    xform = _transforms[key]
    mode = _image_mode(channels)

    def forward(values: np.ndarray) -> np.ndarray:
        n = values.shape[0]
        arr = (np.clip(values, 0.0, 1.0) * 255.0).round().astype("uint8")
        # L 模式需 2D 数组；RGB/CMYK 为 (n,1,C)
        shape = (n, channels) if mode == "L" else (n, 1, channels)
        img = Image.fromarray(arr.reshape(shape), mode=mode)
        out = ImageCms.applyTransform(img, xform)
        # PIL LAB 模式存储：L 字节 = L*2.55；a/b 字节 = int8 补码（有符号 a*/b*）
        raw = np.asarray(out, dtype=np.uint8).reshape(n, 3)
        lab = np.empty((n, 3), dtype=np.float64)
        lab[:, 0] = raw[:, 0] / 2.55
        lab[:, 1:] = raw[:, 1:].astype(np.int8)
        return lab

    return forward


def predict_recipe(
    icc_bytes: bytes,
    device_space: str,
    target_lab,
    tolerance: float = 2.0,
    method: str = "cie2000",
    max_rounds: int = 6,
) -> dict:
    """给定目标 LAB，反查最优墨量配方。

    返回:
        target_lab / predicted_lab / recipe(通道名→百分比) / delta_e /
        converged / iterations(ΔE 轨迹) / predictions(本次正向预测数) /
        lut_points(粗查表规模)
    """
    channels = SPACE_CHANNELS.get(device_space, 3)
    forward = _build_forward(icc_bytes, channels)
    target = np.asarray(target_lab, dtype=np.float64)

    key = _sha(icc_bytes)
    lut_key = f"{key}:{LUT_LEVELS}:{channels}"
    if lut_key not in _luts:
        axes = np.linspace(0.0, 1.0, LUT_LEVELS)
        grids = np.meshgrid(*([axes] * channels), indexing="ij")
        pts = np.stack([g.ravel() for g in grids], axis=1)
        _luts[lut_key] = (pts, forward(pts))
    lut_pts, lut_labs = _luts[lut_key]

    predictions = 0
    # Lab 最近邻粗排，取 3 个种子并行精搜
    dist = np.linalg.norm(lut_labs - target, axis=1)
    seed_idx = np.argsort(dist)[:3]

    best: dict | None = None
    for si in seed_idx:
        cur_val = lut_pts[si].copy()
        cur_lab = lut_labs[si].copy()
        cur_de = float(delta_e(cur_lab, target, method))
        traj = [round(cur_de, 3)]
        step = 1.0 / (LUT_LEVELS - 1)

        for _ in range(max_rounds):
            offsets = np.array([-step, 0.0, step])
            grids = np.meshgrid(*([offsets] * channels), indexing="ij")
            cand = np.clip(
                np.stack([g.ravel() for g in grids], axis=1) + cur_val, 0.0, 1.0
            )
            cand_labs = forward(cand)
            predictions += len(cand)
            des = delta_e_batch(cand_labs, target, method)
            k = int(np.argmin(des))
            if des[k] + 1e-9 < cur_de:
                cur_de = float(des[k])
                cur_val = cand[k].copy()
                cur_lab = cand_labs[k]
                traj.append(round(cur_de, 3))
            else:
                # 该步长无改进 -> 细化步长（轨迹不重复记录同一 ΔE）
                step /= 2.0
                if step < 1.0 / 255.0:
                    break
            if cur_de <= DE_FLOOR:
                break

        if best is None or cur_de < best["delta_e"]:
            best = {
                "value": cur_val,
                "lab": cur_lab,
                "delta_e": cur_de,
                "trajectory": traj,
            }

    assert best is not None
    names = CHANNEL_NAMES.get(channels, [f"CH{i}" for i in range(channels)])
    return {
        "target_lab": [round(float(v), 2) for v in target],
        "predicted_lab": [round(float(v), 2) for v in best["lab"]],
        "recipe": {
            name: round(float(v) * 100.0, 1)
            for name, v in zip(names, best["value"])
        },
        "delta_e": round(best["delta_e"], 3),
        "converged": best["delta_e"] <= tolerance,
        "iterations": best["trajectory"],
        "predictions": predictions + int(len(lut_pts)),
        "lut_points": int(len(lut_pts)),
    }


def forward_query(
    icc_bytes: bytes,
    device_space: str,
    values_pct,
) -> dict:
    """正向查询：给定墨量配方（百分比）→ 预测 Lab。

    用于工具箱的 CMYK→LAB 正查，与反查引擎共用同一变换与解码。
    """
    channels = SPACE_CHANNELS.get(device_space, 3)
    forward = _build_forward(icc_bytes, channels)
    values = np.asarray(values_pct, dtype=np.float64).reshape(1, channels) / 100.0
    lab = forward(values)[0]
    names = CHANNEL_NAMES.get(channels, [f"CH{i}" for i in range(channels)])
    return {
        "values": {
            name: round(float(v) * 100.0, 1) for name, v in zip(names, values[0])
        },
        "lab": [round(float(v), 2) for v in lab],
        "hex": _lab_to_hex(lab),
    }


def _lab_to_hex(lab) -> str:
    """Lab（D50 PCS）→ sRGB hex（Bradford 线性矩阵色适应 D50→D65），用于前端色块。"""
    import colour

    d50 = colour.CCS_ILLUMINANTS["CIE 1931 2 Degree Standard Observer"]["D50"]
    xyz_d50 = np.asarray(colour.Lab_to_XYZ(np.asarray(lab), illuminant=d50))
    # Bradford D50→D65 色适应（Lindbloom 线性矩阵，与前端 JS 实现一致）
    bradford = np.array(
        [
            [0.9555766, -0.0230393, 0.0631636],
            [-0.0282895, 1.0099416, 0.0210077],
            [0.0122982, -0.0204830, 1.3299098],
        ]
    )
    xyz_d65 = bradford @ xyz_d50
    rgb = np.clip(colour.XYZ_to_sRGB(xyz_d65) * 255, 0, 255).round().astype(int)
    return "#{:02x}{:02x}{:02x}".format(*rgb)


def run_verification(
    icc_bytes: bytes,
    device_space: str,
    recipe_doc: dict,
    measured_lab,
    tolerance: float = 2.0,
    method: str = "cie2000",
) -> dict:
    """打印实测验证闭环：模型误差补偿后再次反查。

    模型：配方实际输出 m 偏离预测 p，误差 e = m - p；
    要使实际输出达到目标 t，需让预测值落在 t - e，即 p' = t - (m - p)。
    """
    target = np.asarray(recipe_doc["target_lab"], dtype=np.float64)
    predicted = np.asarray(recipe_doc["predicted_lab"], dtype=np.float64)
    measured = np.asarray(measured_lab, dtype=np.float64)

    de_measured = float(delta_e(measured, target, method))
    de_model = float(delta_e(predicted, measured, method))

    adjusted = target - (measured - predicted)
    adjusted = np.clip(adjusted, [0.0, -128.0, -128.0], [100.0, 128.0, 128.0])

    corr = predict_recipe(icc_bytes, device_space, adjusted, tolerance, method)

    verification = {
        "measured_lab": [round(float(v), 2) for v in measured],
        "delta_e_measured": round(de_measured, 3),
        "delta_e_model_error": round(de_model, 3),
        "adjusted_target_lab": [round(float(v), 2) for v in adjusted],
        "corrected_recipe": corr["recipe"],
        "corrected_predicted_lab": corr["predicted_lab"],
        "delta_e_corrected": corr["delta_e"],
        "improved": corr["delta_e"] < de_measured,
        "iterations": corr["iterations"],
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    return verification


class RecipeStore:
    """配方历史记录存储（data/recipes.json）。"""

    def __init__(self) -> None:
        self._store = JsonStore(DATA_DIR / "recipes.json")

    def create(self, process_doc: dict, result: dict, label: str = "") -> dict:
        rid = uuid.uuid4().hex[:12]
        doc = {
            "id": rid,
            "label": label,
            "process_id": process_doc["id"],
            "process_name": process_doc["name"],
            "device_space": process_doc["icc_meta"]["device_space"],
            "target_lab": result["target_lab"],
            "recipe": result["recipe"],
            "predicted_lab": result["predicted_lab"],
            "delta_e": result["delta_e"],
            "method": result.get("method", "cie2000"),
            "tolerance": result.get("tolerance", 2.0),
            "converged": result.get("converged", False),
            "iterations": result["iterations"],
            "predictions": result["predictions"],
            "lut_points": result["lut_points"],
            "verifications": [],
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        self._store.put(rid, doc)
        return doc

    def get(self, rid: str) -> dict | None:
        return self._store.get(rid)

    def list(self, process_id: str | None = None) -> list[dict]:
        docs = list(self._store.all().values())
        if process_id:
            docs = [d for d in docs if d.get("process_id") == process_id]
        docs.sort(key=lambda d: d.get("created_at", ""), reverse=True)
        return docs

    def delete(self, rid: str) -> bool:
        return self._store.delete(rid)

    def remove_process(self, process_id: str) -> int:
        """级联清理：删除某工艺的全部配方记录（工艺删除时调用）。"""
        removed = 0
        for rid, doc in self._store.all().items():
            if doc.get("process_id") == process_id:
                self._store.delete(rid)
                removed += 1
        return removed

    def append_verification(self, rid: str, verification: dict) -> dict | None:
        doc = self._store.get(rid)
        if not doc:
            return None
        doc.setdefault("verifications", []).append(verification)
        self._store.put(rid, doc)
        return doc
