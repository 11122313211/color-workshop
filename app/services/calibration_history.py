"""校准轮次存储与收敛判定（V0.8）。

把 V0.7 的单轮校准升级为带记忆的迭代流程：
- apply 时把报告快照 + 应用偏置落库为「轮次」（幂等：同一 session 更新不新增）；
- 新报告自动与上一轮对比（compare_rounds）→ 改善/持平/恶化；
- 按工艺聚合轮次趋势（convergence_for_process）→ converged / improving / worsening / flat。

ICC 失效语义：轮次是历史事实，保留不删；但收敛统计只计入 icc_sha256 与
当前工艺一致的轮次（换 ICC 后墨量行为可能完全不同，跨 profile 谈收敛无意义）。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from .filedb import JsonStore
from .processes import DATA_DIR

#: 轮次对比的噪声地板：max ΔE 变化不超过该值视为持平（与 8-bit Lab 量化 ±0.4 同量级）
VERDICT_EPS = 0.05
#: overall 等级序（越高越好）
_OVERALL_RANK = {"needed": 0, "suggest": 1, "ok": 2, "n/a": -1}
VERDICT_LABELS = {"improved": "改善", "flat": "持平", "worsened": "恶化"}
CONVERGENCE_LABELS = {
    "converged": "已收敛",
    "improving": "持续改善",
    "worsening": "持续恶化",
    "flat": "徘徊",
    "n/a": "轮次不足",
}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class CalibrationStore:
    """校准轮次存储（data/calibrations.json）。"""

    def __init__(self) -> None:
        self._store = JsonStore(DATA_DIR / "calibrations.json")

    def save_round(self, process_id: str, icc_sha256: str, session_id: str, report: dict,
                   bias_before: list[float], bias_after: list[float]) -> dict:
        """落库一轮校准（幂等：同 session 已有轮次则更新）。

        seq 为进程内单调递增序号——同秒落多轮时保证排序确定（created_at 只有秒精度）。
        """
        existing = self.get_by_session(session_id)
        rid = existing["id"] if existing else f"cal_{uuid.uuid4().hex[:12]}"
        seq = existing["seq"] if existing else max(
            (int(d.get("seq", 0)) for d in self._store.all().values()), default=0) + 1
        doc = {
            "id": rid,
            "seq": seq,
            "process_id": process_id,
            "icc_sha256": icc_sha256,
            "session_id": session_id,
            "created_at": existing["created_at"] if existing else _now(),
            "overall": report.get("overall", "n/a"),
            "summary": report.get("summary", {}),
            "ink_suggestions": report.get("ink_suggestions", []),
            "bias_before": [round(float(v), 4) for v in bias_before],
            "bias_after": [round(float(v), 4) for v in bias_after],
        }
        self._store.put(rid, doc)
        return doc

    def get_by_session(self, session_id: str) -> dict | None:
        for doc in self._store.all().values():
            if doc.get("session_id") == session_id:
                return doc
        return None

    def latest_for_process(self, process_id: str, icc_sha256: str | None = None,
                           exclude_session: str | None = None) -> dict | None:
        """该工艺最近一轮（可选按 icc_sha256 过滤；exclude_session 排除当前会话自己的轮次）。"""
        rounds = [r for r in self.list_for_process(process_id, icc_sha256)
                  if r.get("session_id") != exclude_session]
        return rounds[0] if rounds else None

    def list_for_process(self, process_id: str, icc_sha256: str | None = None) -> list[dict]:
        docs = [d for d in self._store.all().values() if d.get("process_id") == process_id]
        if icc_sha256 is not None:
            docs = [d for d in docs if d.get("icc_sha256") == icc_sha256]
        docs.sort(key=lambda d: (d.get("created_at", ""), int(d.get("seq", 0))), reverse=True)
        return docs

    def remove_process(self, process_id: str) -> int:
        """级联清理：删除某工艺的全部轮次（工艺删除时调用）。"""
        removed = 0
        for rid, doc in list(self._store.all().items()):
            if doc.get("process_id") == process_id:
                self._store.delete(rid)
                removed += 1
        return removed


# ---------------------------------------------------------------------------
# 判定：轮次对比 / 收敛
# ---------------------------------------------------------------------------

def compare_rounds(current_report: dict, prev_round: dict) -> dict:
    """当前报告 vs 上一轮：等级序 + max ΔE 变化（噪声地板 VERDICT_EPS）。"""
    cur_overall = current_report.get("overall", "n/a")
    cur_summary = current_report.get("summary") or {}
    cur_max = cur_summary.get("max_de_expected")
    prev_overall = prev_round.get("overall", "n/a")
    prev_summary = prev_round.get("summary") or {}
    prev_max = prev_summary.get("max_de_expected")
    prev_avg = prev_summary.get("avg_de_expected")

    rank_up = _OVERALL_RANK.get(cur_overall, -1) > _OVERALL_RANK.get(prev_overall, -1)
    rank_down = _OVERALL_RANK.get(cur_overall, -1) < _OVERALL_RANK.get(prev_overall, -1)
    d_max = (round(cur_max - prev_max, 2)
             if cur_max is not None and prev_max is not None else None)
    d_avg = (round((cur_summary.get("avg_de_expected") or 0) - (prev_avg or 0), 2)
             if cur_summary.get("avg_de_expected") is not None and prev_avg is not None else None)

    verdict = "flat"
    if rank_up or (d_max is not None and d_max < -VERDICT_EPS):
        verdict = "improved"
    elif rank_down or (d_max is not None and d_max > VERDICT_EPS):
        verdict = "worsened"

    cur_delta = {s.get("channel"): s.get("delta_pct")
                 for s in (current_report.get("ink_suggestions") or [])}
    per_channel = []
    for s in prev_round.get("ink_suggestions") or []:
        ch = s.get("channel")
        pd, cd = s.get("delta_pct"), cur_delta.get(ch)
        per_channel.append({
            "channel": ch,
            "prev_delta": pd,
            "cur_delta": cd,
            "d": (round(cd - pd, 1) if pd is not None and cd is not None else None),
        })

    return {
        "prev_round_id": prev_round["id"],
        "prev_created_at": prev_round.get("created_at", ""),
        "prev_overall": prev_overall,
        "prev_max_de_expected": prev_max,
        "prev_avg_de_expected": prev_avg,
        "verdict": verdict,
        "verdict_label": VERDICT_LABELS[verdict],
        "d_max_de_expected": d_max,
        "d_avg_de_expected": d_avg,
        "per_channel": per_channel,
    }


def _round_order(r: dict) -> tuple:
    """轮次时间序：created_at 只有秒精度，同秒必须用 seq 决定先后。"""
    return (r.get("created_at", ""), int(r.get("seq", 0) or 0))


def convergence_for_process(rounds: list[dict]) -> dict:
    """按轮次序列判定收敛（调用方负责按当前 icc_sha256 过滤）。rounds 任意顺序。"""
    if len(rounds) < 2:
        return {"status": "n/a", "status_label": CONVERGENCE_LABELS["n/a"], "rounds": len(rounds)}
    ordered = sorted(rounds, key=_round_order)
    ok_rounds = [r for r in ordered if r.get("overall") == "ok"]
    if ok_rounds:
        return {
            "status": "converged",
            "status_label": CONVERGENCE_LABELS["converged"],
            "rounds": len(ordered),
            "converged_at": ok_rounds[0].get("created_at", ""),
        }
    last, prev = ordered[-1], ordered[-2]
    last_max = (last.get("summary") or {}).get("max_de_expected")
    prev_max = (prev.get("summary") or {}).get("max_de_expected")
    if last_max is None or prev_max is None:
        status = "flat"
    elif last_max < prev_max - VERDICT_EPS:
        status = "improving"
    elif last_max > prev_max + VERDICT_EPS:
        status = "worsening"
    else:
        status = "flat"
    return {
        "status": status,
        "status_label": CONVERGENCE_LABELS[status],
        "rounds": len(ordered),
        "trend": [round(float((r.get("summary") or {}).get("max_de_expected") or 0), 2)
                  for r in ordered],
    }


def round_no_for(store: CalibrationStore, process_id: str, session_id: str,
                 icc_sha256: str) -> int:
    """当前会话对应的轮次号（1-based 按时间升序；未落库时=已有轮次+1）。"""
    rounds = list(reversed(store.list_for_process(process_id, icc_sha256)))  # 时间升序
    for i, r in enumerate(rounds, 1):
        if r.get("session_id") == session_id:
            return i
    return len(rounds) + 1


def bias_snapshot(bias) -> list[float]:
    return [round(float(v), 4) for v in (bias or [])]


def trend_series(rounds: list[dict]) -> list[float]:
    """max ΔE 时间序列（收敛 sparkline 用）。"""
    ordered = sorted(rounds, key=_round_order)
    return [round(float((r.get("summary") or {}).get("max_de_expected") or 0), 2)
            for r in ordered]
