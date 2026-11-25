from __future__ import annotations

import math
from typing import Any

from ..errors import DiagnosticError, ErrorCode
from ..jsonutil import parse_int


def parse_flat_mask(
    value: Any,
    *,
    field: str,
    line: int,
    case_id: str | None,
) -> tuple[int, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise DiagnosticError(
            ErrorCode.E_FORMAT_CORRUPT,
            f"{field} must be an array of integer indices",
            field=field,
            line=line,
            case_id=case_id,
        )
    result: list[int] = []
    for index, item in enumerate(value):
        parsed = parse_int(item, f"{field}[{index}]", line, case_id, minimum=0)
        result.append(parsed)
    return tuple(sorted(set(result)))


def parse_revision_object(value: Any, field: str, line: int, case_id: str | None) -> tuple[int, str]:
    if not isinstance(value, dict):
        raise DiagnosticError(
            ErrorCode.E_FORMAT_CORRUPT,
            f"{field} must be an object",
            field=field,
            line=line,
            case_id=case_id,
        )
    number = value.get("number", value.get("increment"))
    revision_id = value.get("revision_id", value.get("id"))
    if number is None:
        raise DiagnosticError(
            ErrorCode.E_FORMAT_CORRUPT,
            f"missing field {field}.number",
            field=f"{field}.number",
            line=line,
            case_id=case_id,
        )
    if revision_id is None:
        raise DiagnosticError(
            ErrorCode.E_FORMAT_CORRUPT,
            f"missing field {field}.revision_id",
            field=f"{field}.revision_id",
            line=line,
            case_id=case_id,
        )
    revision = parse_int(number, f"{field}.number", line, case_id, minimum=1)
    if not isinstance(revision_id, str) or not revision_id:
        raise DiagnosticError(
            ErrorCode.E_FORMAT_CORRUPT,
            f"{field}.revision_id must be a non-empty string",
            field=f"{field}.revision_id",
            line=line,
            case_id=case_id,
        )
    return revision, revision_id


def default_chunk_count(logits_length: int, chunk_size: int) -> int:
    if chunk_size <= 0:
        raise DiagnosticError(
            ErrorCode.E_CONFIG_INVALID,
            "default chunk_size must be positive",
            field="chunk_size",
        )
    return math.ceil(logits_length / chunk_size)


def check_logits_shape(
    declared_shape: tuple[int, ...],
    logits_length: int,
    *,
    field: str,
    line: int,
    case_id: str | None,
) -> None:
    if len(declared_shape) != 1 or declared_shape[0] != logits_length:
        raise DiagnosticError(
            ErrorCode.E_SHAPE_MISMATCH,
            "declared logits shape does not match logits values",
            field=field,
            line=line,
            case_id=case_id,
            details={
                "declared_shape": list(declared_shape),
                "actual_shape": [logits_length],
            },
        )

