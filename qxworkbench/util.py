"""Deterministic serialization and filesystem helpers."""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable

from .errors import format_corrupt


def stable_json_bytes(value: Any, *, newline: bool = True) -> bytes:
    """Serialize JSON in one canonical byte representation."""

    text = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return (text + ("\n" if newline else "")).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def checksum_object(value: Any) -> str:
    return sha256_bytes(stable_json_bytes(value, newline=False))


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_bytes(path, stable_json_bytes(value))


def read_json(path: Path) -> Any:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise format_corrupt(
            "cannot read JSON file",
            source=str(path),
            reason=str(exc),
        ) from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise format_corrupt(
            "invalid JSON",
            source=str(path),
            line=exc.lineno,
            column=exc.colno,
            reason=exc.msg,
        ) from exc


def read_jsonl_with_locations(path: Path) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Read JSONL and retain the physical source location of every object."""

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise format_corrupt(
            "cannot read JSONL file",
            source=str(path),
            reason=str(exc),
        ) from exc

    records: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise format_corrupt(
                "invalid JSONL record",
                source=str(path),
                line=line_number,
                column=exc.colno,
                reason=exc.msg,
            ) from exc
        if not isinstance(value, dict):
            raise format_corrupt(
                "JSONL record must be an object",
                source=str(path),
                line=line_number,
            )
        records.append((value, {"source": str(path), "line": line_number}))
    return records


def require_int(
    value: Any,
    *,
    field: str,
    minimum: int | None = None,
    location: dict[str, Any] | None = None,
) -> int:
    if isinstance(value, bool):
        raise format_corrupt(
            "integer field is not parseable",
            field=field,
            value=value,
            location=location or {},
        )
    if isinstance(value, str):
        try:
            parsed_value = int(value.strip(), 10)
        except ValueError as exc:
            raise format_corrupt(
                "integer field is not parseable",
                field=field,
                value=value,
                location=location or {},
            ) from exc
        value = parsed_value
    elif not isinstance(value, int):
        raise format_corrupt(
            "integer field is not parseable",
            field=field,
            value=value,
            location=location or {},
        )
    if minimum is not None and value < minimum:
        raise format_corrupt(
            "integer field is below its minimum",
            field=field,
            value=value,
            minimum=minimum,
            location=location or {},
        )
    return value


def require_float(
    value: Any,
    *,
    field: str,
    minimum: float | None = None,
    maximum: float | None = None,
    location: dict[str, Any] | None = None,
) -> float:
    if isinstance(value, bool):
        raise format_corrupt(
            "numeric field is not parseable",
            field=field,
            value=value,
            location=location or {},
        )
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise format_corrupt(
            "numeric field is not parseable",
            field=field,
            value=value,
            location=location or {},
        ) from exc
    if not math.isfinite(parsed):
        raise format_corrupt(
            "numeric field must be finite",
            field=field,
            value=value,
            location=location or {},
        )
    if minimum is not None and parsed < minimum:
        raise format_corrupt(
            "numeric field is below its minimum",
            field=field,
            value=value,
            minimum=minimum,
            location=location or {},
        )
    if maximum is not None and parsed > maximum:
        raise format_corrupt(
            "numeric field is above its maximum",
            field=field,
            value=value,
            maximum=maximum,
            location=location or {},
        )
    return parsed


def parse_bool(
    value: Any,
    *,
    field: str,
    location: dict[str, Any] | None = None,
) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes"}:
            return True
        if lowered in {"false", "0", "no"}:
            return False
    raise format_corrupt(
        "boolean field is not parseable",
        field=field,
        value=value,
        location=location or {},
    )


def sorted_unique(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted(set(values)))


def round_score(value: float) -> float:
    # Twelve decimal places are enough to survive JSON/process boundaries while
    # keeping every emitted arithmetic value exactly reproducible.
    return float(round(float(value), 12))
