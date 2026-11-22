from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from types import TracebackType
from typing import IO

from .errors import LOCK_001, error


class FileLock:
    """Cross-platform advisory process lock.

    Linux/macOS use ``flock``. Windows uses a one-byte ``msvcrt.locking``
    region. The lock file only serializes writes; it is never part of runtime
    state and therefore may be recreated safely.
    """

    def __init__(self, path: str | Path, timeout_seconds: float = 30.0) -> None:
        self.path = Path(path)
        self.timeout_seconds = timeout_seconds
        self._file: IO[bytes] | None = None
        self._thread_lock = threading.RLock()

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(self.path, "a+b")
        self._file.seek(0, os.SEEK_END)
        if self._file.tell() == 0:
            self._file.write(b"\0")
            self._file.flush()
        self._file.seek(0)
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    assert self._file is not None
                    self._file.seek(0)
                    msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    assert self._file is not None
                    fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except (BlockingIOError, OSError) as exc:
                if time.monotonic() >= deadline:
                    self._file.close()
                    self._file = None
                    raise error(
                        LOCK_001,
                        "timed out waiting for the connection write lock",
                        details={"path": str(self.path)},
                        http_status=503,
                        category="concurrency",
                    ) from exc
                time.sleep(0.005)

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
