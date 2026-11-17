from __future__ import annotations

import json
from typing import Any

from .models import Event

MAX_TS_MS = 253_402_300_799_999
MAX_SEQ = 2**63 - 1

REQUIRED_STRING_FIELDS = ("event_id", "trace_id", "actor", "action", "object")
REQUIRED_FIELDS = REQUIRED_STRING_FIELDS + ("ts", "seq")


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _reject(
    line_no: int,
    source: str,
    code: str,
    message: str,
    **details: Any,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "code": code,
        "message": message,
        "line": line_no,
        "source": source,
    }
    record.update(details)
    return record


def normalize_line(
    raw_line: bytes, line_no: int, source: str
) -> tuple[Event | None, dict[str, Any] | None]:
    try:
        text = raw_line.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        return None, _reject(
            line_no,
            source,
            "E_ENCODING",
            "line is not valid UTF-8",
            detail=str(exc),
        )

    if not text.strip():
        return None, _reject(
            line_no,
            source,
            "E_EMPTY_LINE",
            "empty JSONL line",
        )

    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, _reject(
            line_no,
            source,
            "E_JSON",
            "invalid JSON",
            column=exc.colno,
        )

    if not isinstance(value, dict):
        return None, _reject(
            line_no,
            source,
            "E_NOT_OBJECT",
            "JSONL value must be an object",
            actual_type=type(value).__name__,
        )
    return validate_event_mapping(value, line_no, source)


def validate_event_mapping(
    value: Any,
    line_no: int = 0,
    source: str = "checkpoint",
) -> tuple[Event | None, dict[str, Any] | None]:
    if not isinstance(value, dict):
        return None, _reject(
            line_no,
            source,
            "E_NOT_OBJECT",
            "event value must be an object",
            actual_type=type(value).__name__,
        )
    missing = [field for field in REQUIRED_FIELDS if field not in value]
    if missing:
        return None, _reject(
            line_no,
            source,
            "E_MISSING_FIELD",
            "required event field is missing",
            fields=missing,
        )

    bad_types = [
        field
        for field in REQUIRED_STRING_FIELDS
        if not isinstance(value[field], str)
    ]
    if not _is_int(value["ts"]):
        bad_types.append("ts")
    if not _is_int(value["seq"]):
        bad_types.append("seq")
    parent = value.get("parent_event_id")
    if parent is not None and not isinstance(parent, str):
        bad_types.append("parent_event_id")
    if bad_types:
        return None, _reject(
            line_no,
            source,
            "E_INVALID_TYPE",
            "event field has an invalid type",
            fields=sorted(bad_types),
        )

    empty_fields = [
        field
        for field in REQUIRED_STRING_FIELDS
        if not value[field].strip()
    ]
    if parent is not None and not parent.strip():
        empty_fields.append("parent_event_id")
    if empty_fields:
        return None, _reject(
            line_no,
            source,
            "E_INVALID_VALUE",
            "string event field must not be empty",
            fields=sorted(empty_fields),
        )

    if value["ts"] < 0 or value["ts"] > MAX_TS_MS:
        return None, _reject(
            line_no,
            source,
            "E_TS_RANGE",
            f"ts must be between 0 and {MAX_TS_MS}",
            value=value["ts"],
        )

    if value["seq"] < 1 or value["seq"] > MAX_SEQ:
        return None, _reject(
            line_no,
            source,
            "E_SEQ_RANGE",
            f"seq must be between 1 and {MAX_SEQ}",
            value=value["seq"],
        )

    event = Event(
        event_id=value["event_id"],
        trace_id=value["trace_id"],
        actor=value["actor"],
        action=value["action"],
        object=value["object"],
        ts=value["ts"],
        seq=value["seq"],
        parent_event_id=parent,
    )
    return event, None
