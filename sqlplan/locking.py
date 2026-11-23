from __future__ import annotations

import os
import time
from pathlib import Path

from .errors import BusinessError, ErrorCode


class FileLock:
    """Cross-platform advisory exclusive lock backed by a single lock file."""

    def __init__(self, path: str | Path, timeout_seconds: float = 15.0) -> None:
        self.path = Path(path)
        self.timeout_seconds = timeout_seconds
        self._handle = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a+b")
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            try:
                self._try_lock()
                self._handle.seek(0)
                self._handle.truncate()
                self._handle.write(f"{os.getpid()}\n".encode("ascii"))
                self._handle.flush()
                return
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    self._handle.close()
                    self._handle = None
                    raise BusinessError(
                        ErrorCode.E_LOCK_TIMEOUT,
                        "等待应用文件锁超时",
                        {
                            "lock_path": str(self.path),
                            "timeout_seconds": self.timeout_seconds,
                        },
                    ) from None
                time.sleep(0.05)

    def _try_lock(self) -> None:
        assert self._handle is not None
        if os.name == "nt":
            import msvcrt

            self._handle.seek(0)
            if self._handle.read(1) == b"":
                self._handle.write(b"\0")
                self._handle.flush()
            self._handle.seek(0)
            msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None

    def __enter__(self) -> "FileLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()
