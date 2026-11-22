"""Cross-platform advisory file locking used next to SQLite WAL."""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .errors import ErrorCode, business_error

try:  # pragma: no cover - platform-specific
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

try:  # pragma: no cover - platform-specific
    import msvcrt
except ImportError:  # pragma: no cover
    msvcrt = None


def lock_path_for(db_path: str | Path) -> Path:
    return Path(str(db_path) + ".lock")


@contextmanager
def file_lock(db_path: str | Path, timeout_seconds: float = 15.0) -> Iterator[None]:
    path = lock_path_for(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    if msvcrt is not None and handle.tell() == 0:  # pragma: no cover
        handle.write(b"\0")
        handle.flush()
    deadline = time.monotonic() + timeout_seconds
    acquired = False
    try:
        while True:
            try:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                elif msvcrt is not None:  # pragma: no cover
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:  # pragma: no cover
                    raise OSError("no supported file locking primitive")
                acquired = True
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise business_error(
                        ErrorCode.STORE_LOCK,
                        f"timed out waiting for lock {path}",
                        path=str(path),
                        reason=str(exc),
                    ) from exc
                time.sleep(0.02)
        yield
    finally:
        if acquired:
            try:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                elif msvcrt is not None:  # pragma: no cover
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        handle.close()


def remove_lock_file_if_unlocked(db_path: str | Path) -> None:
    """Best-effort cleanup for test harnesses that explicitly reset a DB."""

    path = lock_path_for(db_path)
    if not path.exists():
        return
    try:
        with file_lock(db_path, timeout_seconds=0.05):
            path.unlink(missing_ok=True)
    except Exception:
        return


def process_lock_owner_pid(db_path: str | Path) -> int | None:
    """Linux-only diagnostic helper; returns None when no lock is held."""

    path = lock_path_for(db_path)
    if not path.exists():
        return None
    try:
        with file_lock(db_path, timeout_seconds=0.01):
            return None
    except Exception:
        for proc in Path("/proc").iterdir():
            if not proc.name.isdigit():
                continue
            try:
                for fd in (proc / "fd").iterdir():
                    if os.path.realpath(fd) == str(path.resolve()):
                        return int(proc.name)
            except OSError:
                continue
    return None
