"""Blocking and non-blocking advisory file locks for Linux/WSL."""

from __future__ import annotations

import fcntl
import hashlib
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .errors import E_LOCK, WorkbenchError


class FileLock:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = None

    def acquire(self, *, blocking: bool = True) -> "FileLock":
        self._handle = self.path.open("a+")
        flags = fcntl.LOCK_EX
        if not blocking:
            flags |= fcntl.LOCK_NB
        try:
            fcntl.flock(self._handle.fileno(), flags)
        except BlockingIOError as exc:
            self._handle.close()
            self._handle = None
            raise WorkbenchError(E_LOCK, "lock is busy", {"path": str(self.path)}) from exc
        return self

    def release(self) -> None:
        if self._handle is not None:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            self._handle.close()
            self._handle = None

    def __enter__(self) -> "FileLock":
        return self.acquire(blocking=True)

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


@contextmanager
def global_lock(lock_path: Path, *, blocking: bool = True) -> Iterator[FileLock]:
    lock = FileLock(lock_path).acquire(blocking=blocking)
    try:
        yield lock
    finally:
        lock.release()


def session_lock_path(lock_dir: Path, connection_id: str) -> Path:
    digest = hashlib.sha256(connection_id.encode("utf-8")).hexdigest()[:32]
    return Path(lock_dir) / f"{digest}.lock"


@contextmanager
def session_lock(lock_dir: Path, connection_id: str, *, blocking: bool = True) -> Iterator[FileLock]:
    lock = FileLock(session_lock_path(lock_dir, connection_id)).acquire(blocking=blocking)
    try:
        yield lock
    finally:
        lock.release()
