"""Non-blocking-first cross-process file lock."""

from __future__ import annotations

import os
import time
from pathlib import Path
from types import TracebackType
from typing import BinaryIO

from .codes import E_LOCK
from .errors import GuardError


class FileLock:
    def __init__(self, path: str | os.PathLike[str], timeout_ms: int = 0) -> None:
        if timeout_ms < 0:
            raise ValueError("timeout_ms must be nonnegative")
        self.path = Path(path)
        self.timeout_ms = timeout_ms
        self._handle: BinaryIO | None = None

    def acquire(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")
        deadline = time.monotonic() + self.timeout_ms / 1000.0
        while True:
            try:
                if os.name == "nt":  # pragma: no cover - exercised on Windows
                    import msvcrt

                    handle.seek(0, os.SEEK_END)
                    if handle.tell() == 0:
                        handle.write(b"guard-lock")
                        handle.flush()
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._handle = handle
                return self
            except OSError:
                if time.monotonic() >= deadline:
                    handle.close()
                    raise GuardError(
                        E_LOCK,
                        "workspace checkpoint lock is held by another writer",
                        details={"lock_path": str(self.path)},
                    )
                time.sleep(0.02)

    def release(self) -> None:
        if self._handle is None:
            return
        handle = self._handle
        self._handle = None
        try:
            if os.name == "nt":  # pragma: no cover - Windows only
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def __enter__(self) -> "FileLock":
        return self.acquire()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()
