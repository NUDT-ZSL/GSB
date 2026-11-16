"""Atomic JSON persistence used by snapshots, caches and reports."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def atomic_write_json(path: str | Path, payload: Any, *, indent: int | None = 2) -> None:
    """Write JSON through a same-directory temporary file and ``os.replace``.

    The file is flushed and fsynced before replacement, and the containing
    directory is fsynced afterwards where the platform supports it.  A process
    killed during the write therefore leaves either the old complete file or
    the new complete file, never a partially replaced destination.
    """

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd = -1
    temporary_name = ""
    try:
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=str(destination.parent),
        )
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            if indent is None:
                handle.write(canonical_json(payload))
            else:
                json.dump(
                    payload,
                    handle,
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=indent,
                    allow_nan=False,
                )
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, destination)
        temporary_name = ""
        try:
            directory_fd = os.open(destination.parent, os.O_RDONLY)
        except OSError:
            directory_fd = -1
        if directory_fd >= 0:
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if fd >= 0:
            os.close(fd)
        if temporary_name:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


def read_json(path: str | Path) -> Any:
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)
