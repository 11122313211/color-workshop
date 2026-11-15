"""工艺库：工艺实体（打印机/纸张/墨水组合）与其绑定的 ICC 特性文件。

工艺是一等实体——不同工艺的 ICC 文件不一样，配方预测、印前转换
均以工艺为上下文。ICC 文件上传后落盘 data/processes/，注册表存
data/processes.json（重启不丢）。
"""

from __future__ import annotations

import hashlib
import io
import os
import uuid
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import ImageCms

from .filedb import JsonStore

# 数据目录：可用环境变量 COLORFORGE_DATA_DIR 重定向（测试隔离用）
DATA_DIR = Path(
    os.environ.get(
        "COLORFORGE_DATA_DIR",
        str(Path(__file__).resolve().parents[2] / "data"),
    )
)
PROFILE_DIR = DATA_DIR / "processes"

# 设备类签名 -> 中文说明（ICC 头部 device class）
DEVICE_CLASS_NAMES = {
    "prtr": "输出设备（打印机/印刷机）",
    "mntr": "显示设备",
    "scnr": "输入设备（扫描/相机）",
    "spac": "色彩空间转换",
}

# 设备色彩空间 -> 通道数
SPACE_CHANNELS = {"CMYK": 4, "RGB": 3, "GRAY": 1, "Lab": 3, "XYZ": 3}


def detect_device_space(icc: bytes) -> str:
    """从 ICC 头部探测设备色彩空间。

    各厂商 profile 头部偏移存在差异（部分文件无 preferred CMM 字段），
    故不依赖固定偏移，而是定位设备类签名（prtr/mntr/...）后取其后 4 字节。
    """
    for cls in (b"prtr", b"mntr", b"scnr", b"spac", b"link", b"abst"):
        i = icc.find(cls, 0, 64)
        if i >= 0:
            sig = icc[i + 4 : i + 8].decode("ascii", "replace").strip()
            if sig in SPACE_CHANNELS:
                return sig
    return "RGB"


class ProcessStore:
    """工艺存储：ICC 文件 + 元数据注册表。"""

    def __init__(self) -> None:
        self._store = JsonStore(DATA_DIR / "processes.json")
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    # -- 元数据 ---------------------------------------------------------

    @staticmethod
    def parse_meta(icc_bytes: bytes) -> dict:
        """解析 ICC 元数据：描述、版权、设备类与色彩空间。"""
        prof = ImageCms.getOpenProfile(io.BytesIO(icc_bytes))
        space = detect_device_space(icc_bytes)
        return {
            "description": ImageCms.getProfileDescription(prof).strip(),
            "copyright": ImageCms.getProfileCopyright(prof).strip()[:200],
            "device_space": space,
            "channels": SPACE_CHANNELS.get(space, 3),
        }

    # -- CRUD -----------------------------------------------------------

    def create(
        self,
        name: str,
        icc_bytes: bytes,
        paper: str = "",
        ink: str = "",
        notes: str = "",
        filename: str = "",
    ) -> dict:
        pid = uuid.uuid4().hex[:12]
        icc_file = f"{pid}.icc"
        (PROFILE_DIR / icc_file).write_bytes(icc_bytes)
        doc = {
            "id": pid,
            "name": name,
            "paper": paper,
            "ink": ink,
            "notes": notes,
            "icc_file": icc_file,
            "icc_original_name": filename or icc_file,
            "icc_size": len(icc_bytes),
            "icc_sha256": hashlib.sha256(icc_bytes).hexdigest()[:16],
            "icc_meta": self.parse_meta(icc_bytes),
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        self._store.put(pid, doc)
        return doc

    def get(self, pid: str) -> dict | None:
        return self._store.get(pid)

    def list(self) -> list[dict]:
        docs = list(self._store.all().values())
        docs.sort(key=lambda d: d.get("created_at", ""), reverse=True)
        return docs

    def delete(self, pid: str) -> bool:
        doc = self._store.get(pid)
        if not doc:
            return False
        self._store.delete(pid)
        try:
            (PROFILE_DIR / doc["icc_file"]).unlink(missing_ok=True)
        except OSError:
            pass
        return True

    def icc_bytes(self, pid: str) -> bytes:
        doc = self.get(pid)
        if not doc:
            raise KeyError(pid)
        return (PROFILE_DIR / doc["icc_file"]).read_bytes()


# ---------------------------------------------------------------------------
# 工艺色域采样（供 CIE 色度图展示）
# ---------------------------------------------------------------------------

_gamut_cache: dict[str, dict] = {}

# sRGB 三原色 xy 色度坐标（D65），与前端色度图绘制的参考三角形一致
_SRGB_PRIMARIES_XY = ((0.6400, 0.3300), (0.3000, 0.6000), (0.1500, 0.0600))


def _convex_hull_indices(points) -> list[int]:
    """xy 平面凸包顶点在原数组的索引（Andrew 单调链，逆时针闭合）。

    仅依赖 numpy，不引入 scipy。共线点会被剔除；点数 < 3 时退化为全部索引。
    """
    pts = np.asarray(points, dtype=np.float64)
    n = len(pts)
    if n < 3:
        return list(range(n))
    idx = np.lexsort((pts[:, 1], pts[:, 0]))
    sx, sy = pts[idx, 0], pts[idx, 1]

    def half(seq_i):
        stack: list[int] = []
        for i in seq_i:
            while len(stack) >= 2:
                ox, oy = sx[stack[-2]], sy[stack[-2]]
                ax, ay = sx[stack[-1]], sy[stack[-1]]
                if (ax - ox) * (sy[i] - oy) - (ay - oy) * (sx[i] - ox) <= 0:
                    stack.pop()
                else:
                    break
            stack.append(i)
        return stack

    lower = half(range(n))
    upper = half(range(n - 1, -1, -1))
    hull = lower[:-1] + upper[:-1]
    return [int(idx[i]) for i in hull] if len(hull) >= 3 else []


def _point_in_srgb_triangle(xy) -> bool:
    """判断 xy 色度点是否位于 sRGB 原色三角形内（近似覆盖率用）。"""
    (x1, y1), (x2, y2), (x3, y3) = _SRGB_PRIMARIES_XY
    x, y = float(xy[0]), float(xy[1])
    denom = (y2 - y3) * (x1 - x3) + (x3 - x2) * (y1 - y3)
    if abs(denom) < 1e-12:
        return False
    a = ((y2 - y3) * (x - x3) + (x3 - x2) * (y - y3)) / denom
    b = ((y3 - y1) * (x - x3) + (x1 - x3) * (y - y3)) / denom
    c = 1.0 - a - b
    return a >= -1e-9 and b >= -1e-9 and c >= -1e-9


def sample_process_gamut(process: dict, levels: int = 6) -> dict:
    """经工艺 ICC 采样设备空间网格 -> Lab，返回色域数据。

    返回:
        xy_points: CIE 1931 xy 色度点集（PCS 为 D50 Lab，按 D50 白点转 XYZ）
        lab_points: 与 xy_points 等长对齐的 Lab 点集（前端 L* 着色 / 3D 投影用）
        hull: 凸包顶点在 xy_points/lab_points 中的索引（闭合顺序，按 Y 过滤暗点）
        coverage_srgb: 采样点落在 sRGB 三角形内的比例（近似覆盖率）
        volume: Lab 包围盒体积估计
        L_range/a_range/b_range: 各维范围
        sampled: 采样点数
    """
    # 缓存键必须含 levels：同工艺不同采样密度是不同结果
    key = f"{process['icc_sha256']}:{levels}"
    if key in _gamut_cache:
        return _gamut_cache[key]

    import colour
    from PIL import Image, ImageCms

    icc_bytes = (PROFILE_DIR / process["icc_file"]).read_bytes()
    channels = process["icc_meta"]["channels"]
    # Pillow 合法模式：GRAY 通道即 "L"（"GRAY" 不是合法图像模式）
    in_modes = {1: ["L", "GRAY"], 3: ["RGB"], 4: ["CMYK"]}.get(channels, ["CMYK"])

    prof_dev = ImageCms.getOpenProfile(io.BytesIO(icc_bytes))
    prof_lab = ImageCms.createProfile("LAB")
    xform = None
    last_err: Exception | None = None
    for in_mode in in_modes:
        try:
            xform = ImageCms.buildTransform(prof_dev, prof_lab, in_mode, "LAB")
            break
        except Exception as e:  # noqa: BLE001 —— 逐候选尝试签名
            last_err = e
    if xform is None:
        raise ValueError(f"无法构建色域采样变换: {last_err}")

    axes = np.linspace(0.0, 1.0, levels)
    grids = np.meshgrid(*([axes] * channels), indexing="ij")
    pts = np.stack([g.ravel() for g in grids], axis=1)
    arr = (pts * 255.0).round().astype("uint8")
    # L 模式需 2D 数组；RGB/CMYK 为 (n,1,C)
    shape = (-1, channels) if in_modes[0] == "L" else (-1, 1, channels)
    img = Image.fromarray(arr.reshape(shape), mode=in_modes[0])
    out = ImageCms.applyTransform(img, xform)
    # PIL LAB 模式存储：L 字节 = L*2.55；a/b 字节 = int8 补码（有符号 a*/b*）
    raw = np.asarray(out, dtype=np.uint8).reshape(-1, 3)
    lab = np.empty(raw.shape, dtype=np.float64)
    lab[:, 0] = raw[:, 0] / 2.55
    lab[:, 1:] = raw[:, 1:].astype(np.int8)

    # Lab(D50) -> XYZ(D50) -> xy 色度坐标
    d50 = colour.CCS_ILLUMINANTS["CIE 1931 2 Degree Standard Observer"]["D50"]
    xyz = colour.Lab_to_XYZ(lab, illuminant=d50)
    xy = colour.XYZ_to_xyY(xyz)[:, :2]
    xy = np.clip(xy, 0.0, 0.8)

    # 凸包仅统计有实际亮度的点（Y≈0 的暗色在 xy 图上聚在原点，会破坏凸包形状）
    hull_idx = _convex_hull_indices(xy[xyz[:, 1] > 1e-6])
    coverage = float(
        np.mean([_point_in_srgb_triangle(p) for p in xy])
    ) if len(xy) else 0.0

    result = {
        "xy_points": [[round(float(x), 4), round(float(y), 4)] for x, y in xy],
        "lab_points": [
            [round(float(l), 1), round(float(a), 1), round(float(b), 1)]
            for l, a, b in lab
        ],
        "hull": hull_idx,
        "coverage_srgb": round(coverage, 4),
        "volume": float(
            (lab[:, 0].max() - lab[:, 0].min())
            * (lab[:, 1].max() - lab[:, 1].min())
            * (lab[:, 2].max() - lab[:, 2].min())
        ),
        "L_range": [round(float(lab[:, 0].min()), 1), round(float(lab[:, 0].max()), 1)],
        "a_range": [round(float(lab[:, 1].min()), 1), round(float(lab[:, 1].max()), 1)],
        "b_range": [round(float(lab[:, 2].min()), 1), round(float(lab[:, 2].max()), 1)],
        "sampled": int(len(lab)),
    }
    _gamut_cache[key] = result
    return result
