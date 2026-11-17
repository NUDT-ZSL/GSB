"""Portable file locking and atomic JSON replacement."""

from __future__ import annotations

import errno
import json
import os
import tempfile
from pathlib import Path
from types import TracebackType
from typing import Any

from .codes import Code


class LockConflict(RuntimeError):
    def __init__(self, code: str, path: Path):
        super().__init__(f"{code}: {path}")
        self.code = code
        self.path = path


class FileLock:
    """Non-blocking cross-platform exclusive lock for a sidecar file."""

    def __init__(self, path: Path | str, *, conflict_code: str):
        self.path = Path(path)
        self.conflict_code = conflict_code
        self._file: Any = None

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(self.path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt

                self._file.seek(0, os.SEEK_END)
                if self._file.tell() == 0:
                    self._file.write(b"\0")
                    self._file.flush()
                self._file.seek(0)
                msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._file.close()
            self._file = None
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK) or os.name == "nt":
                raise LockConflict(self.conflict_code, self.path) from exc
            raise
        return self

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


def atomic_write_json_unlocked(path: Path | str, value: Any) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.tmp.",
        dir=str(destination.parent),
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(
                value,
                handle,
                ensure_ascii=False,
                allow_nan=False,
                indent=2,
                sort_keys=True,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        if os.name != "nt":
            try:
                directory_fd = os.open(destination.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def atomic_write_json(
    path: Path | str,
    value: Any,
    *,
    conflict_code: str = Code.OUTPUT_LOCK,
) -> None:
    destination = Path(path)
    lock_path = destination.with_name(destination.name + ".lock")
    with FileLock(lock_path, conflict_code=conflict_code):
        atomic_write_json_unlocked(destination, value)
