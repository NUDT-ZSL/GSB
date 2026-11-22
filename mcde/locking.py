from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

from .errors import E_LOCK_TIMEOUT, StateError


class FileLock:
    """Cross-platform advisory byte-range lock held by an OS file handle."""

    def __init__(self, path: Path, timeout_s: float = 30.0) -> None:
        self.path = path
        self.timeout_s = timeout_s
        self._handle = None
        self._is_windows = os.name == "nt"

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = open(self.path, "a+b")
        if self._handle.tell() == 0:
            self._handle.write(b"\0")
            self._handle.flush()
        self._handle.seek(0)
        deadline = time.monotonic() + self.timeout_s
        while True:
            try:
                if self._is_windows:
                    import msvcrt

                    msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(
                        self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB
                    )
                return self
            except OSError as exc:
                if time.monotonic() >= deadline:
                    self._handle.close()
                    self._handle = None
                    raise StateError(
                        E_LOCK_TIMEOUT,
                        "timed out waiting for file lock",
                        path=str(self.path),
                        reason=str(exc),
                    ) from exc
                time.sleep(0.02)

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._handle is None:
            return
        try:
            if self._is_windows:
                import msvcrt

                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None


def cluster_lock_name(cluster_id: str) -> str:
    digest = hashlib.sha256(cluster_id.encode("utf-8")).hexdigest()[:20]
    return f"cluster-{digest}.lock"


def keyed_lock_name(prefix: str, key: str) -> str:
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
    return f"{prefix}-{digest}.lock"

