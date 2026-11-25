"""工艺级误差补偿模型：闭环从「单次补偿」沉淀为「累积学习」。

数据模型（data/compensations.json，key=process_id）：
    { icc_sha256, n, bias_device: [C], history: [≤10 条], updated_at }

- 累积：实测验证且修正有效（improved）时，取 corrected_recipe − 原recipe（/100 归一）
  按指数滑动平均累积，逐通道截幅 ±0.15。
- 应用：predict 时以 device_bias 作用于搜索种子（引擎内再 clip 一次）。
- 失效：工艺 ICC 替换后 sha 不匹配即自动重置（新 ICC 的墨量行为可能完全不同）。
"""

from __future__ import annotations

from datetime import datetime

import numpy as np

from .filedb import JsonStore
from .processes import DATA_DIR

# EMA 学习率与单通道偏置上限（0-1 墨量单位；±0.15 = ±15%）
ALPHA = 0.35
BIAS_CAP = 0.15
# 应用门槛：至少积累的验证样本数
MIN_N = 2
HISTORY_MAX = 10


class CompensationStore:
    """工艺级补偿模型存储与更新（data/compensations.json）。"""

    def __init__(self) -> None:
        self._store = JsonStore(DATA_DIR / "compensations.json")

    def get(self, pid: str) -> dict | None:
        return self._store.get(pid)

    def reset(self, pid: str) -> bool:
        return self._store.delete(pid)

    def accumulate(
        self,
        pid: str,
        icc_sha256: str,
        delta_device,
        alpha: float = ALPHA,
        cap: float = BIAS_CAP,
    ) -> dict:
        """累积一次有效修正的设备空间偏置。delta_device 为 0-1 单位、长度=通道数。"""
        raw_delta = np.asarray(delta_device, dtype=np.float64)
        if raw_delta.ndim != 1 or raw_delta.size == 0 or not np.all(np.isfinite(raw_delta)):
            raise ValueError("delta_device 必须为有限的一维数值序列")
        delta = np.clip(raw_delta, -cap, cap)

        def _mutate(doc):
            if (
                not doc
                or doc.get("icc_sha256") != icc_sha256
                or len(doc.get("bias_device") or []) != len(delta)
            ):
                doc = {"icc_sha256": icc_sha256, "n": 0, "bias_device": [0.0] * len(delta), "history": []}
            else:
                doc = dict(doc)
            bias = (1.0 - alpha) * np.asarray(doc["bias_device"], dtype=np.float64) + alpha * delta
            bias = np.clip(bias, -cap, cap)
            doc["n"] = int(doc["n"]) + 1
            doc["bias_device"] = [round(float(v), 4) for v in bias]
            hist = list(doc.get("history") or [])
            hist.append(
                {
                    "ts": datetime.now().isoformat(timespec="seconds"),
                    "delta_device": [round(float(v), 4) for v in delta],
                }
            )
            doc["history"] = hist[-HISTORY_MAX:]
            doc["updated_at"] = datetime.now().isoformat(timespec="seconds")
            return doc

        return self._store.mutate_key(pid, _mutate)

    def bias_for_predict(self, pid: str, icc_sha256: str) -> list[float] | None:
        """返回可应用的偏置（sha 匹配且样本数达标），否则 None。"""
        doc = self._store.get(pid)
        if not doc or doc.get("icc_sha256") != icc_sha256:
            return None
        if int(doc.get("n", 0)) < MIN_N:
            return None
        return doc.get("bias_device")

    def apply_calibration(
        self,
        pid: str,
        icc_sha256: str,
        delta_device,
        *,
        base_bias=None,
    ) -> dict:
        """校准建议写入（V0.7）：直接叠加（区别于 EMA——校准是用户显式确认的动作）。

        sha 不匹配时按既有语义重置（新 ICC 的墨量行为可能完全不同）；
        n 提升至 MIN_N（显式校准视为可信样本，越过应用门槛）。
        若提供 base_bias（同会话幂等重复应用时传入原轮次的 bias_before），
        则以 base_bias 为基准叠加，防止同会话重复点击导致偏置翻倍。
        """
        raw_delta = np.asarray(delta_device, dtype=np.float64)
        if raw_delta.ndim != 1 or raw_delta.size == 0 or not np.all(np.isfinite(raw_delta)):
            raise ValueError("delta_device 必须为有限的一维数值序列")
        delta = np.clip(raw_delta, -BIAS_CAP, BIAS_CAP)

        base_arr = None
        if base_bias is not None and len(base_bias) == len(delta):
            arr = np.asarray(base_bias, dtype=np.float64)
            if np.all(np.isfinite(arr)):
                base_arr = np.clip(arr, -BIAS_CAP, BIAS_CAP)

        def _mutate(doc):
            if (
                not doc
                or doc.get("icc_sha256") != icc_sha256
                or len(doc.get("bias_device") or []) != len(delta)
            ):
                doc = {"icc_sha256": icc_sha256, "n": 0, "bias_device": [0.0] * len(delta), "history": []}
            else:
                doc = dict(doc)
            cur = base_arr if base_arr is not None else np.asarray(doc["bias_device"], dtype=np.float64)
            bias = np.clip(cur + delta, -BIAS_CAP, BIAS_CAP)
            doc["n"] = max(int(doc.get("n", 0)), MIN_N)
            doc["bias_device"] = [round(float(v), 4) for v in bias]
            hist = list(doc.get("history") or [])
            hist.append(
                {
                    "ts": datetime.now().isoformat(timespec="seconds"),
                    "source": "calibration",
                    "delta_device": [round(float(v), 4) for v in delta],
                }
            )
            doc["history"] = hist[-HISTORY_MAX:]
            doc["updated_at"] = datetime.now().isoformat(timespec="seconds")
            return doc

        return self._store.mutate_key(pid, _mutate)
