"""Deterministic serialization, timestamps, and atomic file operations."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from .constants import VERSION_RE


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def pretty_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_value(value: Any) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def version_tuple(version: str) -> tuple[int, int, int] | None:
    match = VERSION_RE.fullmatch(version)
    if match is None:
        return None
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def normalize_timestamp(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc)
    return parsed.isoformat(timespec="microseconds").replace("+00:00", "Z")


def iter_json_array(raw: str) -> Iterator[tuple[int, int, Any]]:
    """Yield (index, character_offset, value) for a valid top-level JSON array."""

    decoder = json.JSONDecoder()
    length = len(raw)
    pos = 0
    while pos < length and raw[pos].isspace():
        pos += 1
    if pos >= length or raw[pos] != "[":
        raise ValueError("top-level JSON value is not an array")
    pos += 1
    index = 0
    while True:
        while pos < length and raw[pos].isspace():
            pos += 1
        if pos >= length:
            raise ValueError("unterminated JSON array")
        if raw[pos] == "]":
            pos += 1
            break
        start = pos
        value, end = decoder.raw_decode(raw, pos)
        yield index, start, value
        index += 1
        pos = end
        while pos < length and raw[pos].isspace():
            pos += 1
        if pos >= length:
            raise ValueError("unterminated JSON array")
        if raw[pos] == ",":
            pos += 1
            continue
        if raw[pos] == "]":
            pos += 1
            break
        raise ValueError(f"unexpected character at offset {pos}")
    while pos < length and raw[pos].isspace():
        pos += 1
    if pos != length:
        raise ValueError(f"trailing content at offset {pos}")


def parse_json_with_offset(raw: str) -> tuple[Any, int | None]:
    try:
        return json.loads(raw), None
    except json.JSONDecodeError as exc:
        return None, exc.pos


def atomic_write_bytes(
    path: Path,
    data: bytes,
    validator: Callable[[bytes], None] | None = None,
) -> None:
    """Write a complete file, validate it, then atomically replace the target."""

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        with temp_path.open("rb") as handle:
            observed = handle.read()
        if observed != data:
            raise OSError("temporary file verification failed")
        if validator is not None:
            validator(observed)
        os.replace(temp_path, path)
        _fsync_directory(path.parent)
    except Exception:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
        raise


def atomic_write_json(
    path: Path,
    value: Any,
    *,
    pretty: bool = True,
) -> None:
    data = pretty_json_bytes(value) if pretty else canonical_json_bytes(value)

    def validate(content: bytes) -> None:
        json.loads(content.decode("utf-8"))

    atomic_write_bytes(path, data, validate)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        descriptor = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def cleanup_temp_files(directory: Path, prefixes: tuple[str, ...]) -> list[str]:
    removed: list[str] = []
    if not directory.exists():
        return removed
    for item in sorted(directory.iterdir(), key=lambda entry: entry.name):
        if not item.is_file() or not item.name.endswith(".tmp"):
            continue
        if not any(item.name.startswith(prefix) for prefix in prefixes):
            continue
        try:
            item.unlink()
            removed.append(item.name)
        except FileNotFoundError:
            continue
    return removed
