"""Atomic file-write helpers shared by checkpoint and result output."""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any, Dict

from .errors import WriteError


def canonical_json_bytes(payload: Dict[str, Any]) -> bytes:
    return (
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode("utf-8")


def atomic_write_bytes(path: str, data: bytes) -> None:
    """Write ``data`` via a same-directory temp file and ``os.replace``.

    Failures raise :class:`WriteError`; any pre-existing file at ``path`` is
    left untouched because the temp file is never moved into place.
    """
    directory = os.path.dirname(os.path.abspath(path))
    try:
        os.makedirs(directory, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            prefix=".tmp-" + os.path.basename(path) + "-", dir=directory
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, path)
        except BaseException:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise
    except WriteError:
        raise
    except OSError as exc:
        raise WriteError("failed to atomically write %s: %s" % (path, exc),
                         {"path": path})


def atomic_write_json(path: str, payload: Dict[str, Any]) -> None:
    atomic_write_bytes(path, canonical_json_bytes(payload))


def replace_if_different(path: str, data: bytes) -> bool:
    """Replace ``path`` atomically only when its current bytes differ.

    Returns ``True`` when the file was replaced, ``False`` when it already
    contained identical content.  Keeps the old file intact on write failure.
    """
    if os.path.exists(path):
        try:
            with open(path, "rb") as handle:
                if handle.read() == data:
                    return False
        except OSError as exc:
            raise WriteError("failed to read existing output %s: %s" % (path, exc),
                             {"path": path})
    atomic_write_bytes(path, data)
    return True
