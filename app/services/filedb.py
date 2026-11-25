"""极简 JSON 落盘存储：data/ 目录下的注册表持久化。

MVP 阶段替代内存存储，保证工艺库、配方记录等服务重启后不丢数据；
后续可整体替换为 PostgreSQL，接口语义与 CRUD 对齐。

并发与健壮性保障：
- 按规范化路径共享进程内 RLock，不同 JsonStore 实例指向同一文件时仍强互斥；
- 写入使用同目录唯一临时文件 + fsync + Windows 退避重试 os.replace，避免固定 .tmp 踩踏；
- 仅在 JSON 语法损坏或顶层非 dict 时隔离备份；瞬态 OSError 重试后抛出，绝不误清健康主库；
- 强制 allow_nan=False，杜绝 NaN/Infinity 毒丸数据落盘；
- 基于 (st_mtime_ns, st_size) 的进程内读缓存（返回深拷贝），消除高频只读接口重复反序列化；
- 提供 update / mutate_key / put_many / delete_many 原子事务与批量方法，消除 TOCTOU 与写放大。
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, TypeVar

T = TypeVar("T")

# 哨兵值：mutate_key 回调返回该值表示删除该键
DELETE_KEY = object()

# 按规范路径共享的进程内锁表与读缓存
_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: dict[Path, threading.RLock] = {}
_READ_CACHE: dict[Path, tuple[int, int, dict[str, Any]]] = {}

# 全局跨表维护锁：用于保护工艺创建/替换 ICC、永久删除与完整性修复之间的跨文件一致性
MAINTENANCE_LOCK = threading.RLock()

_WIN_RETRY_DELAYS = (0.005, 0.015, 0.04, 0.08)


def _lock_for(path: Path) -> threading.RLock:
    resolved = path.resolve()
    with _LOCKS_GUARD:
        lock = _PATH_LOCKS.get(resolved)
        if lock is None:
            lock = threading.RLock()
            _PATH_LOCKS[resolved] = lock
        return lock


def _read_text_with_retry(path: Path) -> str:
    last_err: OSError | None = None
    for idx, delay in enumerate((0.0, *_WIN_RETRY_DELAYS)):
        if delay:
            time.sleep(delay)
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise
        except OSError as exc:
            last_err = exc
            if idx == len(_WIN_RETRY_DELAYS):
                break
    assert last_err is not None
    raise last_err


def _replace_with_retry(src: Path, dst: Path) -> None:
    last_err: OSError | None = None
    for idx, delay in enumerate((0.0, *_WIN_RETRY_DELAYS)):
        if delay:
            time.sleep(delay)
        try:
            os.replace(src, dst)
            return
        except OSError as exc:
            last_err = exc
            if idx == len(_WIN_RETRY_DELAYS):
                break
    assert last_err is not None
    raise last_err


class JsonStore:
    """单文件 JSON 键值存储（原子写入，跨实例路径级线程安全，带 mtime 缓存）。"""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._resolved = self._path.resolve()
        self._lock = _lock_for(self._path)

    def _load_unlocked(self) -> dict[str, Any]:
        try:
            st = self._path.stat()
        except FileNotFoundError:
            _READ_CACHE.pop(self._resolved, None)
            return {}

        cached = _READ_CACHE.get(self._resolved)
        if cached is not None and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
            return copy.deepcopy(cached[2])

        try:
            raw = _read_text_with_retry(self._path)
        except FileNotFoundError:
            _READ_CACHE.pop(self._resolved, None)
            return {}

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            # 仅确定为 JSON 损坏时才隔离，避免将瞬态 OSError 误判为损坏
            self._quarantine_unlocked()
            _READ_CACHE.pop(self._resolved, None)
            return {}

        if not isinstance(data, dict):
            self._quarantine_unlocked()
            _READ_CACHE.pop(self._resolved, None)
            return {}

        try:
            st_after = self._path.stat()
            _READ_CACHE[self._resolved] = (
                st_after.st_mtime_ns,
                st_after.st_size,
                copy.deepcopy(data),
            )
        except OSError:
            _READ_CACHE.pop(self._resolved, None)
        return data

    def _quarantine_unlocked(self) -> None:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        backup = self._path.with_name(
            f"{self._path.name}.corrupt-{stamp}-{uuid.uuid4().hex[:6]}"
        )
        try:
            _replace_with_retry(self._path, backup)
        except OSError:
            pass

    def _flush_unlocked(self, data: dict[str, Any]) -> None:
        # 严格禁止 NaN/Infinity 落盘，防止永久破坏标准 JSON 序列化响应
        payload = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self._path.parent),
            prefix=f".{self._path.name}.",
            suffix=".tmp",
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                try:
                    os.fsync(fh.fileno())
                except OSError:
                    pass
            _replace_with_retry(tmp_path, self._path)
        except BaseException:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            _READ_CACHE.pop(self._resolved, None)
            raise

        try:
            st = self._path.stat()
            _READ_CACHE[self._resolved] = (
                st.st_mtime_ns,
                st.st_size,
                copy.deepcopy(data),
            )
        except OSError:
            _READ_CACHE.pop(self._resolved, None)

    def all(self) -> dict[str, Any]:
        with self._lock:
            return self._load_unlocked()

    def get(self, key: str) -> Any | None:
        with self._lock:
            return self._load_unlocked().get(key)

    def put(self, key: str, value: Any) -> None:
        with self._lock:
            data = self._load_unlocked()
            data[key] = value
            self._flush_unlocked(data)

    def put_many(self, items: dict[str, Any]) -> None:
        if not items:
            return
        with self._lock:
            data = self._load_unlocked()
            data.update(items)
            self._flush_unlocked(data)

    def delete(self, key: str) -> bool:
        with self._lock:
            data = self._load_unlocked()
            if key not in data:
                return False
            del data[key]
            self._flush_unlocked(data)
            return True

    def delete_many(self, keys: Iterable[str]) -> int:
        key_list = list(keys)
        if not key_list:
            return 0
        with self._lock:
            data = self._load_unlocked()
            removed = 0
            for key in key_list:
                if key in data:
                    del data[key]
                    removed += 1
            if removed:
                self._flush_unlocked(data)
            return removed

    def update(self, fn: Callable[[dict[str, Any]], T]) -> T:
        """在路径锁内原子执行全表读-改-写。"""
        with self._lock:
            data = self._load_unlocked()
            before = json.dumps(data, ensure_ascii=False, sort_keys=True)
            result = fn(data)
            after = json.dumps(data, ensure_ascii=False, sort_keys=True, allow_nan=False)
            if after != before:
                self._flush_unlocked(data)
            return result

    def mutate_key(self, key: str, fn: Callable[[Any | None], Any]) -> Any:
        """在路径锁内原子执行单键读-改-写（回调返回 DELETE_KEY 则删除该键）。"""
        with self._lock:
            data = self._load_unlocked()
            current = data.get(key)
            updated = fn(current)
            if updated is DELETE_KEY:
                if key in data:
                    del data[key]
                    self._flush_unlocked(data)
                return None
            data[key] = updated
            self._flush_unlocked(data)
            return copy.deepcopy(updated)
