"""Small cross-process advisory lock helper."""

from __future__ import annotations

import contextlib
import fcntl
import os
from pathlib import Path
from typing import Iterator


@contextlib.contextmanager
def file_lock(path: str | os.PathLike[str]) -> Iterator[None]:
    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
