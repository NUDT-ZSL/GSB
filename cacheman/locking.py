"""Cross-platform advisory file lock used around SQLite write phases."""

from __future__ import annotations

import os
import time
from pathlib import Path
from types import TracebackType

from .errors import CACHE_LOCK_TIMEOUT, CacheError


class FileLock:
    def __init__(self, path: Path, timeout_seconds: float = 15.0) -> None:
        self.path = Path(path)
        self.timeout_seconds = timeout_seconds
        self._file = None
        self._fd: int | None = None

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("a+b")
        if self._file.tell() == 0:
            self._file.write(b"\0")
            self._file.flush()
        self._file.seek(0)
        self._fd = self._file.fileno()
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            try:
                self._try_lock()
                return self
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    self._close()
                    raise CacheError(
                        CACHE_LOCK_TIMEOUT,
                        "timed out waiting for the cache state file lock",
                        fields={
                            "lock_path": str(self.path),
                            "timeout_seconds": self.timeout_seconds,
                        },
                    )
                time.sleep(0.05)

    def _try_lock(self) -> None:
        assert self._fd is not None
        if os.name == "nt":
            import msvcrt

            self._file.seek(0)
            msvcrt.locking(self._fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(self) -> None:
        if self._fd is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._file.seek(0)
                msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fd, fcntl.LOCK_UN)
        except OSError:
            pass

    def _close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None
            self._fd = None

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._unlock()
        self._close()
