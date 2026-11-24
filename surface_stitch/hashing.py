"""Canonical JSON and binary digest helpers."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Iterable

import numpy as np


def canonical_json_bytes(value: Any) -> bytes:
    """Return deterministic compact JSON bytes.

    ``allow_nan=False`` ensures invalid non-finite values cannot silently
    participate in a version identifier.
    """

    text = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return text.encode("utf-8")


def digest_canonical(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def array_digest(arrays: Dict[str, np.ndarray]) -> str:
    """Hash named arrays using stable little-endian float64/int64 bytes."""

    digest = hashlib.sha256()
    for name in sorted(arrays):
        array = np.ascontiguousarray(arrays[name])
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(array.shape).encode("ascii"))
        digest.update(b"\0")
        digest.update(str(array.dtype.str).encode("ascii"))
        digest.update(b"\0")
        if np.issubdtype(array.dtype, np.floating):
            canonical = array.astype("<f8", copy=False)
        elif np.issubdtype(array.dtype, np.integer):
            canonical = array.astype("<i8", copy=False)
        else:
            canonical = array
        digest.update(canonical.tobytes(order="C"))
    return digest.hexdigest()


def combine_version(parts: Dict[str, Any]) -> str:
    return digest_canonical(parts)


def iter_deterministic_sqlite_rows(connection: Any) -> Iterable[Dict[str, Any]]:
    """Dump all user tables as sorted JSON-safe rows."""

    cursor = connection.execute(
        "SELECT name, sql FROM sqlite_master "
        "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )
    tables = cursor.fetchall()
    for table_name, create_sql in tables:
        yield {"table": "__schema__", "name": table_name, "sql": create_sql}
        columns = [
            row[1]
            for row in connection.execute(
                f'PRAGMA table_info("{table_name}")'
            ).fetchall()
        ]
        rows = connection.execute(
            f'SELECT * FROM "{table_name}" ORDER BY '
            + ", ".join(f'"{column}"' for column in columns)
        ).fetchall()
        for row in rows:
            yield {
                "table": table_name,
                "row": {column: row[index] for index, column in enumerate(columns)},
            }
