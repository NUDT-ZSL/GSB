"""Cross-platform file locking and atomic whole-file replacement."""

from __future__ import annotations

import contextlib
import os
import time
from pathlib import Path
from typing import Iterator, Optional

from .budget import LockConflict


def _try_lock(fd: int) -> bool:
    if os.name == "nt":  # pragma: no cover - exercised on Windows CI/dev box
        import msvcrt

        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except (BlockingIOError, OSError):
        return False


def _unlock(fd: int) -> None:
    if os.name == "nt":  # pragma: no cover
        import msvcrt

        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        return
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


@contextlib.contextmanager
def file_lock(
    path: str | os.PathLike[str], timeout_ms: int = 0, purpose: str = "output"
) -> Iterator[None]:
    """Acquire an exclusive advisory lock on ``path``.

    ``timeout_ms == 0`` means a single non-blocking attempt.  A conflict raises
    :class:`LockConflict` with business code ``E_LOCK`` instead of silently
    overwriting output produced by another process.
    """

    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")
            os.fsync(fd)
        deadline = time.perf_counter() + max(0, timeout_ms) / 1000.0
        while True:
            if _try_lock(fd):
                break
            if timeout_ms <= 0 or time.perf_counter() >= deadline:
                raise LockConflict(
                    "output lock is held by another writer",
                    lock_file=str(lock_path),
                    purpose=purpose,
                    timeout_ms=timeout_ms,
                )
            time.sleep(0.01)
        try:
            yield
        finally:
            _unlock(fd)
    finally:
        os.close(fd)


def atomic_write_bytes(
    path: str | os.PathLike[str],
    data: bytes,
    *,
    lock_path: Optional[str | os.PathLike[str]] = None,
    lock_timeout_ms: int = 0,
    purpose: str = "write",
) -> None:
    """Write ``data`` to a temp file, fsync it, then atomically replace ``path``."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_target = Path(lock_path) if lock_path is not None else target.parent / ".tlsdiag.lock"
    with file_lock(lock_target, timeout_ms=lock_timeout_ms, purpose=purpose):
        _atomic_write_bytes_locked(target, data)


def _atomic_write_bytes_locked(target: Path, data: bytes) -> None:
    temp = target.parent / (
        f".{target.name}.{os.getpid()}.{time.time_ns()}.tmp"
    )
    try:
        with open(temp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, target)
        if os.name != "nt":
            try:
                dir_fd = os.open(target.parent, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:
                pass
    finally:
        if temp.exists():
            try:
                temp.unlink()
            except OSError:
                pass


def atomic_write_text(
    path: str | os.PathLike[str],
    text: str,
    *,
    lock_path: Optional[str | os.PathLike[str]] = None,
    lock_timeout_ms: int = 0,
    purpose: str = "write",
) -> None:
    atomic_write_bytes(
        path,
        text.encode("utf-8"),
        lock_path=lock_path,
        lock_timeout_ms=lock_timeout_ms,
        purpose=purpose,
    )


def atomic_write_many(
    items: dict[str, bytes],
    *,
    lock_path: str | os.PathLike[str],
    lock_timeout_ms: int = 0,
    purpose: str = "write",
) -> None:
    """Atomically replace several files while holding one output lock.

    Tempt files are fully written and fsynced first; only then are the target
    paths replaced.  A crash can leave temp files but never a partially written
    report or checkpoint.
    """

    targets = [(Path(path), data) for path, data in items.items()]
    for target, _data in targets:
        target.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(lock_path, timeout_ms=lock_timeout_ms, purpose=purpose):
        _atomic_write_many_locked(targets)


def _atomic_write_many_locked(targets: list[tuple[Path, bytes]]) -> None:
    for target, data in targets:
        _atomic_write_bytes_locked(target, data)


def atomic_write_many_locked(items: dict[str, bytes]) -> None:
    """Write/replace several files while the caller already holds the lock."""

    targets = [(Path(path), data) for path, data in items.items()]
    for target, _data in targets:
        target.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_many_locked(targets)


def cleanup_stale_temp_files(directory: str | os.PathLike[str]) -> list[str]:
    """Remove stale ``.*.tmp`` files left by a killed process."""

    base = Path(directory)
    removed: list[str] = []
    if not base.exists():
        return removed
    for candidate in base.glob(".*.tmp"):
        try:
            candidate.unlink()
            removed.append(str(candidate))
        except OSError:
            continue
    return removed
