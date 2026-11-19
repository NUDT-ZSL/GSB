"""Atomic local persistence and crash-recovery helpers."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def atomic_write_bytes(path: Path, payload: bytes) -> None:
    """Write a complete file with fsync + atomic replace.

    A killed process can leave a temporary file behind, but never a partially
    written active file.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    tmp_path = Path(raw_tmp)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        try:
            dir_fd = os.open(path.parent, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


def atomic_write_json(path: Path, value: Any) -> None:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ).encode("utf-8")
    atomic_write_bytes(path, payload + b"\n")


def cleanup_stale_temps(directory: Path) -> list[str]:
    """Remove temporary files left by a forcibly terminated writer."""

    directory.mkdir(parents=True, exist_ok=True)
    removed: list[str] = []
    for path in sorted(directory.glob(".*.tmp")):
        if path.is_file():
            path.unlink(missing_ok=True)
            removed.append(path.name)
    return removed


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)

