"""签样工作流与放行门禁（V0.9 · P0-3）。

状态机
------
```
draft ──submit──▶ pending ──approve──▶ approved ──lock──▶ locked（终态）
                     │
                     ├──reject──▶ rejected ──submit──▶ pending
                     └──withdraw──▶ draft
```

- **draft**：草稿，可修改证据、可删除；
- **pending**：已提交确认，等待批准/驳回；
- **approved**：已批准，可作为放行依据；
- **rejected**：已驳回（必须填写理由），修改后可重新提交；
- **locked**：签样冻结，记录不可再变更。

放行门禁
--------
``gate(process)`` 仅在存在 approved/locked 签样、且签样快照中的 ICC 指纹与当前工艺
一致时放行。工艺替换 ICC 后旧签样自动失效（reason=``icc_changed``），必须重新签样。

数据落盘 ``data/signoffs.json``（复用 JsonStore 原子写）。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime

from .filedb import JsonStore
from .processes import DATA_DIR

#: 签样状态
SIGN_STATES = ("draft", "pending", "approved", "rejected", "locked")

#: 状态迁移表：当前状态 → {动作: 目标状态}
TRANSITIONS = {
    "draft": {"submit": "pending"},
    "pending": {"approve": "approved", "reject": "rejected", "withdraw": "draft"},
    "approved": {"lock": "locked"},
    "rejected": {"submit": "pending"},
    "locked": {},
}

#: 提交时认可的证据键（至少一项非空才允许提交）
EVIDENCE_KEYS = (
    "calibration_session_id",
    "measurement_session_id",
    "evaluation_id",
    "testform",
    "file_name",
)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _new_id() -> str:
    return f"sg_{uuid.uuid4().hex[:12]}"


def _clean_evidence(raw: dict | None) -> dict:
    """只保留已知证据键，去掉空值。"""
    evidence: dict = {}
    for key in EVIDENCE_KEYS:
        value = (raw or {}).get(key)
        if isinstance(value, dict):
            value = {k: v for k, v in value.items() if v not in (None, "", [], {})}
            if value:
                evidence[key] = value
        elif value not in (None, "", [], {}):
            evidence[key] = value
    return evidence


def _has_evidence(evidence: dict | None) -> bool:
    return any(bool(v) for v in (evidence or {}).values())


def _release_token(doc: dict) -> str:
    """冻结签样的放行令牌：绑定 id、ICC 指纹、证据与批准决定。"""
    payload = json.dumps({
        "id": doc.get("id"),
        "process_id": doc.get("process_id"),
        "icc_sha256": (doc.get("snapshot") or {}).get("icc_sha256"),
        "evidence": doc.get("evidence") or {},
        "decision": doc.get("decision") or {},
    }, ensure_ascii=False, sort_keys=True)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def summary(doc: dict) -> dict:
    return {
        "id": doc.get("id"),
        "title": doc.get("title", ""),
        "job_no": doc.get("job_no", ""),
        "process_id": doc.get("process_id", ""),
        "state": doc.get("state", "draft"),
        "created_by": doc.get("created_by", ""),
        "created_at": doc.get("created_at", ""),
        "updated_at": doc.get("updated_at", ""),
        "evidence": doc.get("evidence") or {},
        "snapshot": doc.get("snapshot") or {},
        "decision": doc.get("decision"),
        "locked_at": doc.get("locked_at"),
        "release_token": doc.get("release_token"),
        "history": doc.get("history") or [],
    }


class SignoffStore:
    """签样记录存储（data/signoffs.json）。"""

    def __init__(self, path=None) -> None:
        self._store = JsonStore(path or DATA_DIR / "signoffs.json")

    # -- 创建与查询 -------------------------------------------------------
    def create(self, process_doc: dict, title: str = "", job_no: str = "",
               created_by: str = "", evidence: dict | None = None) -> dict:
        now = _now()
        doc = {
            "id": _new_id(),
            "process_id": process_doc.get("id", ""),
            "title": (title or "").strip() or f"{process_doc.get('name', '未命名工艺')} 签样",
            "job_no": (job_no or "").strip(),
            "state": "draft",
            "created_by": (created_by or "").strip(),
            "created_at": now,
            "updated_at": now,
            "evidence": _clean_evidence(evidence),
            "snapshot": {
                "process_name": process_doc.get("name", ""),
                "icc_sha256": process_doc.get("icc_sha256", ""),
                "device_space": (process_doc.get("icc_meta") or {}).get("device_space", ""),
                "paper": process_doc.get("paper", ""),
                "ink": process_doc.get("ink", ""),
            },
            "decision": None,
            "history": [],
        }
        self._store.put(doc["id"], doc)
        return doc

    def get(self, sid: str) -> dict | None:
        return self._store.get(sid)

    def list(self, process_id: str = "", state: str = "") -> list[dict]:
        docs = list(self._store.all().values())
        if process_id:
            docs = [d for d in docs if d.get("process_id") == process_id]
        if state:
            docs = [d for d in docs if d.get("state") == state]
        docs.sort(key=lambda d: (d.get("updated_at", ""), d.get("created_at", "")), reverse=True)
        return docs

    # -- 状态迁移 ---------------------------------------------------------
    def transition(self, sid: str, action: str, actor: str = "", note: str = "") -> dict:
        doc = self.get(sid)
        if doc is None:
            raise KeyError("签样记录不存在")
        state = doc.get("state", "draft")
        target = TRANSITIONS.get(state, {}).get(action)
        if target is None:
            raise ValueError(f"当前状态 {state} 不支持操作 {action}")

        actor = (actor or "").strip()
        note = (note or "").strip()
        if not actor:
            raise ValueError("操作人不能为空")
        if action == "reject" and not note:
            raise ValueError("驳回必须填写理由")
        if action == "submit" and not _has_evidence(doc.get("evidence")):
            raise ValueError("提交前需关联至少一项依据（校准会话/测量会话/评价/样张/文件）")

        now = _now()
        if action in ("approve", "reject"):
            doc["decision"] = {
                "action": action,
                "by": actor,
                "at": now,
                "note": note,
            }
        if action == "lock":
            doc["locked_at"] = now
            doc["release_token"] = _release_token(doc)

        doc["history"] = (doc.get("history") or []) + [{
            "at": now,
            "action": action,
            "from": state,
            "to": target,
            "by": actor,
            "note": note,
        }]
        doc["state"] = target
        doc["updated_at"] = now
        self._store.put(sid, doc)
        return doc

    def delete(self, sid: str) -> bool:
        doc = self.get(sid)
        if doc is None:
            raise KeyError("签样记录不存在")
        if doc.get("state") != "draft":
            raise ValueError("仅草稿状态的签样可删除")
        return self._store.delete(sid)

    # -- 门禁 -----------------------------------------------------------
    def gate(self, process_doc: dict) -> dict:
        """工艺是否已具备放行条件（最新 approved/locked 签样 + ICC 指纹一致）。"""
        pid = process_doc.get("id", "")
        candidates = [
            d for d in self._store.all().values()
            if d.get("process_id") == pid and d.get("state") in ("approved", "locked")
        ]
        if not candidates:
            return {
                "allowed": False,
                "reason": "no_signoff",
                "message": "该工艺尚未有已批准的签样",
                "signoff_id": None,
            }
        candidates.sort(
            key=lambda d: (d.get("updated_at", ""), d.get("created_at", "")),
            reverse=True,
        )
        latest = candidates[0]
        current_sha = process_doc.get("icc_sha256", "")
        snapshot_sha = (latest.get("snapshot") or {}).get("icc_sha256", "")
        if not snapshot_sha:
            return {
                "allowed": False,
                "reason": "snapshot_missing",
                "message": "签样快照缺少 ICC 指纹，无法判定有效性",
                "signoff_id": latest["id"],
            }
        if current_sha and snapshot_sha != current_sha:
            return {
                "allowed": False,
                "reason": "icc_changed",
                "message": "工艺 ICC 已替换，原签样失效，需重新签样",
                "signoff_id": latest["id"],
                "snapshot_icc": snapshot_sha,
                "current_icc": current_sha,
            }
        return {
            "allowed": True,
            "reason": "approved",
            "message": "签样有效，可放行" if latest.get("state") == "locked" else "已批准，可放行",
            "signoff_id": latest["id"],
            "state": latest.get("state"),
            "approval": latest.get("decision"),
            "release_token": latest.get("release_token"),
        }

    def release_manifest(self, process_doc: dict) -> dict:
        """生成交接清单（放行门禁的第一个消费者，供 P2 交付包复用）。"""
        gate = self.gate(process_doc)
        if not gate["allowed"]:
            raise PermissionError(gate["message"])
        doc = self.get(gate["signoff_id"])
        return {
            "generated_at": _now(),
            "gate": gate,
            "process": {
                "id": process_doc.get("id", ""),
                "name": process_doc.get("name", ""),
                "icc_sha256": process_doc.get("icc_sha256", ""),
                "paper": process_doc.get("paper", ""),
                "ink": process_doc.get("ink", ""),
            },
            "signoff": summary(doc),
        }