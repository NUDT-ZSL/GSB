"""File locking, atomic validated writes and snapshot version detection."""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from .canonical import stable_json_bytes
from .codes import Code
from .errors import LockConflict, StorageError


try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - exercised on Windows
    fcntl = None  # type: ignore[assignment]

try:  # Windows
    import msvcrt
except ImportError:  # pragma: no cover - exercised on Windows
    msvcrt = None  # type: ignore[assignment]


SNAPSHOT_VERSION = 2
SUPPORTED_SNAPSHOT_VERSIONS = (1, 2)


class FileLock:
    """Advisory exclusive lock implemented with flock on POSIX and msvcrt on Windows."""

    def __init__(self, path: str | os.PathLike[str], timeout_seconds: float = 10.0):
        self.path = Path(path)
        self.timeout_seconds = timeout_seconds
        self._handle: Any = None

    def __enter__(self) -> "FileLock":
        self.acquire()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.release()

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = open(self.path, "a+b")
        deadline = time.perf_counter() + max(0.0, self.timeout_seconds)
        while True:
            try:
                if fcntl is not None:
                    fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                elif msvcrt is not None:  # pragma: no cover
                    self._handle.seek(0, os.SEEK_END)
                    if self._handle.tell() == 0:
                        self._handle.write(b"l")
                        self._handle.flush()
                    self._handle.seek(0)
                    msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:  # pragma: no cover
                    raise StorageError(Code.E_LOCK, "no file lock primitive is available")
                return
            except (BlockingIOError, OSError):
                if time.perf_counter() >= deadline:
                    self._handle.close()
                    self._handle = None
                    raise LockConflict(
                        Code.E_LOCK,
                        f"timed out waiting for file lock {self.path}",
                        None,
                        lock_path=str(self.path),
                    )
                time.sleep(0.01)

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            elif msvcrt is not None:  # pragma: no cover
                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            self._handle.close()
            self._handle = None


def atomic_write_bytes(
    path: str | os.PathLike[str],
    content: bytes,
    *,
    validator: Callable[[bytes], None] | None = None,
    lock_timeout_seconds: float = 10.0,
) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.parent / ".osl-storage.lock"

    with FileLock(lock_path, timeout_seconds=lock_timeout_seconds):
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())

            temporary.chmod(0o644)
            if validator is not None:
                validator(temporary.read_bytes())

            os.replace(temporary, target)
            _fsync_directory(target.parent)
            return target
        except LockConflict:
            raise
        except Exception:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise


def atomic_write_json(
    path: str | os.PathLike[str],
    value: Any,
    *,
    lock_timeout_seconds: float = 10.0,
) -> Path:
    payload = stable_json_bytes(value) + b"\n"

    def validate(data: bytes) -> None:
        parsed = json.loads(data.decode("utf-8"))
        if not isinstance(parsed, (dict, list)):
            raise StorageError(Code.E_INTERNAL, "JSON snapshot did not parse to object/array")

    return atomic_write_bytes(
        path,
        payload,
        validator=validate,
        lock_timeout_seconds=lock_timeout_seconds,
    )


def read_json(path: str | os.PathLike[str]) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_snapshot(
    path: str | os.PathLike[str],
    snapshot: dict[str, Any],
    *,
    lock_timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    target = Path(path)
    payload = dict(snapshot)
    new_version = int(payload.get("snapshot_version", SNAPSHOT_VERSION))
    payload["snapshot_version"] = new_version

    old_version: int | None = None
    if target.exists():
        try:
            old = json.loads(target.read_text(encoding="utf-8"))
            old_version = int(old.get("snapshot_version")) if isinstance(old, dict) else None
        except (json.JSONDecodeError, OSError, ValueError):
            old_version = None

    if old_version is not None and old_version not in SUPPORTED_SNAPSHOT_VERSIONS:
        raise StorageError(
            Code.E_SNAPSHOT_VERSION,
            f"unsupported existing snapshot version {old_version}",
            None,
            old_version=old_version,
            supported=list(SUPPORTED_SNAPSHOT_VERSIONS),
        )
    if new_version not in SUPPORTED_SNAPSHOT_VERSIONS:
        raise StorageError(
            Code.E_SNAPSHOT_VERSION,
            f"unsupported replacement snapshot version {new_version}",
            None,
            new_version=new_version,
            supported=list(SUPPORTED_SNAPSHOT_VERSIONS),
        )
    if old_version is not None and old_version != new_version:
        payload["previous_snapshot_version"] = old_version

    atomic_write_json(target, payload, lock_timeout_seconds=lock_timeout_seconds)
    return {
        "path": str(target),
        "old_version": old_version,
        "new_version": new_version,
        "version_changed": old_version is not None and old_version != new_version,
    }


def _fsync_directory(directory: Path) -> None:
    if not hasattr(os, "O_DIRECTORY"):
        return
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
