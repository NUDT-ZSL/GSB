from __future__ import annotations

import os
from pathlib import Path
from types import TracebackType
from typing import BinaryIO

from .errors import BlockStoreError, ErrorCode


class InterProcessLock:
    """Exclusive cross-platform lock backed by a single lock file."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._handle: BinaryIO | None = None

    def __enter__(self) -> "InterProcessLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._handle = self.path.open("a+b")
            if os.name == "nt":
                import msvcrt

                self._handle.seek(0)
                if self._handle.read(1) == b"":
                    self._handle.write(b"\0")
                    self._handle.flush()
                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX)
            return self
        except OSError as exc:
            if self._handle is not None:
                self._handle.close()
                self._handle = None
            raise BlockStoreError(
                ErrorCode.LOCK_FAILED,
                "unable to acquire inter-process lock",
                {"path": str(self.path), "reason": str(exc)},
            ) from exc

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
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
