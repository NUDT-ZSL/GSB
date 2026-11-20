from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .errors import GrayrollError


def canonical_json(value: Any, *, indent: int | None = None) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":") if indent is None else None,
        indent=indent,
        allow_nan=False,
    )


def canonical_bytes(value: Any) -> bytes:
    return canonical_json(value).encode("utf-8")


def canonical_line(value: Any) -> bytes:
    return canonical_bytes(value) + b"\n"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json_file(path: Path, code: str) -> Any:
    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise GrayrollError(code, f"duplicate JSON key in {path.name}: {key}")
            result[key] = value
        return result

    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle, object_pairs_hook=reject_duplicate_keys)
    except FileNotFoundError as exc:
        raise GrayrollError(code, f"file not found: {path.name}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise GrayrollError(code, f"cannot parse JSON file {path.name}: {exc}") from exc


def write_json_file(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    data = (canonical_json(value, indent=2) + "\n").encode("utf-8")
    with tmp.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def natural_key(value: str) -> tuple[tuple[int, Any], ...]:
    parts = re.split(r"(\d+)", value)
    result: list[tuple[int, Any]] = []
    for part in parts:
        if not part:
            continue
        if part.isdigit():
            result.append((0, int(part)))
        else:
            result.append((1, part))
    return tuple(result)


def require_dict(value: Any, field: str, code: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GrayrollError(code, f"{field} must be an object")
    return value


def require_list(value: Any, field: str, code: str) -> list[Any]:
    if not isinstance(value, list):
        raise GrayrollError(code, f"{field} must be an array")
    return value


def require_str(value: Any, field: str, code: str) -> str:
    if not isinstance(value, str) or not value:
        raise GrayrollError(code, f"{field} must be a non-empty string")
    return value


def require_int(value: Any, field: str, code: str, *, minimum: int | None = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise GrayrollError(code, f"{field} must be an integer")
    if minimum is not None and value < minimum:
        raise GrayrollError(code, f"{field} must be >= {minimum}")
    return value


def require_number(value: Any, field: str, code: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GrayrollError(code, f"{field} must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise GrayrollError(code, f"{field} must be finite")
    return number


def require_keys(
    obj: dict[str, Any], keys: Iterable[str], field: str, code: str
) -> None:
    missing = [key for key in keys if key not in obj]
    if missing:
        raise GrayrollError(code, f"{field} missing fields: {', '.join(missing)}")


def tier_digest(batches: Iterable[tuple[str, str, Iterable[float]]]) -> str:
    payload = {
        f"{service}|{batch_id}": [round(float(ratio), 12) for ratio in ratios]
        for service, batch_id, ratios in batches
    }
    return sha256_bytes(canonical_bytes(payload))


def as_float_list(value: Any, field: str, code: str) -> list[float]:
    raw = require_list(value, field, code)
    return [require_number(item, f"{field}[]", code) for item in raw]


def safe_relative_path(base: Path, relative: str, code: str) -> Path:
    if not isinstance(relative, str) or not relative:
        raise GrayrollError(code, "path must be a non-empty string")
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise GrayrollError(code, f"unsafe relative path: {relative}")
    resolved_base = base.resolve()
    resolved = (base / candidate).resolve()
    try:
        resolved.relative_to(resolved_base)
    except ValueError as exc:
        raise GrayrollError(code, f"path escapes package root: {relative}") from exc
    return resolved
