from __future__ import annotations

import os
import threading
from pathlib import Path
from types import TracebackType


_thread_lock = threading.RLock()


class FileLock:
    """Process-wide and OS-level advisory lock used for write serialization."""

    def __init__(self, path: Path):
        self.path = path
        self._fd: int | None = None
        self._depth = 0

    def __enter__(self) -> "FileLock":
        _thread_lock.acquire()
        self._depth += 1
        if self._fd is not None:
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX)
        except Exception:
            os.close(fd)
            _thread_lock.release()
            self._depth -= 1
            raise
        self._fd = fd
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._depth -= 1
        if self._depth == 0 and self._fd is not None:
            fd = self._fd
            self._fd = None
            try:
                if os.name == "nt":
                    import msvcrt

                    os.lseek(fd, 0, os.SEEK_SET)
                    try:
                        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                    except OSError:
                        pass
                else:
                    import fcntl

                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
        _thread_lock.release()
