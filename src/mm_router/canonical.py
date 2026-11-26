"""Canonical serialization, hashes, timestamps, and atomic file helpers."""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import WorkbenchError, corrupt


def canonical_bytes(value: Any) -> bytes:
    try:
        text = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise corrupt("value is not canonically serializable", reason=str(exc)) from exc
    return text.encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_bytes(value))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_json_strict(path: Path) -> Any:
    def reject_constant(token: str) -> None:
        raise ValueError(f"non-finite JSON constant {token}")

    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle, parse_constant=reject_constant)
    except FileNotFoundError as exc:
        raise corrupt("required JSON file is missing", path=str(path)) from exc
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise corrupt("JSON file cannot be parsed", path=str(path), reason=str(exc)) from exc


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def atomic_write_json(path: Path, value: Any, *, pretty: bool = True) -> None:
    if pretty:
        text = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            indent=2,
        ) + "\n"
        atomic_write_bytes(path, text.encode("utf-8"))
    else:
        atomic_write_bytes(path, canonical_bytes(value))


def require_mapping(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise corrupt("expected an object", path=path, actual_type=type(value).__name__)
    return value


def require_field(mapping: dict[str, Any], key: str, path: str) -> Any:
    if key not in mapping:
        raise corrupt("required field is missing", path=f"{path}.{key}")
    return mapping[key]


def require_int(value: Any, path: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise corrupt("expected an integer", path=path, value=value)
    if minimum is not None and value < minimum:
        raise corrupt("integer is below minimum", path=path, value=value, minimum=minimum)
    return value


def require_number(value: Any, path: str) -> float:
    if isinstance(value, bool):
        raise corrupt("boolean is not numeric", path=path, value=value)
    if isinstance(value, str):
        try:
            parsed = float(value)
        except ValueError as exc:
            raise corrupt("numeric value cannot be parsed", path=path, value=value) from exc
    elif isinstance(value, (int, float)):
        parsed = float(value)
    else:
        raise corrupt("expected a numeric value", path=path, value=value)
    if not math.isfinite(parsed):
        raise corrupt("numeric value must be finite", path=path, value=value)
    return parsed


def require_threshold(value: Any, path: str) -> float:
    parsed = require_number(value, path)
    if parsed < 0.0 or parsed > 1.0:
        raise corrupt("threshold must lie in [0, 1]", path=path, value=parsed)
    return parsed


def require_string(value: Any, path: str, *, nonempty: bool = True) -> str:
    if not isinstance(value, str) or (nonempty and not value):
        raise corrupt("expected a non-empty string", path=path, value=value)
    return value


def require_sha256(value: Any, path: str) -> str:
    text = require_string(value, path)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise corrupt("expected a lowercase SHA-256 hex digest", path=path, value=text)
    return text


def parse_timestamp_ms(value: Any, path: str) -> int:
    text = require_string(value, path)
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise corrupt("timestamp cannot be parsed", path=path, value=text) from exc
    if parsed.tzinfo is None:
        raise corrupt("timestamp must contain an explicit timezone", path=path, value=text)
    utc = parsed.astimezone(timezone.utc)
    return int(utc.timestamp() * 1000)


def format_timestamp_ms(value: int) -> str:
    seconds, milliseconds = divmod(value, 1000)
    moment = datetime.fromtimestamp(seconds, tz=timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{milliseconds:03d}Z"


def ensure_timestamp_not_rolling_back(
    current_ms: int,
    previous_ms: int | None,
    path: str,
) -> None:
    if previous_ms is not None and current_ms < previous_ms:
        raise corrupt(
            "configuration timestamp rolled backwards",
            path=path,
            current_ms=current_ms,
            previous_ms=previous_ms,
        )


def ensure_no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise corrupt("duplicate JSON key", key=key)
        result[key] = value
    return result


def read_json_strict_no_duplicates(path: Path) -> Any:
    def reject_constant(token: str) -> None:
        raise ValueError(f"non-finite JSON constant {token}")

    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(
                handle,
                parse_constant=reject_constant,
                object_pairs_hook=ensure_no_duplicate_keys,
            )
    except FileNotFoundError as exc:
        raise corrupt("required JSON file is missing", path=str(path)) from exc
    except WorkbenchError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise corrupt("JSON file cannot be parsed", path=str(path), reason=str(exc)) from exc

