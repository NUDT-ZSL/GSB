"""Cross-platform advisory lock backed by an OS file lock."""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .errors import E_LOCK_TIMEOUT, LedgerError


@contextmanager
def exclusive_file_lock(
    lock_path: str | Path,
    timeout_seconds: float = 30.0,
) -> Iterator[None]:
    path = Path(lock_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if os.name == "nt":
            _lock_windows(descriptor, timeout_seconds)
        else:
            _lock_posix(descriptor, timeout_seconds)
        yield
    finally:
        try:
            if os.name == "nt":
                _unlock_windows(descriptor)
            else:
                _unlock_posix(descriptor)
        finally:
            os.close(descriptor)


def _lock_posix(descriptor: int, timeout_seconds: float) -> None:
    import fcntl

    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise LedgerError(
                    E_LOCK_TIMEOUT,
                    "timed out waiting for the ledger file lock",
                )
            time.sleep(0.01)


def _unlock_posix(descriptor: int) -> None:
    import fcntl

    try:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    except OSError:
        pass


def _lock_windows(descriptor: int, timeout_seconds: float) -> None:
    import msvcrt

    if os.fstat(descriptor).st_size == 0:
        os.write(descriptor, b"\0")
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            return
        except OSError:
            if time.monotonic() >= deadline:
                raise LedgerError(
                    E_LOCK_TIMEOUT,
                    "timed out waiting for the ledger file lock",
                )
            time.sleep(0.01)


def _unlock_windows(descriptor: int) -> None:
    import msvcrt

    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
    except OSError:
        pass

