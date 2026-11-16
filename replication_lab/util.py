"""Small, dependency-free filesystem and canonicalization helpers."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def stable_json(value: Any, *, indent: int | None = 2) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        indent=indent,
        separators=(",", ":") if indent is None else None,
    )


def fsync_directory(path: Path) -> None:
    """Best-effort directory fsync; Windows does not support this operation."""

    try:
        descriptor = os.open(path, os.O_RDONLY)
    except (OSError, AttributeError):
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def atomic_write_bytes(path: Path, data: bytes, *, mode: int | None = None) -> None:
    """Write bytes through a same-directory temporary file and atomically replace.

    A forced termination can leave a temporary file behind, but it can never
    leave a partially written file under the destination name.
    """

    ensure_dir(path.parent)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.tmp-", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(temporary, mode)
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_bytes(path, stable_json(value).encode("utf-8") + b"\n")


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def mark_read_only(path: Path) -> None:
    try:
        os.chmod(path, 0o444)
    except OSError:
        pass


def sorted_ints(values: Iterable[int]) -> list[int]:
    return sorted(set(int(value) for value in values))

