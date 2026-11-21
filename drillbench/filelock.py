from __future__ import annotations

import os
from pathlib import Path
from types import TracebackType

from .errors import BusinessError


class NonBlockingFileLock:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._handle = None
        self._acquired = False

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        acquired = False
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0, 2)
                if handle.tell() == 0:
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError as exc:
            handle.close()
            raise BusinessError(
                "E_LOCKED",
                "resource is locked by another drill process",
                {"lock": str(self.path)},
            ) from exc
        self._handle = handle
        self._acquired = acquired

    def release(self) -> None:
        if not self._acquired or self._handle is None:
            return
        handle = self._handle
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            else:
                import fcntl

                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
        finally:
            handle.close()
            self._handle = None
            self._acquired = False

    def __enter__(self) -> "NonBlockingFileLock":
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()

