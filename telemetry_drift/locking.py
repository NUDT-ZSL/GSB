"""Process-wide advisory lock used by every atomic commit path."""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
from types import TracebackType

from .errors import DriftError, E_STAGE_BUSY


def resolve_commit_lock(path: str | Path | None = None) -> Path:
    """Resolve the shared commit lock for stages and snapshot writers."""
    if path is not None:
        return Path(path)
    configured = os.environ.get("TELEMETRY_DRIFT_COMMIT_LOCK")
    if configured:
        return Path(configured)
    return Path.cwd() / ".telemetry-drift.commit.lock"


class FileCommitLock:
    """A non-blocking exclusive lock shared by stages and snapshot commits."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._handle = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise DriftError(
                E_STAGE_BUSY,
                "another stage or predictor snapshot commit is active",
                lock_path=str(self.path),
            ) from exc
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n".encode("ascii"))
        handle.flush()
        os.fsync(handle.fileno())
        self._handle = handle

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None

    def __enter__(self) -> "FileCommitLock":
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()

