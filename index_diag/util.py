"""Canonical serialization, hashing, and atomic file helpers."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .errors import DiagnosticError, E_IO, E_SNAPSHOT_CORRUPT


def canonical_json(value: Any) -> str:
    """Return the byte-stable JSON representation used for every checksum."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_bytes(value: Any) -> bytes:
    return canonical_json(value).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_bytes(value))


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def atomic_write_bytes(path: str | os.PathLike[str], data: bytes) -> None:
    """Write and fsync a temporary sibling, then atomically replace the target."""

    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
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
                pass
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    except DiagnosticError:
        raise
    except OSError as exc:
        raise DiagnosticError(E_IO, f"cannot write {target}: {exc}") from exc


def atomic_write_json(path: str | os.PathLike[str], value: Any) -> None:
    atomic_write_bytes(path, canonical_bytes(value) + b"\n")


def read_json(path: str | os.PathLike[str]) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DiagnosticError(
            E_IO, f"cannot read JSON file {path}: {exc}", {"path": str(path)}
        ) from exc


def verify_checksum(
    document: dict[str, Any],
    checksum_field: str = "checksum",
    *,
    path: str | None = None,
) -> None:
    if not isinstance(document, dict):
        raise DiagnosticError(
            E_SNAPSHOT_CORRUPT,
            "snapshot is not a JSON object",
            {"path": path} if path else {},
        )
    body = {key: value for key, value in document.items() if key != checksum_field}
    actual = sha256_json(body)
    expected = document.get(checksum_field)
    if expected != actual:
        raise DiagnosticError(
            E_SNAPSHOT_CORRUPT,
            "snapshot checksum mismatch",
            {"path": path, "expected": expected, "actual": actual}
            if path
            else {"expected": expected, "actual": actual},
        )


def seal_checksum(body: dict[str, Any], checksum_field: str = "checksum") -> dict[str, Any]:
    sealed = dict(body)
    sealed[checksum_field] = sha256_json(body)
    return sealed
