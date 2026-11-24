from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


class LockError(RuntimeError):
    code = "E_LOCK"

    def __init__(self, message: str = "workspace is locked by another process"):
        super().__init__(message)


class WorkspaceLock:
    """Cross-platform non-blocking workspace writer lock.

    POSIX uses flock(2); Windows uses msvcrt.locking on a one-byte region.
    The lock file is intentionally retained after release so that stale empty
    files do not become a lock-state source.
    """

    def __init__(self, workspace: str | Path, filename: str = ".auditmask.lock"):
        self.workspace = Path(workspace)
        self.filename = filename
        self.path = self.workspace / filename
        self._handle = None
        self._locked = False

    def acquire(self) -> "WorkspaceLock":
        self.workspace.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError as exc:
                    raise LockError() from exc
            else:
                import fcntl

                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except (BlockingIOError, OSError) as exc:
                    raise LockError() from exc
            self._handle = handle
            self._locked = True
            return self
        except Exception:
            handle.close()
            raise

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            if self._locked:
                if os.name == "nt":
                    import msvcrt

                    try:
                        self._handle.seek(0)
                        msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
                    except OSError:
                        pass
                else:
                    import fcntl

                    try:
                        fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
                    except OSError:
                        pass
        finally:
            self._handle.close()
            self._handle = None
            self._locked = False

    def __enter__(self) -> "WorkspaceLock":
        return self.acquire()

    def __exit__(self, exc_type, exc, tb) -> Optional[bool]:
        self.release()
        return None
