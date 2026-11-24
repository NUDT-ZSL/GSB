from __future__ import annotations

import os
from pathlib import Path
from types import TracebackType
from typing import Optional, Type

from .codes import E_LOCK


class LockConflict(RuntimeError):
    def __init__(self, path: Path):
        super().__init__(f"workdir lock is held: {path}")
        self.code = E_LOCK
        self.path = str(path)


class WorkbenchLock:
    """Cross-platform non-blocking exclusive workdir lock."""

    def __init__(self, workdir: os.PathLike[str] | str, name: str = "workbench.lock"):
        self.workdir = Path(workdir)
        self.path = self.workdir / name
        self.fd: Optional[int] = None

    def acquire(self) -> "WorkbenchLock":
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            os.lseek(self.fd, 0, os.SEEK_SET)
            try:
                os.write(self.fd, b"0")
            except OSError:
                pass
            os.lseek(self.fd, 0, os.SEEK_SET)
            if os.name == "nt":  # pragma: no cover - exercised on Windows
                import msvcrt

                try:
                    msvcrt.locking(self.fd, msvcrt.LK_NBLCK, 1)
                except OSError as exc:
                    raise LockConflict(self.path) from exc
            else:
                import fcntl

                try:
                    fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    raise LockConflict(self.path) from exc
            os.ftruncate(self.fd, 0)
            os.write(self.fd, str(os.getpid()).encode("ascii"))
            os.fsync(self.fd)
        except Exception:
            if self.fd is not None:
                os.close(self.fd)
                self.fd = None
            raise
        return self

    def release(self) -> None:
        if self.fd is None:
            return
        try:
            if os.name == "nt":  # pragma: no cover
                import msvcrt

                os.lseek(self.fd, 0, os.SEEK_SET)
                try:
                    msvcrt.locking(self.fd, msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            else:
                import fcntl

                fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd)
            self.fd = None

    def __enter__(self) -> "WorkbenchLock":
        return self.acquire()

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> None:
        self.release()
