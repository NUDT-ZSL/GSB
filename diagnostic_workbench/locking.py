from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class FileLock:
    """Small cross-platform advisory byte-range file lock."""

    def __init__(self, path: Path, timeout_seconds: float = 20.0) -> None:
        self.path = Path(path)
        self.timeout_seconds = timeout_seconds
        self._fd: int | None = None

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        if os.fstat(self._fd).st_size == 0:
            os.write(self._fd, b"\0")
            os.fsync(self._fd)
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    os.lseek(self._fd, 0, os.SEEK_SET)
                    msvcrt.locking(self._fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    os.close(self._fd)
                    self._fd = None
                    raise TimeoutError(
                        f"Timed out waiting for file lock {self.path}"
                    ) from None
                # Lock waiting may use wall time; no switch, cooldown, or budget
                # decision is derived from it.
                time.sleep(0.01)

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self._fd is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(self._fd, 0, os.SEEK_SET)
                msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None


@contextmanager
def lock_path(path: Path) -> Iterator[None]:
    with FileLock(path):
        yield

