"""Cross-process advisory lock used in front of SQLite write transactions."""

from __future__ import annotations

import os
from pathlib import Path
from typing import BinaryIO


class FileLock:
    """A small blocking exclusive file lock.

    POSIX uses ``flock``; Windows uses ``msvcrt.locking``.  The lock is
    advisory and must be acquired by every writer.  SQLite still provides the
    authoritative transactional guarantee.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._file: BinaryIO | None = None

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(self.path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt

                self._file.seek(0)
                if os.fstat(self._file.fileno()).st_size == 0:
                    self._file.write(b"\0")
                    self._file.flush()
                self._file.seek(0)
                msvcrt.locking(self._file.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(self._file.fileno(), fcntl.LOCK_EX)
        except BaseException:
            self._file.close()
            self._file = None
            raise
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
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
