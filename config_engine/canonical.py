"""Canonical JSON, strict JSON reading, hashes, and scalar validation."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from .errors import error


class _DuplicateKey(ValueError):
    pass


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey(key)
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


def canonical_json(value: Any) -> str:
    """Return the single canonical representation used for hashes and output."""

    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def pretty_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        indent=2,
    )


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def sha256_value(value: Any) -> str:
    return sha256_text(canonical_json(value))


def digest_token(value: str) -> str:
    value = value.strip().lower()
    if value.startswith("sha256:"):
        value = value[7:]
    return value


def load_json_file(path: Path) -> Any:
    if not path.exists():
        raise error("CONFIG_PACKAGE_MISSING", f"missing JSON file: {path}", path=str(path))
    if not path.is_file():
        raise error("CONFIG_PACKAGE_MISSING", f"not a regular file: {path}", path=str(path))
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise error(
            "CONFIG_PACKAGE_MISSING",
            f"cannot read JSON file: {path}",
            path=str(path),
            os_error=str(exc),
        ) from exc
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise error("JSON_MALFORMED", f"JSON file is not UTF-8: {path}", path=str(path)) from exc
    try:
        return json.loads(
            text,
            object_pairs_hook=_no_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except _DuplicateKey as exc:
        raise error(
            "JSON_MALFORMED",
            f"duplicate object key in {path}: {exc}",
            path=str(path),
            reason="duplicate_key",
        ) from exc
    except (json.JSONDecodeError, ValueError) as exc:
        raise error(
            "JSON_MALFORMED",
            f"malformed JSON in {path}: {exc}",
            path=str(path),
            reason="parse_error",
        ) from exc


def is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def is_finite_number(value: Any) -> bool:
    return is_number(value) and math.isfinite(float(value))


_SERVICE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ENV_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def validate_name(value: Any, field: str, *, environment: bool = False) -> str:
    pattern = _ENV_RE if environment else _SERVICE_RE
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise error(
            "SCHEMA_INVALID",
            f"{field} must be a non-empty safe identifier",
            field=field,
            value=value,
        )
    return value


def validate_flat_params(value: Any, field: str = "params") -> dict[str, Any]:
    if not isinstance(value, dict):
        raise error("INVALID_PARAMS", f"{field} must be a JSON object", field=field)
    result: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise error("INVALID_PARAMS", f"{field} keys must be non-empty strings", field=field)
        if isinstance(item, (dict, list)):
            raise error(
                "INVALID_PARAMS",
                f"{field}.{key} must be a flat scalar value",
                field=f"{field}.{key}",
            )
        if isinstance(item, float) and not math.isfinite(item):
            raise error(
                "INVALID_PARAMS",
                f"{field}.{key} must be finite",
                field=f"{field}.{key}",
            )
        if item is not None and not isinstance(item, (str, int, float, bool)):
            raise error(
                "INVALID_PARAMS",
                f"{field}.{key} has an unsupported JSON type",
                field=f"{field}.{key}",
                type=type(item).__name__,
            )
        result[key] = item
    return result


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = pretty_json(value) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)

