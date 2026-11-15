"""极简 JSON 落盘存储：data/ 目录下的注册表持久化。

MVP 阶段替代内存存储，保证工艺库、配方记录等服务重启后不丢数据；
后续可整体替换为 PostgreSQL，接口语义与 CRUD 对齐。
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any


class JsonStore:
    """单文件 JSON 键值存储（原子写入，线程安全，每次读取自动重载）。

    读取时重新从磁盘加载，保证多个实例 / 多进程下看到一致的最新数据；
    写入为临时文件 + 原子替换。
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()

    def _load(self) -> dict[str, Any]:
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
            except (json.JSONDecodeError, OSError):
                pass
            # 文件损坏：先隔离保留现场，避免下次写入用空数据覆盖清库
            self._quarantine()
        return {}

    def _quarantine(self) -> None:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = self._path.with_name(f"{self._path.name}.corrupt-{stamp}")
        try:
            self._path.replace(backup)
        except OSError:
            pass

    def all(self) -> dict[str, Any]:
        with self._lock:
            return self._load()

    def get(self, key: str) -> Any | None:
        with self._lock:
            return self._load().get(key)

    def put(self, key: str, value: Any) -> None:
        with self._lock:
            data = self._load()
            data[key] = value
            self._flush(data)

    def delete(self, key: str) -> bool:
        with self._lock:
            data = self._load()
            if key not in data:
                return False
            del data[key]
            self._flush(data)
            return True

    def _flush(self, data: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self._path)
