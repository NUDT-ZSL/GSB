from __future__ import annotations

import fcntl
import threading
from pathlib import Path
from types import TracebackType
from typing import Type

from .errors import BusinessError, ErrorCode


class FileLock:
    """A process-scoped fcntl lock plus an in-process thread lock."""

    _thread_locks: dict[str, threading.Lock] = {}
    _registry_lock = threading.Lock()

    def __init__(self, path: Path, *, blocking: bool = True) -> None:
        self.path = path
        self.blocking = blocking
        self._handle: object | None = None
        with self._registry_lock:
            self._thread_lock = self._thread_locks.setdefault(
                str(path.resolve()), threading.Lock()
            )
        self._thread_locked = False

    def __enter__(self) -> "FileLock":
        if not self.blocking:
            acquired = self._thread_lock.acquire(blocking=False)
        else:
            acquired = self._thread_lock.acquire()
        if not acquired:
            raise BusinessError(
                ErrorCode.RUN_BUSY,
                "another operation holds the in-process lock",
                details={"lock": str(self.path)},
            )
        self._thread_locked = True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        operation = fcntl.LOCK_EX
        if not self.blocking:
            operation |= fcntl.LOCK_NB
        try:
            fcntl.flock(handle.fileno(), operation)
        except BlockingIOError as exc:
            handle.close()
            self._thread_lock.release()
            self._thread_locked = False
            raise BusinessError(
                ErrorCode.RUN_BUSY,
                "another process holds the file lock",
                details={"lock": str(self.path)},
            ) from exc
        self._handle = handle
        return self

    def __exit__(
        self,
        exc_type: Type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        handle = self._handle
        self._handle = None
        if handle is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)  # type: ignore[attr-defined]
            finally:
                handle.close()  # type: ignore[attr-defined]
        if self._thread_locked:
            self._thread_locked = False
            self._thread_lock.release()
