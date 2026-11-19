"""Portable process-level lock used in addition to SQLite transactions."""

from __future__ import annotations

import os
import time
from pathlib import Path

from .errors import E_DB_ERROR, BusinessError


class FileLockTimeout(BusinessError):
    def __init__(self, path: Path) -> None:
        super().__init__(
            E_DB_ERROR,
            "timed out waiting for the instance file lock",
            fields={"lock_path": str(path)},
            exit_code=3,
        )


class InstanceFileLock:
    def __init__(self, path: str | Path, timeout_ms: int = 30_000) -> None:
        self.path = Path(path)
        self.timeout_ms = timeout_ms
        self._file: object | None = None

    def __enter__(self) -> "InstanceFileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        deadline = time.monotonic() + self.timeout_ms / 1000.0
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._file = handle
                return self
            except (OSError, BlockingIOError):
                if time.monotonic() >= deadline:
                    handle.close()
                    raise FileLockTimeout(self.path)
                time.sleep(0.02)

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        handle = self._file
        if handle is None:
            return
        stream = handle
        try:
            stream.seek(0)  # type: ignore[attr-defined]
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)  # type: ignore[attr-defined]
        finally:
            stream.close()  # type: ignore[attr-defined]
            self._file = None

