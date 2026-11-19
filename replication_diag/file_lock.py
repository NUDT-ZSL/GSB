"""Cross-platform exclusive file lock for state writers."""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .errors import DiagnosticsError


def _lock_fd(handle, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except (BlockingIOError, OSError):
            if time.monotonic() >= deadline:
                raise DiagnosticsError(
                    "E_LOCK",
                    message="timed out waiting for the state lock",
                    details={"timeout_seconds": timeout},
                )
            time.sleep(0.05)


def _unlock_fd(handle) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


@contextmanager
def exclusive_file_lock(path: Path, timeout: float = 30.0) -> Iterator[None]:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise DiagnosticsError(
            "E_LOCK",
            message=f"cannot create lock directory {path.parent}",
            location=str(path.parent),
            details={"reason": str(exc)},
        ) from exc
    try:
        handle = path.open("a+b")
    except OSError as exc:
        raise DiagnosticsError(
            "E_LOCK",
            message=f"cannot open lock file {path}",
            location=str(path),
            details={"reason": str(exc)},
        ) from exc
    try:
        if os.name == "nt" and path.stat().st_size == 0:
            handle.write(b"\0")
            handle.flush()
        _lock_fd(handle, timeout)
        try:
            yield
        finally:
            _unlock_fd(handle)
    finally:
        handle.close()
