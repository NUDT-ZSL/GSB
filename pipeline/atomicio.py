"""Temporary-file plus atomic-replace IO helpers.

Every durable write goes through :func:`atomic_write_json` /
:func:`atomic_write_text`: the payload is fully flushed and fsynced to a
sibling temporary file, then ``os.replace`` performs the atomic rename. A
crash or ``SIGKILL`` can therefore leave either the previous complete file
or the new complete file, never a torn file.
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any

from .hashing import canonical_json


def _atomic_write(path: str, payload: bytes) -> None:
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp-", suffix=".part", dir=directory)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        # fsync the directory entry so the rename itself is durable.
        dir_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise


def atomic_write_json(path: str, obj: Any) -> None:
    _atomic_write(path, canonical_json(obj).encode("utf-8"))


def atomic_write_text(path: str, text: str) -> None:
    _atomic_write(path, text.encode("utf-8"))
