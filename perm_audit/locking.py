from __future__ import annotations

import os
import time
from pathlib import Path
from types import TracebackType
from typing import Optional

from .codes import E_LOCK


class LockConflict(RuntimeError):
    code = E_LOCK

    def __init__(self, path: Path, owner: str = "another audit process"):
        super().__init__(f"{E_LOCK}: audit work directory is locked: {path} ({owner})")
        self.path = path
        self.owner = owner


class WorkDirLock:
    """Non-blocking exclusive lock. POSIX uses flock; Windows uses msvcrt locking."""

    def __init__(self, workdir: str | Path, timeout_ms: int = 0):
        self.workdir = Path(workdir)
        self.path = self.workdir / ".audit.lock"
        self.timeout_ms = timeout_ms
        self._handle = None
        self._locked = False

    def acquire(self) -> "WorkDirLock":
        self.workdir.mkdir(parents=True, exist_ok=True)
        self._handle = open(self.path, "a+b")
        if os.name == "nt":  # pragma: no cover - Windows lock preparation
            self._handle.seek(0, os.SEEK_END)
            if self._handle.tell() == 0:
                self._handle.write(b"0")
                self._handle.flush()
        deadline = time.monotonic() + max(0, self.timeout_ms) / 1000.0
        while True:
            try:
                if os.name == "nt":  # pragma: no cover - exercised on Windows
                    import msvcrt

                    self._handle.seek(0)
                    msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._locked = True
                self._handle.write(f"{os.getpid()}\n".encode("ascii"))
                self._handle.flush()
                return self
            except OSError:
                if time.monotonic() >= deadline:
                    self._handle.close()
                    self._handle = None
                    raise LockConflict(self.path)
                time.sleep(0.02)

    def release(self) -> None:
        if not self._handle:
            return
        try:
            if self._locked:
                if os.name == "nt":  # pragma: no cover
                    import msvcrt

                    self._handle.seek(0)
                    msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None
            self._locked = False

    def __enter__(self) -> "WorkDirLock":
        return self.acquire()

    def __exit__(
        self,
        exc_type: Optional[type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> None:
        self.release()
