"""主观评价检查单（V0.6 · M4，GB/T 34053 方法论）。

评价体系：标准观测环境（D65 光源、无杂光）下多名评价人**独立**按统一 5 级
尺度打分（1 非常差 … 5 非常好），统计各指标算术平均与标准差——标准差反映
评价人打分一致性，离散过大提示评价尺度失稳（需重新统一打分规则）。

三套模板（书刊 / 纸包装 / 塑料包装）按材质评价要点差异化：
- 书刊以阅读性为核心：文字清晰度、阶调层次、装订牢固
- 纸包装侧重成型加工：裁切、粘合、盒体规整
- 塑料包装侧重热封与附着：封边、拉伸变形、墨层附着

数据落盘 ``data/evaluations.json``（复用 JsonStore 原子写）。
"""

from __future__ import annotations

import math
import uuid
from datetime import datetime

from .filedb import JsonStore
from .processes import DATA_DIR

#: 5 级打分制说明（GB/T 34053 精神）
SCORE_SCALE = {"1": "非常差", "2": "较差", "3": "一般", "4": "较好", "5": "非常好"}

#: 打分一致性预警：单项指标标准差超过该值提示评价人分歧
SCORE_STD_WARN = 0.8

#: 检查单模板：维度 → 指标列表（通用行业指标组，非特定产品指标）
TEMPLATES: dict[str, dict] = {
    "book": {
        "name": "书刊",
        "groups": [
            {"group": "色彩", "items": ["颜色无明显偏色", "色彩还原准确", "色彩过渡自然"]},
            {"group": "阶调", "items": ["明暗部细节完整", "阶调过渡平顺"]},
            {"group": "清晰度", "items": ["文字清晰不发虚", "图案边缘顺滑", "网点还原干净"]},
            {"group": "墨色", "items": ["墨色均匀无深浅差异", "墨层洁净无脏点"]},
            {"group": "装订", "items": ["书页平整无皱褶", "装订牢固无散页"]},
        ],
    },
    "paper_pack": {
        "name": "纸包装",
        "groups": [
            {"group": "色彩", "items": ["颜色无明显偏色", "色彩鲜艳饱满", "色彩过渡自然"]},
            {"group": "阶调", "items": ["明暗部细节完整", "阶调过渡平顺"]},
            {"group": "清晰度", "items": ["图案边缘顺滑无毛刺", "小字细线清晰不发虚"]},
            {"group": "墨色", "items": ["墨色均匀无深浅差异", "墨层洁净无脏点晕染"]},
            {"group": "成型", "items": ["裁切整齐无毛边", "套印精准无错位", "粘合牢固无开胶", "盒体规整无歪斜"]},
        ],
    },
    "plastic_pack": {
        "name": "塑料包装",
        "groups": [
            {"group": "色彩", "items": ["颜色无明显偏色", "色彩鲜艳饱满", "色彩过渡自然"]},
            {"group": "阶调", "items": ["明暗部细节完整", "阶调过渡平顺"]},
            {"group": "清晰度", "items": ["图案边缘顺滑无毛刺", "小字细线清晰不发虚"]},
            {"group": "墨色", "items": ["墨色均匀无深浅差异", "墨层洁净无脏点晕染", "墨层附着无脱落"]},
            {"group": "成型", "items": ["封边整齐无锯齿漏封", "套印对齐无拉伸变形", "袋体牢固无脱层", "袋身平整无歪斜"]},
        ],
    },
}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def template_items(template: str) -> list[str]:
    """模板的扁平指标列表。"""
    doc = TEMPLATES.get(template)
    if doc is None:
        raise ValueError(f"未知评价模板: {template}")
    return [item for g in doc["groups"] for item in g["items"]]


def validate_scores(template: str, scores: dict) -> dict[str, int]:
    """校验打分：指标必须属于模板，分值必须为 1-5 整数。非法则抛 ValueError。"""
    items = template_items(template)
    if not scores:
        raise ValueError("打分不能为空")
    out: dict[str, int] = {}
    for key, val in scores.items():
        if key not in items:
            raise ValueError(f"指标不在模板中: {key}")
        if isinstance(val, bool):
            raise ValueError(f"分值必须为 1-5 整数: {key}")
        if isinstance(val, int):
            v = val
        elif isinstance(val, float):
            if not math.isfinite(val) or not val.is_integer():
                raise ValueError(f"分值必须为 1-5 整数: {key}")
            v = int(val)
        elif isinstance(val, str) and val.strip().lstrip("+-").isdigit():
            v = int(val.strip())
        else:
            raise ValueError(f"分值必须为 1-5 整数: {key}")
        if not 1 <= v <= 5:
            raise ValueError(f"分值超出 1-5: {key}={v}")
        out[key] = v
    return out


def evaluation_stats(evaluation: dict) -> dict:
    """统计：每指标 mean/std（样本标准差，≥2 条时）、综合分、一致性预警。"""
    template = evaluation.get("template", "book")
    try:
        items = template_items(template)
    except ValueError:
        items = []
    entries = evaluation.get("entries") or []
    rows = []
    warn_items = []
    raw_means = []
    for item in items:
        vals = [float(e["scores"][item]) for e in entries
                if item in (e.get("scores") or {})]
        n = len(vals)
        raw_mean = sum(vals) / n if n else None
        mean = round(raw_mean, 2) if raw_mean is not None else None
        std = round(math.sqrt(sum((v - raw_mean) ** 2 for v in vals) / (n - 1)), 3) if (n >= 2 and raw_mean is not None) else None
        if std is not None and std > SCORE_STD_WARN:
            warn_items.append(item)
        if raw_mean is not None:
            raw_means.append(raw_mean)
        rows.append({"item": item, "n": n, "mean": mean, "std": std})
    return {
        "rows": rows,
        "entries": len(entries),
        "overall": round(sum(raw_means) / len(raw_means), 2) if raw_means else None,
        "consistency_warn": warn_items,
    }


class EvaluationStore:
    """主观评价存储（data/evaluations.json）。"""

    def __init__(self, path=None) -> None:
        self._store = JsonStore(path or DATA_DIR / "evaluations.json")

    def create_evaluation(self, template: str, title: str = "",
                          process_id: str = "") -> dict:
        if template not in TEMPLATES:
            raise ValueError(f"未知评价模板: {template}")
        eid = _new_id("ev")
        doc = {
            "id": eid,
            "template": template,
            "template_name": TEMPLATES[template]["name"],
            "title": title.strip() or f"{TEMPLATES[template]['name']}主观评价",
            "process_id": process_id,
            "created_at": _now(),
            "entries": [],
        }
        self._store.put(eid, doc)
        return doc

    def get(self, eid: str) -> dict | None:
        return self._store.get(eid)

    def list_evaluations(self) -> list[dict]:
        docs = list(self._store.all().values())
        docs.sort(key=lambda d: d.get("created_at", ""), reverse=True)
        return docs

    def add_entry(self, eid: str, evaluator: str, scores: dict) -> dict:
        name = evaluator.strip()
        if not name:
            raise ValueError("评价人姓名不能为空")

        def _mutate(doc):
            if doc is None:
                raise KeyError("评价不存在")
            clean = validate_scores(doc.get("template", ""), scores)
            doc = dict(doc)
            entry = {
                "id": _new_id("ent"),
                "evaluator": name,
                "scores": {k: int(v) for k, v in clean.items()},
                "submitted_at": _now(),
            }
            doc["entries"] = list(doc.get("entries") or []) + [entry]
            return doc

        return self._store.mutate_key(eid, _mutate)

    def delete_evaluation(self, eid: str) -> bool:
        return self._store.delete(eid)
