from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_json_text(value: Any) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def content_hash(value: Any) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def text_hash(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def sentence_sort_key(sentence_id: str) -> str:
    """Unicode code-point order for JSON sentence identifiers."""
    return str(sentence_id)


def sorted_sentence_ids(ids: list[str]) -> list[str]:
    return sorted(ids, key=sentence_sort_key)


def read_json_file(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        from .errors import E_FORMAT_CORRUPT, WorkbenchError

        raise WorkbenchError(
            E_FORMAT_CORRUPT,
            f"invalid JSON file: {path}",
            {"path": str(path), "error": str(exc)},
        ) from exc


def read_jsonl_file(path: Path) -> list[dict[str, Any]]:
    from .errors import E_FORMAT_CORRUPT, WorkbenchError

    rows: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise WorkbenchError(
                        E_FORMAT_CORRUPT,
                        f"invalid JSONL at line {line_number}",
                        {"path": str(path), "line": line_number, "error": str(exc)},
                    ) from exc
                if not isinstance(row, dict):
                    raise WorkbenchError(
                        E_FORMAT_CORRUPT,
                        f"JSONL line {line_number} is not an object",
                        {"path": str(path), "line": line_number},
                    )
                rows.append(row)
    except FileNotFoundError:
        raise
    except UnicodeDecodeError as exc:
        raise WorkbenchError(
            E_FORMAT_CORRUPT,
            f"invalid UTF-8 input: {path}",
            {"path": str(path), "error": str(exc)},
        ) from exc
    return rows


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    ensure_parent(path)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        fsync_directory(path.parent)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_bytes(path, canonical_json_bytes(value) + b"\n")


def atomic_write_text(path: Path, value: str) -> None:
    atomic_write_bytes(path, value.encode("utf-8"))


def as_int(value: Any, field: str, *, minimum: int | None = None) -> int:
    from .errors import E_FORMAT_CORRUPT, WorkbenchError

    if isinstance(value, bool):
        raise WorkbenchError(
            E_FORMAT_CORRUPT, f"{field} must be an integer", {"field": field}
        )
    if isinstance(value, int):
        result = value
    elif isinstance(value, str) and re.fullmatch(r"[+-]?\d+", value.strip()):
        result = int(value.strip())
    else:
        raise WorkbenchError(
            E_FORMAT_CORRUPT,
            f"{field} is not parseable as an integer",
            {"field": field, "value": value},
        )
    if minimum is not None and result < minimum:
        raise WorkbenchError(
            E_FORMAT_CORRUPT,
            f"{field} must be >= {minimum}",
            {"field": field, "value": result, "minimum": minimum},
        )
    return result


def require_mapping(value: Any, field: str) -> dict[str, Any]:
    from .errors import E_FORMAT_CORRUPT, WorkbenchError

    if not isinstance(value, dict):
        raise WorkbenchError(
            E_FORMAT_CORRUPT, f"{field} must be an object", {"field": field}
        )
    return value


def require_list(value: Any, field: str) -> list[Any]:
    from .errors import E_FORMAT_CORRUPT, WorkbenchError

    if not isinstance(value, list):
        raise WorkbenchError(
            E_FORMAT_CORRUPT, f"{field} must be an array", {"field": field}
        )
    return value


def require_string(value: Any, field: str) -> str:
    from .errors import E_FORMAT_CORRUPT, WorkbenchError

    if not isinstance(value, str) or not value:
        raise WorkbenchError(
            E_FORMAT_CORRUPT,
            f"{field} must be a non-empty string",
            {"field": field, "value": value},
        )
    return value


def json_type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__

