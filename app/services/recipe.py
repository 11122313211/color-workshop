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
import threading
import uuid
from collections import OrderedDict
from datetime import datetime
from typing import Callable

import colour
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

# 插值精搜层：插值面（LUT 多线性插值，连续）负责排序与引导步长，真实 forward 定夺；
# 真实 ΔE 低于此值即提前收手（继续搜索收益趋零）
REFINE_FLOOR = 0.10
REFINE_STEP_MIN = 1.0 / 2048.0  # ≈0.05% 墨量
# 总开关（测试对照用：False 时完全走 V0.3 粗搜路径）
REFINE_ENABLED = True

# 有界进程内缓存：按 profile 内容哈希复用粗查表与插值器；Transform 按线程隔离防止 lcms2 跨线程并发风险
_CACHE_MAX = 24
_cache_lock = threading.RLock()
_tls = threading.local()
_luts: OrderedDict[str, tuple[np.ndarray, np.ndarray]] = OrderedDict()
_interps: OrderedDict[str, Callable] = OrderedDict()
_UNIT_OFFSETS: dict[int, np.ndarray] = {}

_D50_ILLUMINANT = colour.CCS_ILLUMINANTS["CIE 1931 2 Degree Standard Observer"]["D50"]
_BRADFORD_D50_TO_D65 = np.array(
    [
        [0.9555766, -0.0230393, 0.0631636],
        [-0.0282895, 1.0099416, 0.0210077],
        [0.0122982, -0.0204830, 1.3299098],
    ],
    dtype=np.float64,
)


def evict_icc_cache(sha16: str) -> None:
    """按 ICC 内容哈希清理 LUT 与插值缓存。"""
    prefix = f"{sha16}:"
    with _cache_lock:
        for cache in (_luts, _interps):
            for key in [k for k in cache if k.startswith(prefix)]:
                cache.pop(key, None)


def _thread_transforms() -> OrderedDict[str, ImageCms.ImageCmsTransform]:
    cache = getattr(_tls, "transforms", None)
    if cache is None:
        cache = OrderedDict()
        _tls.transforms = cache
    return cache


def _unit_offsets(channels: int) -> np.ndarray:
    arr = _UNIT_OFFSETS.get(channels)
    if arr is None:
        axes = np.array([-1.0, 0.0, 1.0], dtype=np.float64)
        grids = np.meshgrid(*([axes] * channels), indexing="ij")
        arr = np.stack([g.ravel() for g in grids], axis=1)
        _UNIT_OFFSETS[channels] = arr
    return arr


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

    变换对象按线程独立缓存（避免 lcms2 Transform 跨线程并发调用风险），有界 LRU 淘汰。
    """
    tkey = f"{_sha(icc_bytes)}:{channels}"
    transforms = _thread_transforms()
    xform = transforms.get(tkey)
    if xform is None:
        prof_dev = ImageCms.getOpenProfile(io.BytesIO(icc_bytes))
        prof_lab = ImageCms.createProfile("LAB")
        mode = _image_mode(channels)
        last_err: Exception | None = None
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
        transforms[tkey] = xform
        while len(transforms) > _CACHE_MAX:
            transforms.popitem(last=False)
    else:
        transforms.move_to_end(tkey)

    mode = _image_mode(channels)

    def forward(values) -> np.ndarray:
        vals = np.asarray(values, dtype=np.float64)
        n = vals.shape[0]
        arr = (np.clip(vals, 0.0, 1.0) * 255.0).round().astype("uint8")
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


def _make_interp(pts: np.ndarray, labs: np.ndarray, levels: int, channels: int) -> Callable:
    """基于粗查表格点的多线性插值连续正向估计器。

    LUT 格点按 meshgrid(indexing="ij") 行主序排列（与 _build_forward 的采样顺序一致）。
    预计算展平 LUT 与角点位掩码矩阵，避免热路径内重复分配。
    """
    flat_labs = np.ascontiguousarray(labs, dtype=np.float64).reshape(-1, 3)
    strides = np.array([levels ** (channels - 1 - k) for k in range(channels)], dtype=np.int64)
    corner_bits = [
        np.array([(mask >> k) & 1 for k in range(channels)], dtype=np.int64)
        for mask in range(1 << channels)
    ]
    corner_offsets = [int(bits @ strides) for bits in corner_bits]

    def interp(values: np.ndarray) -> np.ndarray:
        v = np.clip(np.asarray(values, dtype=np.float64), 0.0, 1.0)
        n = v.shape[0]
        g = v * (levels - 1)
        i0 = np.clip(np.floor(g).astype(np.int64), 0, levels - 2)
        w = g - i0  # [0,1]；v=1 时 w=1，全部权重落在上角点
        one_minus_w = 1.0 - w
        base_flat = i0 @ strides
        lab = np.zeros((n, 3), dtype=np.float64)
        for bits, off in zip(corner_bits, corner_offsets):
            prod = np.ones(n, dtype=np.float64)
            for k in range(channels):
                prod *= w[:, k] if bits[k] else one_minus_w[:, k]
            lab += prod[:, None] * flat_labs[base_flat + off]
        return lab

    return interp


def predict_recipe(
    icc_bytes: bytes,
    device_space: str,
    target_lab,
    tolerance: float = 2.0,
    method: str = "cie2000",
    max_rounds: int = 6,
    device_bias=None,
) -> dict:
    """给定目标 LAB，反查最优墨量配方。

    device_bias: 工艺级补偿偏置（0-1 单位，长度=通道数），作用于搜索种子并同步重算真实预测 Lab。

    返回:
        target_lab / predicted_lab / recipe(通道名→百分比) / delta_e /
        converged / iterations(ΔE 轨迹) / predictions(本次正向预测数) /
        lut_points(粗查表规模) / refine_predictions(插值精搜评估数)
    """
    channels = SPACE_CHANNELS.get(device_space, 3)
    forward = _build_forward(icc_bytes, channels)
    target = np.asarray(target_lab, dtype=np.float64)
    unit_off = _unit_offsets(channels)

    key = _sha(icc_bytes)
    lut_key = f"{key}:{LUT_LEVELS}:{channels}"
    with _cache_lock:
        cached_lut = _luts.get(lut_key)
        cached_interp = _interps.get(lut_key)
        if cached_lut is not None:
            _luts.move_to_end(lut_key)
        if cached_interp is not None:
            _interps.move_to_end(lut_key)

    if cached_lut is None:
        axes = np.linspace(0.0, 1.0, LUT_LEVELS)
        grids = np.meshgrid(*([axes] * channels), indexing="ij")
        pts = np.stack([g.ravel() for g in grids], axis=1)
        computed = (pts, forward(pts))
        with _cache_lock:
            _luts[lut_key] = computed
            _luts.move_to_end(lut_key)
            while len(_luts) > _CACHE_MAX:
                _luts.popitem(last=False)
        lut_pts, lut_labs = computed
    else:
        lut_pts, lut_labs = cached_lut

    if cached_interp is None:
        interp = _make_interp(lut_pts, lut_labs, LUT_LEVELS, channels)
        with _cache_lock:
            _interps[lut_key] = interp
            _interps.move_to_end(lut_key)
            while len(_interps) > _CACHE_MAX:
                _interps.popitem(last=False)
    else:
        interp = cached_interp

    bias = None
    if device_bias is not None:
        bias_arr = np.asarray(device_bias, dtype=np.float64)
        if bias_arr.shape == (channels,) and np.all(np.isfinite(bias_arr)):
            bias = np.clip(bias_arr, -0.15, 0.15)

    predictions = 0
    # Lab 最近邻粗排，取 3 个种子并行精搜
    dist = np.linalg.norm(lut_labs - target, axis=1)
    seed_idx = np.argsort(dist)[:3]

    best: dict | None = None
    for si in seed_idx:
        cur_val = lut_pts[si].copy()
        if bias is not None:
            shifted = np.clip(cur_val + bias, 0.0, 1.0)
            if not np.array_equal(shifted, cur_val):
                cur_val = shifted
                # 种子发生偏置位移后必须用真实 forward 同步重算 Lab，防止配方与 predicted_lab 脱节
                cur_lab = forward(cur_val.reshape(1, channels))[0]
                predictions += 1
            else:
                cur_lab = lut_labs[si].copy()
        else:
            cur_lab = lut_labs[si].copy()
        cur_de = float(delta_e(cur_lab, target, method))
        traj = [round(cur_de, 3)]
        step = 1.0 / (LUT_LEVELS - 1)

        for _ in range(max_rounds):
            cand = np.clip(cur_val + step * unit_off, 0.0, 1.0)
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

    # —— 插值精搜：连续插值目标引导更细步长的下降，候选经真实 forward 定夺 ——
    refine_preds = 0
    if REFINE_ENABLED and best is not None and best["delta_e"] > REFINE_FLOOR:
        cur_val = best["value"].copy()
        cur_de_i = float(delta_e(interp(cur_val[None, :])[0], target, method))
        bt_val, bt_lab, bt_de = best["value"].copy(), best["lab"].copy(), best["delta_e"]
        step = (1.0 / (LUT_LEVELS - 1)) / 4.0  # 1/64 ≈ 1.56%
        for _ in range(max_rounds * 2):
            cand = np.clip(cur_val + step * unit_off, 0.0, 1.0)
            des_i = delta_e_batch(interp(cand), target, method)
            refine_preds += len(cand)
            # 插值 top-2 去重后单次批量真实评估
            top_idx = np.argsort(des_i)[:2]
            top_vals = [cand[int(top_idx[0])]]
            if len(top_idx) > 1 and not np.array_equal(cand[int(top_idx[1])], top_vals[0]):
                top_vals.append(cand[int(top_idx[1])])
            top_batch = np.asarray(top_vals, dtype=np.float64)
            top_labs = forward(top_batch)
            predictions += len(top_batch)
            top_des = delta_e_batch(top_labs, target, method)
            best_t = int(np.argmin(top_des))
            if float(top_des[best_t]) + 1e-9 < bt_de:
                bt_val = top_batch[best_t].copy()
                bt_lab = top_labs[best_t]
                bt_de = float(top_des[best_t])

            min_i = int(np.argmin(des_i))
            if float(des_i[min_i]) + 1e-12 < cur_de_i:
                cur_de_i = float(des_i[min_i])
                cur_val = cand[min_i].copy()
            else:
                step /= 2.0
                if step < REFINE_STEP_MIN:
                    break
            if bt_de <= REFINE_FLOOR:
                break
        if bt_de + 1e-9 < best["delta_e"]:
            best["value"], best["lab"], best["delta_e"] = bt_val, bt_lab, bt_de
            best["trajectory"].append(round(bt_de, 3))

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
        "refine_predictions": refine_preds,
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
    xyz_d50 = np.asarray(colour.Lab_to_XYZ(np.asarray(lab, dtype=np.float64), illuminant=_D50_ILLUMINANT))
    xyz_d65 = _BRADFORD_D50_TO_D65 @ xyz_d50
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

    def _build_doc(self, process_doc: dict, result: dict, label: str = "") -> dict:
        rid = uuid.uuid4().hex[:12]
        return {
            "id": rid,
            "label": label,
            "process_id": process_doc["id"],
            "process_name": process_doc["name"],
            "device_space": process_doc["icc_meta"]["device_space"],
            "target_lab": result["target_lab"],
            "recipe": result["recipe"],
            "recipe_kind": result.get("recipe_kind", "device_values"),
            "recipe_meta": result.get("recipe_meta") or {
                "unit": "percent",
                "basis": "device_channels",
                "note": "分色值（设备值），不是基础油墨调墨配方",
            },
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

    def create(self, process_doc: dict, result: dict, label: str = "") -> dict:
        doc = self._build_doc(process_doc, result, label)
        self._store.put(doc["id"], doc)
        return doc

    def create_many(self, process_doc: dict, items: list[tuple[dict, str]]) -> list[dict]:
        """批量创建配方记录（单次原子落盘，消除批量预测 N 次写盘放大）。"""
        docs = [self._build_doc(process_doc, result, label) for result, label in items]
        self._store.put_many({doc["id"]: doc for doc in docs})
        return docs

    def get(self, rid: str) -> dict | None:
        doc = self._store.get(rid)
        return self._with_defaults(doc) if doc else None

    def list(self, process_id: str | None = None) -> list[dict]:
        docs = [self._with_defaults(d) for d in self._store.all().values()]
        if process_id:
            docs = [d for d in docs if d.get("process_id") == process_id]
        docs.sort(key=lambda d: d.get("created_at", ""), reverse=True)
        return docs

    @staticmethod
    def _with_defaults(doc: dict) -> dict:
        """旧配方读取兼容：V0.10 语义字段按设备分色值补齐。"""
        out = {**doc}
        out.setdefault("recipe_kind", "device_values")
        out.setdefault(
            "recipe_meta",
            {
                "unit": "percent",
                "basis": "device_channels",
                "note": "分色值（设备值），不是基础油墨调墨配方",
            },
        )
        return out

    def delete(self, rid: str) -> bool:
        return self._store.delete(rid)

    def remove_process(self, process_id: str) -> int:
        """级联清理：删除某工艺的全部配方记录（单次原子落盘）。"""
        keys = [
            rid for rid, doc in self._store.all().items()
            if isinstance(doc, dict) and doc.get("process_id") == process_id
        ]
        return self._store.delete_many(keys)

    def append_verification(self, rid: str, verification: dict) -> dict | None:
        def _append(data: dict):
            doc = data.get(rid)
            if not doc:
                return None
            doc = self._with_defaults(doc)
            doc["verifications"] = list(doc.get("verifications") or []) + [verification]
            data[rid] = doc
            return dict(doc)

        return self._store.update(_append)
