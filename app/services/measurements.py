"""测量会话存储与导入编排（V0.5 可信测量闭环 · M1）。

会话登记（仪器/条件/校准时间/背衬/复测模式）→ 导入解析 → 会话级重分级。
数据落盘 ``data/measurements.json``（复用 JsonStore 原子写）。

分级数据流向（方法闭环出口）：
- verified → QC 指标（M2）、ΔE 分量（M4）、补偿模型沉淀（M4 门禁）
- warning  → 仅展示与排查，不进指标与补偿
- rejected → 可查不可用
"""

from __future__ import annotations

import uuid
from datetime import datetime

from .cgats import CgatsParseError, parse_measurement_text
from .filedb import JsonStore
from .measurement_qa import grade_session
from .processes import DATA_DIR

#: 支持的测量条件（ISO 13655 观测条件类别）
MEASUREMENT_CONDITIONS = ("M0", "M1", "M2")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class MeasurementStore:
    """测量会话与记录存储（data/measurements.json）。"""

    def __init__(self, path=None) -> None:
        self._store = JsonStore(path or DATA_DIR / "measurements.json")

    # -- 会话 -----------------------------------------------------------

    def create_session(
        self,
        instrument: str,
        condition: str = "M1",
        calibrated_at: str = "",
        backing: str = "",
        repeat_mode: str = "single",
        process_id: str = "",
        notes: str = "",
    ) -> dict:
        sid = _new_id("sess")
        doc = {
            "id": sid,
            "instrument": instrument.strip() or "未登记仪器",
            "condition": condition if condition in MEASUREMENT_CONDITIONS else "M1",
            "calibrated_at": calibrated_at.strip(),
            "backing": backing.strip(),
            "repeat_mode": repeat_mode if repeat_mode in ("single", "repeat") else "single",
            "process_id": process_id,
            "notes": notes,
            "created_at": _now(),
            "stats": {"verified": 0, "warning": 0, "rejected": 0},
        }
        self._store.put(sid, doc)
        return doc

    def get_session(self, sid: str) -> dict | None:
        return self._store.get(sid)

    def update_session(self, sid: str, **fields) -> dict | None:
        """更新会话登记字段（repeat_mode 等）；改后由调用方重分级。"""
        def _upd(data: dict):
            session = data.get(sid)
            if session is None:
                return None
            for key, value in fields.items():
                if key == "repeat_mode" and value in ("single", "repeat"):
                    session[key] = value
            return dict(session)

        return self._store.update(_upd)

    def list_sessions(self) -> list[dict]:
        sessions = list(self._store.all().values())
        sessions.sort(key=lambda s: s.get("created_at", ""), reverse=True)
        return sessions

    def delete_session(self, sid: str) -> bool:
        return self._store.delete(sid)

    # -- 记录 -----------------------------------------------------------

    def records(self, sid: str) -> list[dict]:
        session = self.get_session(sid)
        return list(session.get("records", {}).values()) if session else []

    def all_records(self) -> list[dict]:
        out: list[dict] = []
        for s in self._store.all().values():
            out.extend(s.get("records", {}).values())
        return out

    def add_record(self, sid: str, filename: str, raw: bytes, process_doc: dict | None = None) -> dict:
        """解析并导入一个测量文件，随后对会话整体重分级。

        parse 失败抛 :class:`CgatsParseError`；返回新记录 doc。
        """
        if self.get_session(sid) is None:
            raise KeyError(sid)
        parsed = parse_measurement_text(raw)
        rid = _new_id("ms")
        record = {
            "id": rid,
            "filename": filename or "未命名.txt",
            "kind": parsed.kind,
            "imported_at": _now(),
            "source_condition": parsed.source_condition,
            "meta": {k: v for k, v in parsed.meta.items() if not k.startswith("_")},
            "encoding": parsed.meta.get("_encoding", ""),
            "notes": parsed.notes,
            "patch_count": len(parsed.patches),
            "reliability": "verified",
            "density_ok": True,
            "reasons": [],
            "patches": [],
            "labels": [],
        }
        # 序列化补丁（spectral 键转字符串以保 JSON 兼容）
        for pt in parsed.patches:
            record["patches"].append({
                "label": pt.label,
                "lab": pt.lab,
                "xyz": pt.xyz,
                "rgb": pt.rgb,
                "spectral": ({str(k): v for k, v in pt.spectral.items()} if pt.spectral else None),
                "densities": pt.densities,
                "lab_integrated": pt.lab_integrated,
            })
        record["labels"] = [pt.label for pt in parsed.patches]

        def _append(data: dict) -> dict:
            session = data.get(sid)
            if session is None:
                raise KeyError(sid)
            session.setdefault("records", {})[rid] = record
            self._regrade(session, process_doc)
            return dict(session["records"][rid])

        return self._store.update(_append)

    def delete_record(self, sid: str, rid: str, process_doc: dict | None = None) -> bool:
        def _remove(data: dict) -> bool:
            session = data.get(sid)
            if session is None or rid not in session.get("records", {}):
                return False
            del session["records"][rid]
            self._regrade(session, process_doc)
            return True

        return self._store.update(_remove)

    # -- 内部 -----------------------------------------------------------

    def _regrade(self, session: dict, process_doc: dict | None) -> None:
        """会话整体重分级并刷新统计（复测一致性/离群是会话级属性）。"""
        records = list(session.get("records", {}).values())
        paper_white = None
        if process_doc is not None:
            paper_white = _icc_paper_white(process_doc)
        grade_session(session, records, paper_white)
        stats = {"verified": 0, "warning": 0, "rejected": 0}
        for rec in records:
            stats[rec["reliability"]] = stats.get(rec["reliability"], 0) + 1
        session["stats"] = stats
        # 记录 dict 直接嵌在 session 文档里持久化
        session["records"] = {r["id"]: r for r in records}


def _icc_paper_white(process_doc: dict) -> list[float] | None:
    """工艺 ICC 纸白 Lab（CMYK 全零正查）；非 CMYK 或失败返回 None。"""
    meta = (process_doc or {}).get("icc_meta") or {}
    if meta.get("device_space") != "CMYK":
        return None
    try:
        import numpy as np

        from .recipe import _build_forward

        channels = int(meta.get("channels", 4))
        forward = _build_forward(process_doc_icc_bytes(process_doc), channels)
        return [float(v) for v in forward(np.zeros((1, channels), dtype=np.float64))[0]]
    except Exception:
        return None


def process_doc_icc_bytes(process_doc: dict) -> bytes:
    """读取工艺 ICC 字节（延迟导入避免与 processes 循环依赖）。"""
    from pathlib import Path

    from .processes import PROFILE_DIR

    icc_name = Path(str(process_doc.get("icc_file", "") or "")).name
    if not icc_name:
        raise FileNotFoundError("工艺缺少 ICC 文件名")
    path = Path(PROFILE_DIR) / icc_name
    return path.read_bytes()
