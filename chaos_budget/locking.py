from __future__ import annotations

import errno
import fcntl
import os
import time
from pathlib import Path
from typing import Optional

from .errors import ChaosError


class FileLock:
    """Blocking advisory file lock released automatically on process exit/kill."""

    def __init__(self, path: os.PathLike[str] | str, timeout_seconds: float = 10.0) -> None:
        self.path = Path(path)
        self.timeout_seconds = timeout_seconds
        self._fd: Optional[int] = None

    def acquire(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                os.ftruncate(self._fd, 0)
                os.write(self._fd, f"{os.getpid()}\n".encode("ascii"))
                return self
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    self.release()
                    raise
                if time.monotonic() >= deadline:
                    self.release()
                    raise ChaosError(
                        "LOCK_TIMEOUT",
                        f"timed out waiting for file lock: {self.path}",
                        details={"lock_path": str(self.path), "timeout_seconds": self.timeout_seconds},
                        exit_code=3,
                    ) from exc
                time.sleep(0.02)

    def release(self) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None

    def __enter__(self) -> "FileLock":
        return self.acquire()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()
