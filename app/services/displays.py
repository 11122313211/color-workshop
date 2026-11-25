"""显示器档案与软打样门禁（V0.9 · P0-4）。

印前软打样（GB/T 41466-2022）要求显示器经过校准并在有效期内：
- **档案**：型号 / 目标白点（D50）/ 目标亮度（100–120 cd/m²）/ 对比度 / 色域覆盖；
- **校准确认**：校准器、软件、校准日期、有效期（月）、实测亮度 / 白点 / 对比度、
  显示器 ICC 名称与指纹；
- **门禁**：校准过期或未登记时，``/api/files/convert`` 的软打样预览附带
  ``softproof`` 状态，前端明确标注「预览仅参考」；
- **测量接收**：导入外部测量的 CGATS.17 / TSV（须含 XYZ 白/黑色块），推导白点 xy
  与对比度，原始文件留档并可原样导出复核（绝对亮度需外部给定，不伪造）。

诚实边界：本模块**不生成显示器 ICC**、不驱动校准硬件。测量与 profile 生成仍由
DisplayCAL / i1Profiler 等外部工具完成，这里只做档案、有效期、门禁与测量留痕。
"""

from __future__ import annotations

import calendar
import hashlib
import math
import uuid
from datetime import date, datetime
from pathlib import Path

from .cgats import CgatsParseError, parse_measurement_text
from .filedb import JsonStore
from .processes import DATA_DIR

#: 目标白点允许值
WHITE_POINTS = ("D50", "D55", "D65")

#: 校准有效期默认值（月）
DEFAULT_VALID_MONTHS = 6

#: 到期前提示阈值（天）
EXPIRING_DAYS = 30

STATUS_LABELS = {
    "ok": "校准有效",
    "expiring": "校准即将到期",
    "expired": "校准已过期",
    "uncalibrated": "未校准确认",
    "none": "未登记显示器",
    "missing": "显示器档案不存在",
}

#: 屏幕测量文件落盘目录（原始 CGATS/TSV，供原样导出）
MEASUREMENT_DIR = DATA_DIR / "display_measurements"


def _meta_float(meta: dict, keys: tuple[str, ...]) -> float | None:
    """从 CGATS 头读取绝对亮度等数值（取首个可解析 token）。"""
    for key in keys:
        raw = (meta or {}).get(key)
        if raw in (None, ""):
            continue
        try:
            return float(str(raw).split()[0])
        except (TypeError, ValueError):
            continue
    return None


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _new_id() -> str:
    return f"disp_{uuid.uuid4().hex[:12]}"


def _parse_date(value: str) -> date | None:
    text = (value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        return None


def _add_months(start: date, months: int) -> date:
    month = start.month - 1 + months
    year = start.year + month // 12
    month = month % 12 + 1
    day = min(start.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _norm_white_point(value: str, default: str = "D50") -> str:
    text = (value or default).strip().upper()
    if text not in WHITE_POINTS:
        raise ValueError(f"白点必须为 {'/'.join(WHITE_POINTS)}")
    return text


def _norm_coverage(raw: dict | None) -> dict[str, float]:
    out: dict[str, float] = {}
    for key, value in (raw or {}).items():
        try:
            num = float(value)
        except (TypeError, ValueError) as e:
            raise ValueError(f"色域覆盖 {key} 必须为数值") from e
        if not math.isfinite(num) or not 0 <= num <= 200:
            raise ValueError(f"色域覆盖 {key} 超出 0–200%")
        out[str(key)] = round(num, 1)
    return out


def validate_targets(raw: dict | None) -> dict:
    raw = raw or {}
    try:
        luminance = float(raw.get("luminance_cd_m2", 120.0))
    except (TypeError, ValueError) as e:
        raise ValueError("目标亮度必须为数值（cd/m²）") from e
    if not math.isfinite(luminance) or not 40 <= luminance <= 400:
        raise ValueError("目标亮度应在 40–400 cd/m²（印前软打样常用 100–120）")
    try:
        contrast = float(raw.get("contrast_ratio", 1000.0))
    except (TypeError, ValueError) as e:
        raise ValueError("对比度必须为数值") from e
    if not math.isfinite(contrast) or contrast < 100:
        raise ValueError("对比度应 ≥100:1")
    return {
        "white_point": _norm_white_point(raw.get("white_point", "D50")),
        "luminance_cd_m2": round(luminance, 1),
        "contrast_ratio": round(contrast, 1),
        "gamut_coverage": _norm_coverage(raw.get("gamut_coverage")),
    }


def validate_calibration(raw: dict | None) -> dict | None:
    if not raw:
        return None
    calibrated_at = _parse_date(raw.get("calibrated_at", ""))
    if calibrated_at is None:
        raise ValueError("校准确认需要有效日期（YYYY-MM-DD）")
    try:
        valid_months = int(raw.get("valid_months", DEFAULT_VALID_MONTHS))
    except (TypeError, ValueError) as e:
        raise ValueError("有效期必须为整数月") from e
    if not 1 <= valid_months <= 60:
        raise ValueError("校准有效期应在 1–60 个月")

    record: dict = {
        "calibrated_at": calibrated_at.isoformat(),
        "valid_months": valid_months,
        "calibrator": str(raw.get("calibrator") or "").strip(),
        "software": str(raw.get("software") or "").strip(),
        "white_point": _norm_white_point(raw.get("white_point", "D50")),
        "gamut_coverage": _norm_coverage(raw.get("gamut_coverage")),
        "profile_name": str(raw.get("profile_name") or "").strip(),
        "profile_sha256": str(raw.get("profile_sha256") or "").strip(),
        "notes": str(raw.get("notes") or "").strip(),
        "recorded_at": _now(),
    }
    for key in ("luminance_cd_m2", "black_luminance_cd_m2", "contrast_ratio"):
        value = raw.get(key)
        if value in (None, ""):
            record[key] = None
            continue
        try:
            num = float(value)
        except (TypeError, ValueError) as e:
            raise ValueError(f"{key} 必须为数值") from e
        if not math.isfinite(num):
            raise ValueError(f"{key} 必须为有限数值")
        record[key] = round(num, 2)
    if record.get("contrast_ratio") is not None and record["contrast_ratio"] < 0:
        raise ValueError("实测对比度不能为负")
    return record


def calibration_status(display: dict, today: date | None = None) -> dict:
    """当前校准状态：ok / expiring / expired / uncalibrated。"""
    today = today or date.today()
    cal = display.get("calibration") or {}
    calibrated_at = _parse_date(cal.get("calibrated_at", ""))
    base = {
        "display_id": display.get("id", ""),
        "name": display.get("name", ""),
        "calibrated": False,
        "calibrated_at": cal.get("calibrated_at"),
        "expires_at": None,
        "days_left": None,
        "calibrator": cal.get("calibrator", ""),
        "profile_name": cal.get("profile_name", ""),
        "targets": display.get("targets") or {},
        "measurements": {
            "luminance_cd_m2": cal.get("luminance_cd_m2"),
            "black_luminance_cd_m2": cal.get("black_luminance_cd_m2"),
            "contrast_ratio": cal.get("contrast_ratio"),
            "white_point": cal.get("white_point", ""),
            "gamut_coverage": cal.get("gamut_coverage") or {},
        },
    }
    if calibrated_at is None:
        return {
            **base,
            "status": "uncalibrated",
            "status_label": STATUS_LABELS["uncalibrated"],
            "message": "显示器未登记校准确认，软打样预览仅参考",
        }

    months = int(cal.get("valid_months") or DEFAULT_VALID_MONTHS)
    expires = _add_months(calibrated_at, months)
    days_left = (expires - today).days
    if days_left < 0:
        status = "expired"
        message = f"显示器校准已过期 {-days_left} 天，软打样预览仅参考"
    elif days_left <= EXPIRING_DAYS:
        status = "expiring"
        message = f"显示器校准将在 {days_left} 天后过期，建议重新校准"
    else:
        status = "ok"
        message = f"显示器校准有效（剩余 {days_left} 天）"
    return {
        **base,
        "status": status,
        "status_label": STATUS_LABELS[status],
        "expires_at": expires.isoformat(),
        "days_left": days_left,
        "calibrated": status in ("ok", "expiring"),
        "message": message,
    }


class DisplayStore:
    """显示器档案存储（data/displays.json）。"""

    def __init__(self, path=None) -> None:
        self._store = JsonStore(path or DATA_DIR / "displays.json")

    def create(self, payload: dict) -> dict:
        name = str(payload.get("name") or "").strip()
        if not name:
            raise ValueError("显示器名称不能为空")
        doc = {
            "id": _new_id(),
            "name": name,
            "model": str(payload.get("model") or "").strip(),
            "serial": str(payload.get("serial") or "").strip(),
            "notes": str(payload.get("notes") or "").strip(),
            "is_active": bool(payload.get("is_active", True)),
            "targets": validate_targets(payload.get("targets")),
            "calibration": validate_calibration(payload.get("calibration")),
            "history": [],
            "created_at": _now(),
            "updated_at": _now(),
        }
        if doc["calibration"]:
            doc["history"].append(doc["calibration"])

        def _insert(data: dict) -> dict:
            if doc["is_active"]:
                now = _now()
                for other in data.values():
                    if isinstance(other, dict) and other.get("is_active"):
                        other["is_active"] = False
                        other["updated_at"] = now
            data[doc["id"]] = doc
            return dict(doc)

        return self._store.update(_insert)

    def get(self, display_id: str) -> dict | None:
        return self._store.get(display_id)

    def list(self) -> list[dict]:
        docs = list(self._store.all().values())
        docs.sort(key=lambda d: (not d.get("is_active"), d.get("created_at", "")))
        return docs

    def update(self, display_id: str, payload: dict) -> dict:
        def _upd(data: dict) -> dict:
            doc = data.get(display_id)
            if doc is None:
                raise KeyError("显示器档案不存在")
            doc = dict(doc)
            for key in ("name", "model", "serial", "notes"):
                if key in payload and payload[key] is not None:
                    value = str(payload[key]).strip()
                    if key == "name" and not value:
                        raise ValueError("显示器名称不能为空")
                    doc[key] = value
            if payload.get("targets") is not None:
                merged = {**(doc.get("targets") or {}), **(payload.get("targets") or {})}
                doc["targets"] = validate_targets(merged)
            now = _now()
            if payload.get("is_active") is not None:
                doc["is_active"] = bool(payload["is_active"])
                if doc["is_active"]:
                    for oid, other in data.items():
                        if oid != display_id and isinstance(other, dict) and other.get("is_active"):
                            other["is_active"] = False
                            other["updated_at"] = now
            doc["updated_at"] = now
            data[display_id] = doc
            return dict(doc)

        return self._store.update(_upd)

    def activate(self, display_id: str) -> dict:
        def _act(data: dict) -> dict:
            doc = data.get(display_id)
            if doc is None:
                raise KeyError("显示器档案不存在")
            now = _now()
            for oid, other in data.items():
                if oid != display_id and isinstance(other, dict) and other.get("is_active"):
                    other["is_active"] = False
                    other["updated_at"] = now
            doc = dict(doc)
            doc["is_active"] = True
            doc["updated_at"] = now
            data[display_id] = doc
            return dict(doc)

        return self._store.update(_act)

    def add_calibration(self, display_id: str, raw: dict) -> dict:
        record = validate_calibration(raw)
        if record is None:
            raise ValueError("缺少校准确认数据")

        def _add(doc):
            if doc is None:
                raise KeyError("显示器档案不存在")
            doc = dict(doc)
            doc["calibration"] = record
            doc["history"] = list(doc.get("history") or []) + [record]
            doc["updated_at"] = _now()
            return doc

        return self._store.mutate_key(display_id, _add)

    def delete(self, display_id: str) -> bool:
        def _del(data: dict) -> tuple[bool, str]:
            doc = data.pop(display_id, None)
            if doc is None:
                return False, ""
            stored = Path(((doc.get("measurement") or {}).get("stored_file") or "")).name
            return True, stored

        deleted, stored = self._store.update(_del)
        if stored:
            (MEASUREMENT_DIR / stored).unlink(missing_ok=True)
        return deleted

    def attach_measurement(self, display_id: str, raw: bytes, filename: str = "",
                           white_luminance_cd_m2: float | None = None) -> dict:
        """导入显示器测量（CGATS.17 / TSV）：推导白点、黑位与对比度，并原样留存。

        要求测量文件含至少 2 个带 XYZ 的色块（白与黑）；光谱反射积分不适用于
        自发光屏幕，故仅接受 XYZ 字段。绝对亮度优先取请求参数，其次读 CGATS 头
        ``LUMINANCE_CD_M2`` 等键；都没有时留空，不猜测。
        """
        if self.get(display_id) is None:
            raise KeyError("显示器档案不存在")
        if not raw:
            raise ValueError("测量文件为空")
        if len(raw) > 5_000_000:
            raise ValueError("测量文件过大（>5MB）")
        if white_luminance_cd_m2 is not None:
            if not math.isfinite(float(white_luminance_cd_m2)) or float(white_luminance_cd_m2) < 0:
                raise ValueError("实测白点亮度必须为非负有限数值")
        try:
            parsed = parse_measurement_text(raw)
        except CgatsParseError as e:
            raise ValueError(f"无法解析测量文件：{e}") from e

        xyz_patches = [
            p for p in parsed.patches
            if p.xyz and len(p.xyz) == 3 and all(math.isfinite(float(v)) for v in p.xyz)
        ]
        if len(xyz_patches) < 2:
            raise ValueError("显示器测量需至少 2 个含 XYZ 的色块（白与黑）；光谱反射数据不适用于自发光屏幕")

        ordered = sorted(xyz_patches, key=lambda p: float(p.xyz[1]))
        black, white = ordered[0], ordered[-1]
        wy = float(white.xyz[1])
        by = float(black.xyz[1])
        total = sum(float(v) for v in white.xyz)
        white_xy = (round(float(white.xyz[0]) / total, 4), round(wy / total, 4)) if total > 0 else None
        contrast = round(wy / by, 1) if by > 0 else None
        absolute = white_luminance_cd_m2
        if absolute is None:
            absolute = _meta_float(parsed.meta, ("LUMINANCE_CD_M2", "LUMINANCE_CDM2", "LUMINANCE"))
        if absolute is not None and not math.isfinite(float(absolute)):
            absolute = None

        MEASUREMENT_DIR.mkdir(parents=True, exist_ok=True)
        stored = f"{display_id}-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}.txt"
        (MEASUREMENT_DIR / stored).write_bytes(raw)
        previous_holder = [""]

        def _attach(doc):
            if doc is None:
                raise KeyError("显示器档案不存在")
            doc = dict(doc)
            previous_holder[0] = Path(((doc.get("measurement") or {}).get("stored_file") or "")).name
            doc["measurement"] = {
                "filename": (filename or stored).strip(),
                "stored_file": stored,
                "sha256": hashlib.sha256(raw).hexdigest()[:16],
                "kind": parsed.kind,
                "patches": len(parsed.patches),
                "xyz_patches": len(xyz_patches),
                "source_condition": parsed.source_condition,
                "white_xy": list(white_xy) if white_xy else None,
                "white_y": round(wy, 3),
                "black_y": round(by, 4),
                "contrast_ratio": contrast,
                "luminance_cd_m2": round(float(absolute), 1) if absolute is not None else None,
                "imported_at": _now(),
            }
            calibration = dict(doc["calibration"]) if doc.get("calibration") else None
            if calibration:
                if calibration.get("contrast_ratio") is None and contrast is not None:
                    calibration["contrast_ratio"] = contrast
                if calibration.get("luminance_cd_m2") is None and absolute is not None:
                    calibration["luminance_cd_m2"] = round(float(absolute), 1)
                doc["calibration"] = calibration
            doc["updated_at"] = _now()
            return doc

        try:
            updated = self._store.mutate_key(display_id, _attach)
        except Exception:
            (MEASUREMENT_DIR / stored).unlink(missing_ok=True)
            raise
        if previous_holder[0] and previous_holder[0] != stored:
            (MEASUREMENT_DIR / previous_holder[0]).unlink(missing_ok=True)
        return updated

    def measurement_export(self, display_id: str) -> tuple[bytes, str, str]:
        """原样导出最近一次屏幕测量文件（供 DisplayCAL / i1Profiler 复核）。"""
        doc = self.get(display_id)
        if doc is None:
            raise KeyError("显示器档案不存在")
        measurement = doc.get("measurement") or {}
        stored = Path(measurement.get("stored_file") or "").name
        if not stored:
            raise KeyError("尚未导入屏幕测量文件")
        path = MEASUREMENT_DIR / stored
        if not path.exists():
            raise KeyError("屏幕测量文件已丢失，请重新导入")
        return path.read_bytes(), measurement.get("filename") or stored, measurement.get("sha256", "")

    def active(self) -> dict | None:
        for doc in self.list():
            if doc.get("is_active"):
                return doc
        return None

    def status(self, display_id: str) -> dict:
        doc = self.get(display_id)
        if doc is None:
            raise KeyError("显示器档案不存在")
        return calibration_status(doc)

    def active_status(self) -> dict:
        doc = self.active()
        if doc is None:
            return {
                "display_id": None,
                "name": "",
                "status": "none",
                "status_label": STATUS_LABELS["none"],
                "calibrated": False,
                "message": "未登记显示器档案，软打样预览仅参考",
            }
        return calibration_status(doc)

    def softproof_status(self, display_id: str = "") -> dict:
        """软打样门禁：指定档案优先，否则用当前激活显示器。"""
        if display_id:
            doc = self.get(display_id)
            if doc is None:
                return {
                    "display_id": display_id,
                    "name": "",
                    "status": "missing",
                    "status_label": STATUS_LABELS["missing"],
                    "calibrated": False,
                    "message": "指定显示器档案不存在，软打样预览仅参考",
                }
            return calibration_status(doc)
        return self.active_status()

    def report(self, display_id: str) -> dict:
        """校准档案报告（JSON 导出，供交付留痕）。"""
        doc = self.get(display_id)
        if doc is None:
            raise KeyError("显示器档案不存在")
        return {
            "generated_at": _now(),
            "display": doc,
            "status": calibration_status(doc),
            "notes": [
                "本报告记录显示器档案与校准确认，不包含显示器 ICC 本身。",
                "显示器 ICC 由 DisplayCAL / i1Profiler 等外部工具生成，系统仅登记名称与指纹。",
            ],
        }

    def _deactivate_others(self, keep_id: str) -> None:
        for doc in list(self._store.all().values()):
            if doc.get("id") == keep_id or not doc.get("is_active"):
                continue
            doc["is_active"] = False
            doc["updated_at"] = _now()
            self._store.put(doc["id"], doc)