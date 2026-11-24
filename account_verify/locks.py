from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Dict

from .codes import E_LOCK
from .errors import LockError

_THREAD_LOCKS_GUARD = threading.Lock()
_THREAD_LOCKS: Dict[str, threading.RLock] = {}


def _thread_lock_for(path: Path) -> threading.RLock:
    key = str(Path(path).resolve())
    with _THREAD_LOCKS_GUARD:
        lock = _THREAD_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _THREAD_LOCKS[key] = lock
        return lock


class FileWriteLock:
    """Exclusive cross-process plus in-process lock for one work directory."""

    def __init__(self, path: Path, *, timeout_seconds: float = 0.0) -> None:
        self.path = Path(path)
        self.timeout_seconds = max(0.0, float(timeout_seconds))
        self._fd = None
        self._thread_lock = _thread_lock_for(self.path)
        self._thread_acquired = False

    def acquire(self) -> "FileWriteLock":
        deadline = time.monotonic() + self.timeout_seconds
        thread_deadline = self.timeout_seconds if self.timeout_seconds > 0 else -1
        acquired = self._thread_lock.acquire(
            blocking=self.timeout_seconds > 0,
            timeout=thread_deadline if self.timeout_seconds > 0 else -1,
        )
        if not acquired:
            raise LockError(
                "state directory is locked by another writer",
                code=E_LOCK,
                details={"lock_path": str(self.path), "scope": "thread"},
            )
        self._thread_acquired = True
        self.path.parent.mkdir(parents=True, exist_ok=True)

        try:
            self._fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o600)
            os.ftruncate(self._fd, 0)
            os.write(self._fd, f"{os.getpid()}\n".encode("ascii", "replace"))
            os.fsync(self._fd)
            self._acquire_os_lock(deadline)
        except Exception:
            self.release()
            raise
        return self

    def _acquire_os_lock(self, deadline: float) -> None:
        assert self._fd is not None
        while True:
            try:
                if os.name == "nt":  # pragma: no cover - exercised on Windows
                    import msvcrt

                    self._fd.seek(0)
                    msvcrt.locking(self._fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except OSError:
                if time.monotonic() >= deadline:
                    raise LockError(
                        "state directory is locked by another writer",
                        code=E_LOCK,
                        details={"lock_path": str(self.path), "scope": "process"},
                    )
                time.sleep(0.02)

    def release(self) -> None:
        if self._fd is not None:
            try:
                if os.name == "nt":  # pragma: no cover
                    import msvcrt

                    self._fd.seek(0)
                    try:
                        msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
                    except OSError:
                        pass
                else:
                    import fcntl

                    fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None
        if self._thread_acquired:
            self._thread_lock.release()
            self._thread_acquired = False

    def __enter__(self) -> "FileWriteLock":
        return self.acquire()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()
