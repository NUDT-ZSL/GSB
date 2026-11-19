"""Cross-process file lock (Windows msvcrt / POSIX fcntl)."""

from __future__ import annotations

import os
import sys


class FileLock:
    """Advisory whole-file lock guarding multi-process commit sections."""

    def __init__(self, path) -> None:
        self.path = str(path)
        self._fh = None

    def __enter__(self) -> "FileLock":
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self._fh = open(self.path, "a+b")
        if sys.platform.startswith("win"):
            import msvcrt
            self._fh.seek(0)
            msvcrt.locking(self._fh.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc) -> None:
        if self._fh is None:
            return
        if sys.platform.startswith("win"):
            import msvcrt
            self._fh.seek(0)
            msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        self._fh.close()
        self._fh = None
