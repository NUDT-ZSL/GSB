from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

import numpy as np


DIGEST_PREFIX = "sha256:"


def canonical_json(obj: Any) -> bytes:
    """Return deterministic JSON bytes.

    Tuples are converted to arrays, numpy scalars/arrays are converted to
    JSON-compatible values.  ``allow_nan=False`` keeps non-finite values from
    producing non-reproducible artifacts.
    """

    def default(value: Any) -> Any:
        if isinstance(value, np.ndarray):
            return value.tolist()
        if isinstance(value, np.generic):
            return value.item()
        if isinstance(value, Path):
            return str(value)
        if isinstance(value, (set, frozenset)):
            return sorted(value)
        raise TypeError(f"Object of type {type(value)!r} is not JSON serializable")

    text = json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
        default=default,
    )
    return text.encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return DIGEST_PREFIX + hashlib.sha256(data).hexdigest()


def sha256_file(path: os.PathLike[str] | str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return DIGEST_PREFIX + digest.hexdigest()


def canonical_digest(obj: Any) -> str:
    return sha256_bytes(canonical_json(obj))


def array_digest(array: np.ndarray) -> str:
    """Digest shape, dtype and raw C-order payload."""

    digest = hashlib.sha256()
    digest.update(str(array.dtype).encode("utf-8"))
    digest.update(b"\0")
    digest.update(json.dumps(list(array.shape)).encode("ascii"))
    digest.update(b"\0")
    digest.update(np.ascontiguousarray(array).tobytes(order="C"))
    return DIGEST_PREFIX + digest.hexdigest()


def is_digest(value: Any) -> bool:
    return (
        isinstance(value, str)
        and value.startswith(DIGEST_PREFIX)
        and len(value) == len(DIGEST_PREFIX) + 64
        and all(c in "0123456789abcdef" for c in value[len(DIGEST_PREFIX) :])
    )


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        _fsync_directory(path.parent)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def _fsync_directory(directory: Path) -> None:
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def digest_object_path(objects_dir: Path, digest: str) -> Path:
    bare = digest.removeprefix(DIGEST_PREFIX)
    return objects_dir / bare[:2] / f"{bare}.json"
