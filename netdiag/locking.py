from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

from .errors import BusinessError


class TargetFileLock:
    """Cross-platform OS file lock scoped to one target.

    The lock file itself may persist, but the OS lock is released when the
    process exits, including after a forced termination.
    """

    def __init__(
        self,
        lock_dir: Path,
        target_name: str,
        *,
        timeout_s: float = 20.0,
        poll_s: float = 0.02,
    ) -> None:
        self.lock_dir = lock_dir
        self.target_name = target_name
        self.timeout_s = timeout_s
        self.poll_s = poll_s
        self._file = None
        self._path = lock_dir / (
            hashlib.sha256(target_name.encode("utf-8")).hexdigest() + ".lock"
        )

    def __enter__(self) -> "TargetFileLock":
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        self._file = open(self._path, "a+b")
        if self._file.tell() == 0:
            try:
                self._file.write(b"\0")
                self._file.flush()
            except OSError:
                pass
        self._file.seek(0)
        deadline = time.monotonic() + self.timeout_s
        while True:
            try:
                self._try_lock()
                return self
            except OSError:
                if time.monotonic() >= deadline:
                    self._file.close()
                    self._file = None
                    raise BusinessError(
                        "TARGET_LOCK_TIMEOUT",
                        "timed out waiting for the target file lock",
                        {
                            "target": self.target_name,
                            "lock_path": str(self._path),
                            "timeout_s": self.timeout_s,
                        },
                    )
                time.sleep(self.poll_s)

    def _try_lock(self) -> None:
        if os.name == "nt":
            import msvcrt

            self._file.seek(0)
            msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def __exit__(self, *_args: object) -> None:
        if self._file is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._file.seek(0)
                msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        finally:
            self._file.close()
            self._file = None
