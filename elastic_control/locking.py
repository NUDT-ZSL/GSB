"""Cross-platform advisory file locks.

SQLite provides transactional isolation; this lock additionally serialises
initialisation, policy patches and a whole round's action execution across
processes.  The latter makes recovery of a ``running`` action safe: a new
process cannot enter the round critical section until the previous owner has
exited (including a forced process termination, which releases the OS lock).
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from types import TracebackType
from typing import IO

from .errors import LOCK_ERROR, ControlError

if os.name == "nt":  # pragma: no cover - exercised on Windows
    import msvcrt
else:  # pragma: no cover - branching is platform-deterministic
    import fcntl


_thread_lock = threading.RLock()
_thread_locks: dict[str, threading.RLock] = {}


def _thread_lock_for(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _thread_lock:
        lock = _thread_locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _thread_locks[key] = lock
        return lock


class FileLock:
    def __init__(self, path: str | os.PathLike[str], timeout: float = 30.0) -> None:
        self.path = Path(path)
        self.timeout = timeout
        self._handle: IO[bytes] | None = None

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a+b")
        if os.name == "nt" and self.path.stat().st_size == 0:  # pragma: no cover
            # msvcrt.locking requires the byte range to exist.
            self._handle.write(b"\0")
            self._handle.flush()
        started = time.monotonic()
        while True:
            try:
                if os.name == "nt":  # pragma: no cover
                    self._handle.seek(0)
                    msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except (OSError, BlockingIOError) as exc:
                if time.monotonic() - started >= self.timeout:
                    self._handle.close()
                    self._handle = None
                    raise ControlError(
                        LOCK_ERROR,
                        f"timed out waiting for lock {self.path}",
                        {"path": str(self.path)},
                    ) from exc
                time.sleep(0.01)

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._handle is None:
            return
        try:
            if os.name == "nt":  # pragma: no cover
                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None


class ProcessThreadLock:
    """A process file lock guarded by a process-local re-entrant lock."""

    def __init__(self, path: str | os.PathLike[str], timeout: float = 30.0) -> None:
        self.path = Path(path)
        self._thread_lock = _thread_lock_for(self.path)
        self._lock = FileLock(path, timeout)

    def __enter__(self) -> "ProcessThreadLock":
        self._thread_lock.acquire()
        try:
            self._lock.__enter__()
        except BaseException:
            self._thread_lock.release()
            raise
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            self._lock.__exit__(exc_type, exc, traceback)
        finally:
            self._thread_lock.release()
