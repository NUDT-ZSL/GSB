"""Filesystem, JSON, hashing, and locking helpers."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any, Iterator, Optional

from .codes import E_LOCK, E_SNAPSHOT_VERSION


class GuardError(Exception):
    """Expected operational failure with a stable business code."""

    def __init__(self, business_code: str, message: str, detail: Optional[dict[str, Any]] = None):
        super().__init__(message)
        self.business_code = business_code
        self.message = message
        self.detail = detail or {}


class LockUnavailable(GuardError):
    def __init__(self, path: str):
        super().__init__(E_LOCK, "checkpoint lock is held by another process", {"lock_path": path})


class SnapshotVersionChanged(GuardError):
    def __init__(self, path: str, expected: int, actual: Optional[int]):
        super().__init__(
            E_SNAPSHOT_VERSION,
            "snapshot version changed before atomic replacement",
            {"snapshot_path": path, "expected_version": expected, "actual_version": actual},
        )


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        descriptor = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def atomic_write_json(
    path: Path,
    payload: Any,
    *,
    expected_snapshot_version: Optional[int] = None,
    require_parse: bool = True,
) -> None:
    """Write JSON to a same-directory temp file, then atomically replace.

    When ``expected_snapshot_version`` is provided, the current file is read
    under the caller's file lock and its ``snapshot_version`` must match. This
    closes the compare-before-replace race even before the rename.
    """

    ensure_parent(path)
    if expected_snapshot_version is not None:
        if not path.exists():
            raise SnapshotVersionChanged(
                str(path), expected_snapshot_version, None
            )
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
            actual = current.get("snapshot_version")
        except (OSError, ValueError, AttributeError) as exc:
            raise GuardError(
                E_SNAPSHOT_VERSION,
                "existing snapshot cannot be version-checked",
                {"snapshot_path": str(path), "error": str(exc)},
            ) from exc
        if actual != expected_snapshot_version:
            raise SnapshotVersionChanged(str(path), expected_snapshot_version, actual)

    raw = canonical_json_bytes(payload)
    if require_parse:
        json.loads(raw.decode("utf-8"))

    temp_path = path.parent / (".%s.%d.%s.tmp" % (path.name, os.getpid(), uuid.uuid4().hex))
    descriptor: Optional[int] = None
    try:
        descriptor = os.open(
            str(temp_path),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        if require_parse:
            json.loads(temp_path.read_text(encoding="utf-8"))
        os.replace(str(temp_path), str(path))
        fsync_directory(path.parent)
    except Exception:
        with contextlib.suppress(OSError):
            if descriptor is not None:
                os.close(descriptor)
        with contextlib.suppress(OSError):
            temp_path.unlink()
        raise


class FileLock:
    """Non-blocking cross-platform one-byte advisory lock."""

    def __init__(self, path: Path):
        self.path = path
        self._handle: Optional[Any] = None

    def acquire(self) -> None:
        ensure_parent(self.path)
        handle = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, IOError) as exc:
            handle.close()
            raise LockUnavailable(str(self.path)) from exc
        self._handle = handle

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        self._handle = None
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def __enter__(self) -> "FileLock":
        self.acquire()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.release()


@contextlib.contextmanager
def temporary_directory(prefix: str) -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix=prefix) as directory:
        yield Path(directory)
