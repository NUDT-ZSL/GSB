"""Deterministic primitives shared by every pipeline stage.

No randomness lives here.  All content fingerprints are SHA-256 hashes over
canonical JSON (sorted keys, compact separators) so identical inputs always
produce identical hashes on every machine and every run.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from typing import Any

HASH_PREFIX_LEN = 16


def canonical_json(obj: Any) -> str:
    """Serialize *obj* deterministically (sorted keys, no whitespace)."""
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False, default=_json_default,
    )


def _json_default(value: Any) -> Any:
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    if hasattr(value, "item"):
        return value.item()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def content_hash(obj: Any) -> str:
    """Return a stable SHA-256 fingerprint for any JSON-able object."""
    payload = canonical_json(obj).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def short_hash(obj: Any) -> str:
    return content_hash(obj)[:HASH_PREFIX_LEN]


def array_fingerprint(array: "np.ndarray", decimals: int = 8) -> str:
    """Fast stable fingerprint for a numeric numpy array.

    Rounds to *decimals* then hashes the raw little-endian bytes together with
    shape and dtype metadata.  Avoids expensive per-element JSON conversion.
    """
    import numpy as np

    rounded = np.round(np.asarray(array, dtype=np.float64), decimals)
    payload = rounded.astype("<f8", copy=False).tobytes()
    digest = hashlib.sha256()
    digest.update(str(rounded.shape).encode("utf-8"))
    digest.update(b"|")
    digest.update(payload)
    return digest.hexdigest()[:HASH_PREFIX_LEN]


def derived_key(upstream_fingerprints: list[str], params_fingerprint: str) -> str:
    """Derive a stage cache key from upstream content and parameter fingerprints."""
    material = {
        "upstream": sorted(upstream_fingerprints),
        "params": params_fingerprint,
    }
    return short_hash(material)


def atomic_write_text(path: str, text: str) -> None:
    """Write *text* to *path* via a temp file + atomic ``os.replace``.

    Works on POSIX and Windows (``os.replace`` is atomic on both).  The temp
    file is always created in the destination directory so the rename never
    crosses a filesystem boundary.
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=".tmp-" + os.path.basename(path) + "-", suffix=".part", dir=directory,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        _silent_unlink(tmp_path)
        raise


def atomic_write_json(path: str, obj: Any) -> None:
    atomic_write_text(path, canonical_json(obj))


def read_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _silent_unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass
