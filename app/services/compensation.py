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
        delta = np.clip(np.asarray(delta_device, dtype=np.float64), -cap, cap)
        doc = self._store.get(pid)
        if not doc or doc.get("icc_sha256") != icc_sha256:
            doc = {"icc_sha256": icc_sha256, "n": 0, "bias_device": [0.0] * len(delta)}
        bias = (1.0 - alpha) * np.asarray(doc["bias_device"], dtype=np.float64) + alpha * delta
        bias = np.clip(bias, -cap, cap)
        doc["n"] = int(doc["n"]) + 1
        doc["bias_device"] = [round(float(v), 4) for v in bias]
        doc.setdefault("history", []).append(
            {
                "ts": datetime.now().isoformat(timespec="seconds"),
                "delta_device": [round(float(v), 4) for v in delta],
            }
        )
        if len(doc["history"]) > HISTORY_MAX:
            doc["history"] = doc["history"][-HISTORY_MAX:]
        doc["updated_at"] = datetime.now().isoformat(timespec="seconds")
        self._store.put(pid, doc)
        return doc

    def bias_for_predict(self, pid: str, icc_sha256: str) -> list[float] | None:
        """返回可应用的偏置（sha 匹配且样本数达标），否则 None。"""
        doc = self._store.get(pid)
        if not doc or doc.get("icc_sha256") != icc_sha256:
            return None
        if int(doc.get("n", 0)) < MIN_N:
            return None
        return doc.get("bias_device")
