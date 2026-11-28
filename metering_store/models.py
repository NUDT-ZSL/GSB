"""Metering record validation and deterministic serialization."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

RECORD_FIELDS = (
    "area_id",
    "meter_id",
    "ts_ms",
    "active_energy_wh",
    "reactive_energy_varh",
    "voltage_mv",
    "current_ma",
    "quality_flag",
)

INTEGER_FIELDS = (
    "ts_ms",
    "active_energy_wh",
    "reactive_energy_varh",
    "voltage_mv",
    "current_ma",
)

STRING_FIELDS = (
    "area_id",
    "meter_id",
    "quality_flag",
)


@dataclass(frozen=True)
class ParsedRecord:
    """One record plus its exact byte span in a JSONL record stream."""

    record: dict[str, Any]
    start_offset: int
    end_offset: int
    raw: bytes


def validate_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a metering record and return a plain dictionary copy."""
    if not isinstance(record, Mapping):
        raise ValueError("record must be a mapping")

    normalized: dict[str, Any] = {}
    for key in RECORD_FIELDS:
        if key not in record:
            raise ValueError(f"record is missing required field: {key}")

    for key in STRING_FIELDS:
        value = record[key]
        if not isinstance(value, str) or not value:
            raise ValueError(f"{key} must be a non-empty string")
        normalized[key] = value

    for key in INTEGER_FIELDS:
        value = record[key]
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{key} must be an integer")
        normalized[key] = value

    extra_fields = sorted(set(record) - set(RECORD_FIELDS))
    if extra_fields:
        raise ValueError(f"record contains unsupported fields: {', '.join(extra_fields)}")
    return normalized


def canonical_record_bytes(record: Mapping[str, Any]) -> bytes:
    """Serialize one record using the stable public byte format."""
    normalized = validate_record(record)
    text = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return text.encode("utf-8") + b"\n"


def serialize_records(records: Iterable[Mapping[str, Any]]) -> bytes:
    """Serialize records with no dependence on input mapping insertion order."""
    return b"".join(canonical_record_bytes(record) for record in records)


def parse_record_stream(data: bytes) -> list[ParsedRecord]:
    """Parse a JSONL record stream while preserving exact offsets and bytes."""
    if not isinstance(data, bytes):
        raise ValueError("record stream must be bytes")

    parsed: list[ParsedRecord] = []
    offset = 0
    for line_number, line in enumerate(data.splitlines(keepends=True), start=1):
        if not line.strip():
            raise ValueError(f"blank record line at line {line_number}")
        try:
            decoded = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid JSON record at line {line_number}: {exc}") from exc
        record = validate_record(decoded)
        end_offset = offset + len(line)
        parsed.append(
            ParsedRecord(
                record=record,
                start_offset=offset,
                end_offset=end_offset,
                raw=line,
            )
        )
        offset = end_offset

    if data and not parsed:
        raise ValueError("record stream is not valid JSONL")
    if parsed and parsed[-1].end_offset != len(data):
        raise ValueError("record stream has trailing data")
    return parsed
