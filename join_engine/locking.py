from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


def partition_lock_path(
    lock_root: Path,
    query_id: str,
    version: int,
    kind: str,
    partition_id: int,
    *,
    side: str | None = None,
) -> Path:
    safe_query = "".join(
        character if character.isalnum() or character in {"-", "_", "."} else "_"
        for character in query_id
    )
    parts = [lock_root, safe_query, f"v{version}", kind]
    if side is not None:
        parts.append(side)
    parts.append(f"part-{partition_id:05d}.lock")
    return Path(*map(str, parts))


@contextmanager
def exclusive_file_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def lock_is_held(path: Path) -> bool:
    if not path.exists():
        return False
    descriptor = os.open(str(path), os.O_RDWR)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return False
    finally:
        os.close(descriptor)

