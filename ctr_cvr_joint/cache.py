"""Content-addressed local cache and non-blocking incremental commit lock."""

from __future__ import annotations

import json
import os
import threading
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import E_CACHE_CORRUPT, E_STAGE_BUSY, fail
from .hashing import atomic_write_json, read_json, sha256_obj


@dataclass(frozen=True)
class CacheKey:
    key: str
    components: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "components": self.components}


def make_cache_key(components: dict[str, Any]) -> CacheKey:
    canonical_components = dict(components)
    return CacheKey(key=sha256_obj(canonical_components), components=canonical_components)


class ContentAddressedCache:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, kind: str, key: str) -> Path:
        if len(key) != 64 or any(character not in "0123456789abcdef" for character in key):
            fail(E_CACHE_CORRUPT, "cache key is not a sha256 hex digest", field="key")
        return self.root / kind / key[:2] / f"{key}.json"

    def get(self, kind: str, key: CacheKey | str) -> dict[str, Any] | None:
        key_text = key.key if isinstance(key, CacheKey) else key
        path = self._path(kind, key_text)
        if not path.exists():
            return None
        try:
            envelope = read_json(path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            fail(
                E_CACHE_CORRUPT,
                "cache entry cannot be read",
                field="cache",
                path=str(path),
                reason=str(exc),
            )
        if not isinstance(envelope, dict):
            fail(
                E_CACHE_CORRUPT,
                "cache entry is not an object",
                field="cache",
                path=str(path),
            )
        if envelope.get("kind") != kind or envelope.get("key") != key_text:
            fail(
                E_CACHE_CORRUPT,
                "cache entry kind or key does not match its path",
                field="cache",
                path=str(path),
                key=key_text,
            )
        product = envelope.get("product")
        expected = envelope.get("product_hash")
        actual = sha256_obj(product)
        if expected != actual:
            fail(
                E_CACHE_CORRUPT,
                "cached product checksum mismatch",
                field="product_hash",
                path=str(path),
                expected=expected,
                actual=actual,
            )
        if isinstance(key, CacheKey) and envelope.get("components") != key.components:
            fail(
                E_CACHE_CORRUPT,
                "cache key components do not match the requested components",
                field="components",
                path=str(path),
            )
        return envelope

    def put(
        self,
        kind: str,
        key: CacheKey,
        *,
        product: Any,
        request_id: str | None,
        node_path: tuple[str, ...] | list[str] | None = None,
    ) -> dict[str, Any]:
        product_hash = sha256_obj(product)
        envelope = {
            "schema": 1,
            "kind": kind,
            "key": key.key,
            "components": key.components,
            "request_id": request_id,
            "node_path": list(node_path or []),
            "product_hash": product_hash,
            "product": product,
        }
        path = self._path(kind, key.key)
        if path.exists():
            existing = self.get(kind, key)
            if existing is None:
                fail(E_CACHE_CORRUPT, "cache entry disappeared during lookup")
            if existing["product_hash"] != product_hash:
                fail(
                    E_CACHE_CORRUPT,
                    "different products were produced for the same cache key",
                    field="product_hash",
                    key=key.key,
                )
            return existing
        atomic_write_json(path, envelope)
        return envelope

    def find_by_request(self, request_id: str) -> list[dict[str, Any]]:
        matches: list[dict[str, Any]] = []
        for path in sorted(self.root.glob("*/*/*.json")):
            try:
                envelope = read_json(path)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if envelope.get("request_id") != request_id:
                continue
            product = envelope.get("product")
            if sha256_obj(product) != envelope.get("product_hash"):
                fail(
                    E_CACHE_CORRUPT,
                    "cache entry checksum mismatch",
                    field="product_hash",
                    path=str(path),
                    request_id=request_id,
                )
            matches.append(
                {
                    "kind": envelope["kind"],
                    "key": envelope["key"],
                    "components": envelope["components"],
                    "product_hash": envelope["product_hash"],
                    "node_path": envelope.get("node_path", []),
                }
            )
        return sorted(matches, key=lambda item: (item["kind"], item["key"]))

    def count(self, kind: str | None = None) -> int:
        pattern = f"{kind}/*/*.json" if kind else "*/*/*.json"
        return sum(1 for _ in self.root.glob(pattern))


_IN_PROCESS_LOCKS: dict[str, str] = {}
_IN_PROCESS_GUARD = threading.Lock()


class StageLock(AbstractContextManager["StageLock"]):
    """One non-blocking writer at a time.

    The in-process guard catches thread contention immediately; an OS file lock
    catches contention between processes without waiting.
    """

    def __init__(self, root: str | Path, *, name: str = ".stage.lock") -> None:
        self.path = Path(root) / name
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fd: int | None = None
        self._owner = f"{os.getpid()}:{threading.get_ident()}"
        self._held = False

    def __enter__(self) -> "StageLock":
        normalized = str(self.path.resolve())
        with _IN_PROCESS_GUARD:
            owner = _IN_PROCESS_LOCKS.get(normalized)
            if owner is not None:
                fail(
                    E_STAGE_BUSY,
                    "another incremental commit is already active",
                    stage="incremental_commit",
                    owner=owner,
                )
            _IN_PROCESS_LOCKS[normalized] = self._owner
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
            self._fd = fd
            self._acquire_os_lock(fd)
        except Exception:
            with _IN_PROCESS_GUARD:
                _IN_PROCESS_LOCKS.pop(normalized, None)
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None
            raise
        self._held = True
        return self

    def _acquire_os_lock(self, fd: int) -> None:
        try:
            import fcntl

            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                fail(
                    E_STAGE_BUSY,
                    "another process holds the incremental commit lock",
                    stage="incremental_commit",
                )
            return
        except ImportError:
            pass
        try:
            import msvcrt

            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError:
                fail(
                    E_STAGE_BUSY,
                    "another process holds the incremental commit lock",
                    stage="incremental_commit",
                )
            return
        except ImportError:
            pass
        # Fallback for exotic platforms: the in-process guard remains active,
        # and an atomic marker detects another writer.
        if self.path.stat().st_size > 0:
            fail(
                E_STAGE_BUSY,
                "another process holds the incremental commit lock",
                stage="incremental_commit",
            )
        os.ftruncate(fd, 0)
        os.write(fd, self._owner.encode("ascii"))

    def __exit__(self, exc_type, exc, traceback) -> None:
        normalized = str(self.path.resolve())
        if self._fd is not None:
            try:
                import fcntl

                fcntl.flock(self._fd, fcntl.LOCK_UN)
            except ImportError:
                pass
            except OSError:
                pass
            try:
                os.close(self._fd)
            finally:
                self._fd = None
        with _IN_PROCESS_GUARD:
            _IN_PROCESS_LOCKS.pop(normalized, None)
        self._held = False

    @property
    def held(self) -> bool:
        return self._held


def require_lock_owner(lock: StageLock) -> None:
    if not lock.held:
        fail(E_STAGE_BUSY, "incremental commit lock is not held", stage="incremental_commit")


__all__ = [
    "CacheKey",
    "make_cache_key",
    "ContentAddressedCache",
    "StageLock",
    "require_lock_owner",
]

