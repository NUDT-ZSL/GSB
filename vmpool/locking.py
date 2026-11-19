"""Cross-platform process-level file lock.

Windows uses msvcrt.locking; POSIX uses fcntl.flock. SQLite transactions
remain the source of truth, while this lock removes writer-writer races before
two-phase recovery work begins.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from types import TracebackType

from .errors import E_DB_BUSY, BusinessError


class FileLock:
    def __init__(self, path: str | Path, timeout: float = 30.0) -> None:
        self.path = Path(path)
        self.timeout = timeout
        self._file = None

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(self.path, "a+b")
        self._file.seek(0, os.SEEK_END)
        if self._file.tell() == 0:
            self._file.write(b"\0")
            self._file.flush()
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    self._file.seek(0)
                    msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if time.monotonic() >= deadline:
                    self._file.close()
                    self._file = None
                    raise BusinessError(
                        E_DB_BUSY,
                        "等待文件锁超时",
                        {"lock_file": str(self.path)},
                        exit_code=4,
                    ) from None
                time.sleep(0.02)

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._file is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._file.seek(0)
                msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        finally:
            self._file.close()
            self._file = None
