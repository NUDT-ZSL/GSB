from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def atomic_write_bytes(path: os.PathLike[str] | str, data: bytes) -> Path:
    """Write bytes in the destination directory and atomically replace target."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(target.parent),
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        try:
            directory_fd = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            # Directory fsync is unavailable on some Windows/filesystem setups.
            pass
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return target


def atomic_write_json(
    path: os.PathLike[str] | str,
    value: Any,
    *,
    indent: int = 2,
    sort_keys: bool = True,
) -> Path:
    data = (
        json.dumps(
            value,
            ensure_ascii=False,
            indent=indent,
            sort_keys=sort_keys,
            separators=(",", ": ") if indent else (",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    return atomic_write_bytes(path, data)
