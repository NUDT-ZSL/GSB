"""Cross-platform workspace locking and atomic JSON replacement."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .errors import E_LOCK, KlpLockError


class FileLock:
    """Non-blocking exclusive lock backed by the local filesystem.

    POSIX uses ``flock``; Windows uses ``msvcrt.locking``. Lock contention is a
    business-level E_LOCK result rather than an indefinite wait, so competing
    writers fail deterministically instead of silently serializing forever.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._fd: int | None = None

    def acquire(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as exc:
            raise KlpLockError(E_LOCK, f"cannot open lock file {self.path}: {exc}") from exc

        try:
            if os.name == "nt":
                import msvcrt

                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"\0")
                    os.fsync(fd)
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            try:
                os.close(fd)
            except OSError:
                pass
            raise KlpLockError(E_LOCK, f"workspace lock is busy: {self.path}") from exc

        self._fd = fd
        return self

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(self._fd, 0, os.SEEK_SET)
                msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> "FileLock":
        return self.acquire()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write_bytes(path: str | Path, payload: bytes, *, lock_path: str | Path) -> None:
    """Write and replace a file atomically while holding the workspace lock."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(lock_path):
        temp_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=destination.parent,
                prefix=f".{destination.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temp_name = handle.name
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, destination)
            temp_name = None
            _fsync_directory(destination.parent)
        finally:
            if temp_name is not None:
                try:
                    os.unlink(temp_name)
                except OSError:
                    pass


def atomic_write_json(
    path: str | Path,
    document: object,
    *,
    lock_path: str | Path,
    indent: int = 2,
) -> None:
    payload = (
        json.dumps(
            document,
            ensure_ascii=False,
            indent=indent,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    atomic_write_bytes(path, payload, lock_path=lock_path)


def read_json(path: str | Path) -> object:
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)

