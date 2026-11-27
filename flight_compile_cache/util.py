"""Small deterministic utilities shared by the generator and cache."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any


def canonical_json(value: Any) -> str:
    """Return a compact, key-sorted JSON representation.

    ``sort_keys`` plus no insignificant whitespace is important because this
    string feeds every compile key and artifact identifier.
    """

    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(value: Any, *, length: int | None = None) -> str:
    raw = canonical_json(value) if not isinstance(value, str) else value
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return digest if length is None else digest[:length]


def deep_copy(value: Any) -> Any:
    # JSON values are used everywhere; this is faster and stricter than copy.
    return json.loads(json.dumps(value))


def json_file_fingerprint(path: str | os.PathLike[str]) -> str:
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def atomic_write_json(path: str | os.PathLike[str], value: Any) -> None:
    """Write a JSON file with a whole-file replacement.

    A temporary file is created in the destination directory, flushed and
    fsynced, then committed with :func:`os.replace`.  A reader therefore sees
    either the old snapshot or the new snapshot, never a partially written one.
    """

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=str(destination.parent)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        try:
            directory_fd = os.open(destination.parent, os.O_RDONLY)
        except OSError:
            directory_fd = -1
        if directory_fd >= 0:
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def read_json(path: str | os.PathLike[str]) -> Any:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def stable_float(value: float, digits: int = 10) -> float:
    rounded = round(float(value), digits)
    return 0.0 if rounded == 0 else rounded
