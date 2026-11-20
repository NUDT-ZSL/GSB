from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .errors import error


@contextmanager
def file_lock(path: Path, timeout_s: float = 30.0) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    acquired = False
    deadline = time.monotonic() + timeout_s
    try:
        if os.name == "posix":
            import fcntl

            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise error(
                            "E_LOCK_TIMEOUT",
                            "timed out waiting for task file lock",
                            path=str(path),
                        )
                    time.sleep(0.02)
        else:
            import msvcrt

            while True:
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    acquired = True
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise error(
                            "E_LOCK_TIMEOUT",
                            "timed out waiting for task file lock",
                            path=str(path),
                        )
                    time.sleep(0.02)
        yield
    finally:
        if acquired:
            try:
                if os.name == "posix":
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                else:
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        handle.close()

