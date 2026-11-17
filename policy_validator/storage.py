"""Atomic snapshot writes and cross-platform file locking (requirement 7).

Every snapshot (checkpoint / report / summary) is written to a temporary
file, validated, then atomically renamed over the destination. Writers in
the same working directory are serialized with an advisory lock file;
contention raises LockConflict which surfaces as business code E5001.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable

from . import errors


class LockConflict(Exception):
    code = errors.E_LOCK_CONFLICT


@contextlib.contextmanager
def file_lock(lock_path: Path):
    """Advisory exclusive lock via O_CREAT|O_EXCL (atomic on POSIX & Windows)."""
    lock_path = Path(lock_path)
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise LockConflict(f"lock held: {lock_path}") from exc
    try:
        os.write(fd, str(os.getpid()).encode())
        yield
    finally:
        os.close(fd)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(lock_path)


def atomic_write_json(path: Path, payload: Any, required_keys: Iterable[str] = ()) -> None:
    """Serialize, validate, then atomically replace ``path``.

    Validation happens before the rename: the payload must round-trip as
    JSON and contain every required top-level key. The destination never
    observes a partially written file.
    """
    path = Path(path)
    data = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
    decoded = json.loads(data.decode("utf-8"))  # integrity check pre-rename
    missing = [k for k in required_keys if k not in decoded]
    if missing:
        raise ValueError(f"payload missing required keys: {missing}")

    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp",
                                    dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)  # atomic on POSIX and Windows (same dir)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)
        raise


def locked_atomic_write_json(path: Path, payload: Any,
                             required_keys: Iterable[str] = ()) -> None:
    """Atomic write serialized against other writers of the same directory."""
    path = Path(path)
    with file_lock(path.parent / (path.name + ".lock")):
        atomic_write_json(path, payload, required_keys)
