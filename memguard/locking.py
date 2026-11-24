"""Cross-process exclusive file locking.

The runtime target is Windows, while CI/local verification may run on Linux.
``msvcrt`` is imported only on Windows and ``fcntl`` only on POSIX.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from .constants import LockConflict


class ExclusiveFileLock:
    def __init__(self, path: os.PathLike | str):
        self.path = Path(path)
        self._fd: Optional[int] = None

    def acquire(self) -> "ExclusiveFileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if os.name == "nt":  # pragma: no cover - exercised on Windows
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                except OSError as exc:
                    raise LockConflict(
                        "workspace is locked by another memguard writer",
                        details={"lock_path": str(self.path)},
                    ) from exc
            else:
                import fcntl

                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise LockConflict(
                        "workspace is locked by another memguard writer",
                        details={"lock_path": str(self.path)},
                    ) from exc
        except Exception:
            os.close(fd)
            raise

        self._fd = fd
        try:
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, f"pid={os.getpid()}\n".encode("ascii"))
            os.fsync(fd)
        except OSError:
            # Locking still works if diagnostic owner metadata cannot be set.
            pass
        return self

    def release(self) -> None:
        if self._fd is None:
            return
        fd = self._fd
        self._fd = None
        try:
            if os.name == "nt":  # pragma: no cover
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self) -> "ExclusiveFileLock":
        return self.acquire()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()
