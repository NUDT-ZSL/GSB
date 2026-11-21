from __future__ import annotations

import os
import time
from pathlib import Path
from typing import BinaryIO


class AdvisoryFileLock:
    """Cross-platform blocking advisory lock backed by a sidecar file."""

    def __init__(self, path: str | Path, timeout_seconds: float = 30.0) -> None:
        self.path = Path(path)
        self.timeout_seconds = timeout_seconds
        self._handle: BinaryIO | None = None

    def __enter__(self) -> "AdvisoryFileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a+b")
        self._handle.seek(0, os.SEEK_END)
        if self._handle.tell() == 0:
            self._handle.write(b"\0")
            self._handle.flush()
        self._handle.seek(0)
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            try:
                self._acquire()
                return self
            except OSError:
                if time.monotonic() >= deadline:
                    self._handle.close()
                    self._handle = None
                    raise TimeoutError(f"timed out acquiring lock {self.path}")
                time.sleep(0.02)

    def _acquire(self) -> None:
        assert self._handle is not None
        if os.name == "nt":
            import msvcrt

            self._handle.seek(0)
            msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self._handle is None:
            return
        if os.name == "nt":
            import msvcrt

            self._handle.seek(0)
            msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        self._handle.close()
        self._handle = None

