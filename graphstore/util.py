from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import unicodedata
from pathlib import Path
from typing import Any, Iterator

from .errors import ErrorCode, fail


JSON_SEPARATORS = (",", ":")
LABEL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:/-]{0,127}$")


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=JSON_SEPARATORS,
        ensure_ascii=False,
        allow_nan=False,
    )


def canonical_bytes(value: Any) -> bytes:
    return canonical_json(value).encode("utf-8")


def digest_json(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def digest_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def canonical_node_id(value: Any) -> str:
    if not isinstance(value, str):
        raise fail(
            ErrorCode.INVALID_ARGUMENT,
            "node id must be a string",
            value=repr(value),
        )
    normalized = unicodedata.normalize("NFC", value.strip())
    if not normalized:
        raise fail(ErrorCode.INVALID_ARGUMENT, "node id must not be empty")
    if normalized != value:
        raise fail(
            ErrorCode.INVALID_ARGUMENT,
            "node id is not canonical (trim/NFC required)",
            value=value,
            canonical=normalized,
        )
    return normalized


def validate_label(value: Any, *, code: str = ErrorCode.INVALID_EDGE_LABEL) -> str:
    if not isinstance(value, str) or not LABEL_RE.fullmatch(value):
        raise fail(
            code,
            "label must match ^[A-Za-z][A-Za-z0-9_.:/-]{0,127}$",
            value=repr(value),
        )
    return value


def stable_sorted(values: Iterator[str] | list[str] | set[str] | tuple[str, ...]) -> list[str]:
    return sorted(values)


def read_json(path: Path, *, code: str) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        raise fail(code, "file does not exist", path=str(path))
    except (OSError, json.JSONDecodeError) as exc:
        raise fail(code, "unable to read JSON", path=str(path), error=str(exc))


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(canonical_json(value))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def ensure_within(root: Path, candidate: Path) -> Path:
    root_resolved = root.resolve()
    candidate_resolved = candidate.resolve()
    try:
        candidate_resolved.relative_to(root_resolved)
    except ValueError:
        raise fail(
            ErrorCode.V2_MANIFEST_INVALID,
            "shard path escapes snapshot directory",
            path=str(candidate),
        )
    return candidate_resolved

