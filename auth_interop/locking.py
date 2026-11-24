from __future__ import annotations

import os
from pathlib import Path
from types import TracebackType
from typing import Optional

from .codes import E_LOCK


class LockConflict(RuntimeError):
    def __init__(self, path: Path, message: str = "checkpoint lock is held") -> None:
        super().__init__(f"{E_LOCK}: {message}: {path}")
        self.code = E_LOCK
        self.path = str(path)


class FileLock:
    """Non-blocking cross-process lock.

    On Windows the one-byte lock uses ``msvcrt`` as required.  On non-Windows
    development/CI hosts, ``fcntl.flock`` is used so the same behaviour can be
    exercised in Linux containers/WSL.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fd: Optional[int] = None

    def acquire(self) -> None:
        if self._fd is not None:
            return
        fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"0")
            os.lseek(fd, 0, os.SEEK_SET)
            if os.name == "nt":  # pragma: win32-no-cover - exercised on Windows
                import msvcrt

                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                except OSError as exc:
                    raise LockConflict(self.path) from exc
            else:
                import fcntl

                os.lseek(fd, 0, os.SEEK_SET)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise LockConflict(self.path) from exc
        except Exception:
            os.close(fd)
            raise
        self._fd = fd

    def release(self) -> None:
        if self._fd is None:
            return
        fd = self._fd
        try:
            if os.name == "nt":  # pragma: win32-no-cover
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
            self._fd = None

    def __enter__(self) -> "FileLock":
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: Optional[type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> None:
        self.release()
