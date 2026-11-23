"""Portable process-level lock used to serialize scheduler leaders."""

from __future__ import annotations

import os
from pathlib import Path
from typing import IO

from .errors import EngineError


class ProcessLock:
    """Advisory lock backed by the host OS and released on process exit."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.handle: IO[str] | None = None

    def __enter__(self) -> "ProcessLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+", encoding="utf-8")
        self.handle.seek(0, os.SEEK_END)
        if self.handle.tell() == 0:
            self.handle.write("\0")
            self.handle.flush()
        try:
            try:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
            except ImportError:
                import msvcrt

                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_LOCK, 1)
        except OSError as exc:
            self.handle.close()
            self.handle = None
            raise EngineError(
                "ERR_LOCK",
                "cannot acquire state lock",
                {"path": str(self.path), "error": str(exc)},
            ) from exc
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self.handle is None:
            return
        try:
            try:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            except ImportError:
                import msvcrt

                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            self.handle.close()
            self.handle = None
