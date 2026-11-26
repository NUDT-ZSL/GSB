"""Non-blocking in-process and cross-process commit locks."""

from __future__ import annotations

import fcntl
import os
import threading
from pathlib import Path
from types import TracebackType

from .errors import DiagnosticError, E_IO, E_STAGE_BUSY


_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


def _thread_lock(path: Path) -> threading.Lock:
    key = str(path.resolve())
    with _THREAD_LOCKS_GUARD:
        lock = _THREAD_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _THREAD_LOCKS[key] = lock
        return lock


class NonBlockingFileLock:
    """A lock failure is surfaced immediately as E_STAGE_BUSY."""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        self._thread_lock = _thread_lock(self.path)
        self._fd: int | None = None
        self._held_thread = False

    def __enter__(self) -> "NonBlockingFileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self._thread_lock.acquire(blocking=False):
            raise DiagnosticError(
                E_STAGE_BUSY,
                "another commit is already active",
                {"lock": str(self.path)},
            )
        self._held_thread = True
        try:
            self._fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._release()
            raise DiagnosticError(
                E_STAGE_BUSY,
                "another process is already committing",
                {"lock": str(self.path)},
            ) from exc
        except OSError as exc:
            self._release()
            raise DiagnosticError(
                E_IO,
                f"cannot acquire commit lock: {exc}",
                {"lock": str(self.path)},
            ) from exc
        return self

    def _release(self) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
        if self._held_thread:
            self._held_thread = False
            self._thread_lock.release()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._release()
