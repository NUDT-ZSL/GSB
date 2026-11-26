"""Small deterministic helpers; no wall-clock reads are permitted here."""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable

from .errors import InputError


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def pretty_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_json(path: str | os.PathLike[str]) -> Any:
    source = Path(path)
    try:
        raw = source.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise InputError(
            "INPUT_FILE_MISSING",
            f"Input file does not exist: {source}",
            diagnostics={"path": str(source)},
        ) from exc
    except OSError as exc:
        raise InputError(
            "INPUT_FILE_UNREADABLE",
            f"Cannot read input file: {source}",
            diagnostics={"path": str(source), "reason": str(exc)},
        ) from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InputError(
            "INPUT_JSON_INVALID",
            f"Invalid JSON in {source}",
            diagnostics={
                "path": str(source),
                "line": exc.lineno,
                "column": exc.colno,
                "reason": exc.msg,
            },
        ) from exc


def read_jsonl(path: str | os.PathLike[str]) -> list[Any]:
    source = Path(path)
    try:
        lines = source.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise InputError(
            "INPUT_FILE_MISSING",
            f"Input file does not exist: {source}",
            diagnostics={"path": str(source)},
        ) from exc
    except OSError as exc:
        raise InputError(
            "INPUT_FILE_UNREADABLE",
            f"Cannot read input file: {source}",
            diagnostics={"path": str(source), "reason": str(exc)},
        ) from exc

    records: list[Any] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise InputError(
                "INPUT_JSONL_INVALID",
                f"Invalid JSONL record at {source}:{line_number}",
                diagnostics={
                    "path": str(source),
                    "line": line_number,
                    "column": exc.colno,
                    "reason": exc.msg,
                },
            ) from exc
    return records


def write_json_atomic(path: str | os.PathLike[str], value: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(target.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(pretty_json(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def is_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def require_object(
    value: Any,
    *,
    path: str,
    code: str = "INPUT_SCHEMA_INVALID",
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise InputError(
            code,
            f"Expected a JSON object at {path}",
            diagnostics={"path": path, "actual_type": type(value).__name__},
        )
    return value


def require_list(
    value: Any,
    *,
    path: str,
    code: str = "INPUT_SCHEMA_INVALID",
) -> list[Any]:
    if not isinstance(value, list):
        raise InputError(
            code,
            f"Expected a JSON array at {path}",
            diagnostics={"path": path, "actual_type": type(value).__name__},
        )
    return value


def require_nonempty_string(value: Any, *, path: str, code: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InputError(
            code,
            f"Expected a non-empty string at {path}",
            diagnostics={"path": path, "actual_type": type(value).__name__},
        )
    return value


def require_nonnegative_int(value: Any, *, path: str, code: str) -> int:
    if not is_int(value) or value < 0:
        raise InputError(
            code,
            f"Expected a non-negative integer at {path}",
            diagnostics={"path": path, "value": value},
        )
    return value


def require_positive_int(value: Any, *, path: str, code: str) -> int:
    if not is_int(value) or value <= 0:
        raise InputError(
            code,
            f"Expected a positive integer at {path}",
            diagnostics={"path": path, "value": value},
        )
    return value


def unique_sorted(values: Iterable[str]) -> list[str]:
    return sorted(set(values))


def stable_partition_values(values: Iterable[Any]) -> list[Any]:
    return sorted(values, key=canonical_json)
