"""Concurrent-safe, content-addressed artifact cache."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

try:
    import fcntl
except ImportError:  # pragma: no cover - the target environment is POSIX
    fcntl = None

from .canonical import canonical_bytes, sha256_obj
from .errors import (
    E_CACHE_CORRUPT,
    E_LOCK_TIMEOUT,
    AppError,
    resource_limit,
)


@dataclass(frozen=True)
class CacheResult:
    key: str
    key_components: dict[str, Any]
    kind: str
    content: dict[str, Any]
    content_sha256: str
    status: str
    write_owner: bool = False
    degradation: str | None = None


class CacheStore:
    def __init__(
        self,
        root: str | Path,
        max_entries: int = 10_000,
        resource_policy: str = "strict",
    ) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_entries = int(max_entries)
        self.resource_policy = resource_policy
        self._thread_lock = threading.Lock()

    def key_for(self, key_components: dict[str, Any]) -> str:
        return sha256_obj(key_components)

    def _path(self, key: str) -> Path:
        return self.root / key[:2] / f"{key}.json"

    def _lock_path(self, key: str) -> Path:
        return self.root / key[:2] / f"{key}.lock"

    def _entry_count(self) -> int:
        return sum(1 for path in self.root.rglob("*.json") if path.is_file())

    def _entry_checksum(self, entry_without_checksum: dict[str, Any]) -> str:
        return sha256_obj(entry_without_checksum)

    def _load_path(self, path: Path, expected_key: str | None = None) -> dict[str, Any]:
        try:
            with open(path, "rb") as handle:
                entry = json.loads(handle.read().decode("utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise AppError(
                E_CACHE_CORRUPT,
                "缓存条目无法读取",
                details={"path": str(path), "reason": str(exc)},
            ) from exc
        checksum = entry.get("entry_checksum")
        body = {key: value for key, value in entry.items() if key != "entry_checksum"}
        if checksum != self._entry_checksum(body):
            raise AppError(
                E_CACHE_CORRUPT,
                "缓存条目校验和不一致",
                details={"path": str(path)},
            )
        key = entry.get("key")
        if expected_key is not None and key != expected_key:
            raise AppError(
                E_CACHE_CORRUPT,
                "缓存键与路径不一致",
                details={"expected": expected_key, "actual": key, "path": str(path)},
            )
        if entry.get("content_sha256") != sha256_obj(entry.get("content")):
            raise AppError(
                E_CACHE_CORRUPT,
                "缓存内容哈希不一致",
                details={"path": str(path), "key": key},
            )
        if key != sha256_obj(entry.get("key_components")):
            raise AppError(
                E_CACHE_CORRUPT,
                "缓存键组成无法重算出缓存键",
                details={"path": str(path), "key": key},
            )
        return entry

    def get(self, key: str) -> dict[str, Any] | None:
        path = self._path(key)
        if not path.exists():
            return None
        return self._load_path(path, expected_key=key)

    def explain(self, key: str) -> dict[str, Any]:
        entry = self.get(key)
        if entry is None:
            raise AppError(
                "E_CACHE_MISS",
                "缓存键不存在",
                details={"key": key},
            )
        return {
            "key": key,
            "kind": entry["kind"],
            "key_components": entry["key_components"],
            "content_sha256": entry["content_sha256"],
            "entry_checksum": entry["entry_checksum"],
        }

    def _acquire_lock(self, key: str, timeout_seconds: float = 10.0) -> int | None:
        lock_path = self._lock_path(key)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            try:
                descriptor = os.open(
                    lock_path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
                os.write(descriptor, f"{os.getpid()}\n".encode("ascii"))
                os.fsync(descriptor)
                return descriptor
            except FileExistsError:
                if self._path(key).exists():
                    return None
                try:
                    age = time.time() - lock_path.stat().st_mtime
                    if age > 30:
                        lock_path.unlink(missing_ok=True)
                        continue
                except FileNotFoundError:
                    continue
                time.sleep(0.005)
        raise AppError(
            E_LOCK_TIMEOUT,
            "等待缓存写锁超时",
            details={"key": key},
        )

    def _release_lock(self, key: str, descriptor: int) -> None:
        os.close(descriptor)
        self._lock_path(key).unlink(missing_ok=True)

    def _atomic_write(self, path: Path, payload: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{path.stem}.", suffix=".tmp", dir=path.parent
        )
        temporary_path = Path(temporary)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, path)
            directory_descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        finally:
            temporary_path.unlink(missing_ok=True)

    @contextmanager
    def _global_write_lock(self):
        lock_path = self.root / ".cache-write.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "a+b") as handle:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _build_entry(
        self,
        key: str,
        kind: str,
        key_components: dict[str, Any],
        content: dict[str, Any],
    ) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "key": key,
            "kind": kind,
            "key_components": key_components,
            "content": content,
            "content_sha256": sha256_obj(content),
        }
        entry["entry_checksum"] = self._entry_checksum(entry)
        return entry

    def get_or_compute(
        self,
        key_components: dict[str, Any],
        kind: str,
        compute: Callable[[], dict[str, Any]],
    ) -> CacheResult:
        key = self.key_for(key_components)
        existing = self.get(key)
        if existing is not None:
            return CacheResult(
                key=key,
                key_components=existing["key_components"],
                kind=existing["kind"],
                content=existing["content"],
                content_sha256=existing["content_sha256"],
                status="hit",
            )

        content = compute()
        content_sha256 = sha256_obj(content)
        descriptor = self._acquire_lock(key)
        if descriptor is None:
            existing = self.get(key)
            if existing is None:
                raise AppError(
                    E_CACHE_CORRUPT,
                    "写锁释放后仍未找到完整缓存条目",
                    details={"key": key},
                )
            return CacheResult(
                key=key,
                key_components=existing["key_components"],
                kind=existing["kind"],
                content=existing["content"],
                content_sha256=existing["content_sha256"],
                status="hit",
            )

        try:
            existing = self.get(key)
            if existing is not None:
                return CacheResult(
                    key=key,
                    key_components=existing["key_components"],
                    kind=existing["kind"],
                    content=existing["content"],
                    content_sha256=existing["content_sha256"],
                    status="hit",
                )
            with self._thread_lock:
                with self._global_write_lock():
                    entry_count = self._entry_count()
                    if entry_count >= self.max_entries:
                        if self.resource_policy == "degrade":
                            return CacheResult(
                                key=key,
                                key_components=key_components,
                                kind=kind,
                                content=content,
                                content_sha256=content_sha256,
                                status="bypass",
                                degradation="CACHE_ENTRY_LIMIT",
                            )
                        raise resource_limit(
                            "max_cache_entries",
                            "缓存条目总量超过资源上限",
                            entry_count=entry_count,
                            max_cache_entries=self.max_entries,
                        )
                    entry = self._build_entry(key, kind, key_components, content)
                    self._atomic_write(self._path(key), canonical_bytes(entry))
            return CacheResult(
                key=key,
                key_components=key_components,
                kind=kind,
                content=content,
                content_sha256=content_sha256,
                status="miss",
                write_owner=True,
            )
        finally:
            self._release_lock(key, descriptor)

    def clear(self) -> None:
        if self.root.exists():
            shutil.rmtree(self.root)
        self.root.mkdir(parents=True, exist_ok=True)

    def count_entries(self) -> int:
        return self._entry_count()

    def list_entries(self) -> list[dict[str, Any]]:
        entries = []
        for path in sorted(self.root.rglob("*.json")):
            entry = self._load_path(path)
            entries.append(
                {
                    "key": entry["key"],
                    "kind": entry["kind"],
                    "content_sha256": entry["content_sha256"],
                    "key_components": entry["key_components"],
                }
            )
        return entries

