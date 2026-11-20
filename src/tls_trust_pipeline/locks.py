from __future__ import annotations

import os
import time
from pathlib import Path
from types import TracebackType
from typing import Self

from .errors import E_LOCK_TIMEOUT, PipelineError


class FileLock:
    """Portable advisory lock with a bounded wait."""

    def __init__(self, path: Path, timeout_seconds: float = 20.0) -> None:
        self.path = path
        self.timeout_seconds = timeout_seconds
        self._handle = None

    def __enter__(self) -> Self:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            try:
                self._acquire(handle)
                break
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    handle.close()
                    raise PipelineError(
                        E_LOCK_TIMEOUT,
                        "timed out waiting for state lock",
                        {"lock": str(self.path)},
                    )
                time.sleep(0.02)
        self._handle = handle
        return self

    @staticmethod
    def _acquire(handle) -> None:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _release(handle) -> None:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._handle is not None:
            self._release(self._handle)
            self._handle.close()
            self._handle = None

