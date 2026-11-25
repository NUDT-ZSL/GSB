from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .canonical import safe_slug
from .errors import ErrorCode, LayerForgeError


class FileLock:
    """Advisory exclusive lock backed by ``flock``.

    The lock file is never deleted because unlinking it could race with another
    process waiting on the same inode.  Stable lock names therefore serialize
    exactly one step-parent or cache-key critical section.
    """

    def __init__(self, lock_dir: Path, name: str, *, timeout: float = 30.0) -> None:
        lock_dir.mkdir(parents=True, exist_ok=True)
        self.path = lock_dir / f"{safe_slug(name)}.lock"
        self.timeout = timeout
        self._handle = None

    def acquire(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a+b")
        flags = fcntl.LOCK_EX
        if self.timeout is not None and self.timeout <= 0:
            flags |= fcntl.LOCK_NB
        if self.timeout and self.timeout > 0:
            # flock has no portable timeout; an alarm is inappropriate in
            # worker threads.  SQLite and short critical sections make a
            # blocking wait the predictable choice for this local component.
            flags &= ~fcntl.LOCK_NB
        try:
            fcntl.flock(self._handle.fileno(), flags)
        except BlockingIOError as exc:
            self._handle.close()
            self._handle = None
            raise LayerForgeError(
                "lock is busy",
                code=ErrorCode.LOCK_BUSY,
                details={"lock": str(self.path)},
            ) from exc
        self._handle.seek(0)
        self._handle.truncate()
        self._handle.write(f"{os.getpid()}\n".encode("ascii"))
        self._handle.flush()
        return self

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None

    def __enter__(self) -> "FileLock":
        return self.acquire()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


@contextmanager
def named_lock(lock_dir: Path, name: str) -> Iterator[FileLock]:
    lock = FileLock(lock_dir, name).acquire()
    try:
        yield lock
    finally:
        lock.release()
