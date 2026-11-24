"""JSONL message-stream parsing and connection-level reassembly."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .constants import SOURCE_RAW, normalize_direction
from .models import RawMessage, Rejection

_HEX_RE = re.compile(r"^[0-9a-fA-F]*$")
_CONNECTION_BAD_RE = re.compile(r"[\x00-\x20\x7f]")


@dataclass
class DirectionStream:
    direction: str
    messages: list[RawMessage]
    sequence_span: dict[str, int | None]
    missing_ranges: list[list[int]]
    duplicate_count: int
    duplicate_conflicts: int
    total_bytes: int
    reconstructed: bytes
    fragments: list[dict[str, Any]]

    def report_dict(self) -> dict[str, Any]:
        return {
            "direction": self.direction,
            "message_count": len(self.messages),
            "sequence_span": dict(self.sequence_span),
            "missing_ranges": [list(item) for item in self.missing_ranges],
            "duplicate_count": self.duplicate_count,
            "duplicate_conflicts": self.duplicate_conflicts,
            "delivered_message_count": len(self.messages)
            + self.duplicate_count,
            "delivered_bytes": self.total_bytes
            + sum(message.duplicate_bytes for message in self.messages),
            "total_bytes": self.total_bytes,
            "reconstructed_sha256": hashlib.sha256(self.reconstructed).hexdigest(),
            "fragments": self.fragments,
        }


@dataclass
class ConnectionGroup:
    connection_id: str
    messages: list[RawMessage]
    streams: dict[str, DirectionStream] = field(default_factory=dict)
    mixup_sequences: list[int] = field(default_factory=list)

    @property
    def message_count(self) -> int:
        return len(self.messages)

    @property
    def total_bytes(self) -> int:
        return sum(len(message.payload) for message in self.messages)

    @property
    def duplicate_count(self) -> int:
        return sum(message.duplicate_count for message in self.messages)

    @property
    def delivered_message_count(self) -> int:
        return len(self.messages) + self.duplicate_count

    @property
    def delivered_bytes(self) -> int:
        return sum(
            len(message.payload) + message.duplicate_bytes
            for message in self.messages
        )

    @property
    def duplicate_conflicts(self) -> int:
        return sum(message.duplicate_conflicts for message in self.messages)

    def has_gap(self) -> bool:
        return any(stream.missing_ranges for stream in self.streams.values())

    def gaps(self) -> list[dict[str, Any]]:
        return [
            {
                "direction": stream.direction,
                "missing_ranges": [list(item) for item in stream.missing_ranges],
            }
            for _, stream in sorted(self.streams.items())
            if stream.missing_ranges
        ]


def _safe_record(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        try:
            json.dumps(value, ensure_ascii=False)
            return value
        except (TypeError, ValueError):
            return {"repr": repr(value)}
    return {"value_repr": repr(value)}


def _first_present(record: dict[str, Any], aliases: tuple[str, ...]) -> tuple[str | None, Any]:
    for alias in aliases:
        if alias in record:
            return alias, record[alias]
    return None, None


def _validate_connection_id(value: Any) -> tuple[str | None, str | None]:
    if not isinstance(value, str):
        return None, "connection identifier must be a string"
    if not value:
        return None, "connection identifier must not be empty"
    if _CONNECTION_BAD_RE.search(value):
        return None, "connection identifier contains whitespace or control characters"
    return value, None


def _validate_sequence(value: Any) -> tuple[int | None, str | None]:
    if isinstance(value, bool) or not isinstance(value, int):
        return None, "sequence must be an integer"
    if value < 1:
        return None, "sequence must be >= 1"
    return value, None


def _validate_timestamp(value: Any) -> tuple[int | None, str | None]:
    if isinstance(value, bool) or not isinstance(value, int):
        return None, "timestamp_ms must be a non-negative integer"
    if value < 0:
        return None, "timestamp_ms must be a non-negative integer"
    return value, None


def _validate_payload(value: Any) -> tuple[bytes | None, str | None]:
    if not isinstance(value, str):
        return None, "payload_hex must be a string"
    if len(value) % 2:
        return None, "payload_hex must have an even number of characters"
    if not _HEX_RE.fullmatch(value):
        return None, "payload_hex contains non-hexadecimal characters"
    try:
        return bytes.fromhex(value), None
    except ValueError as exc:
        return None, f"payload_hex is not decodable: {exc}"


def parse_messages(
    source: str | Path | Iterable[str],
) -> tuple[list[RawMessage], list[Rejection]]:
    """Parse a JSONL message source.

    Invalid records are reported and skipped.  A repeated
    ``(connection_id, direction, sequence)`` keeps the first payload and counts
    later deliveries.  Arrival order is retained separately from logical
    sequence, allowing byte reconstruction to remain order-independent.
    """

    source_ref = str(source) if isinstance(source, (str, Path)) else "<iterable>"
    if isinstance(source, (str, Path)):
        with Path(source).open("r", encoding="utf-8") as handle:
            lines = handle.readlines()
    else:
        lines = list(source)

    messages: list[RawMessage] = []
    rejects: list[Rejection] = []
    first_by_key: dict[tuple[str, str, int], RawMessage] = {}

    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            rejects.append(
                Rejection(
                    source=SOURCE_RAW,
                    record_index=line_number,
                    code="E_JSON",
                    message=f"invalid JSON: {exc.msg}",
                    record={"line": line.rstrip("\r\n")[:512]},
                    record_ref=f"{source_ref}:{line_number}",
                )
            )
            continue
        if not isinstance(record, dict):
            rejects.append(
                Rejection(
                    source=SOURCE_RAW,
                    record_index=line_number,
                    code="E_RECORD_TYPE",
                    message="message record must be an object",
                    record=_safe_record(record),
                    record_ref=f"{source_ref}:{line_number}",
                )
            )
            continue

        errors: list[str] = []
        conn_key, conn_value = _first_present(record, ("connection_id", "conn_id", "connection"))
        if conn_key is None:
            errors.append("missing connection identifier")
            conn_id = None
        else:
            conn_id, error = _validate_connection_id(conn_value)
            if error:
                errors.append(error)

        dir_key, dir_value = _first_present(record, ("direction", "dir"))
        direction = normalize_direction(dir_value)
        if dir_key is None:
            errors.append("missing direction")
        elif direction is None:
            errors.append("direction must be request/response/c2s/s2c/both")

        seq_key, seq_value = _first_present(record, ("sequence", "seq", "message_sequence"))
        sequence, error = _validate_sequence(seq_value)
        if seq_key is None:
            errors.append("missing sequence")
        elif error:
            errors.append(error)

        ts_key, ts_value = _first_present(
            record, ("timestamp_ms", "logical_ms", "ts_ms", "timestamp")
        )
        timestamp_ms, error = _validate_timestamp(ts_value)
        if ts_key is None:
            errors.append("missing timestamp_ms")
        elif error:
            errors.append(error)

        payload_key, payload_value = _first_present(record, ("payload_hex", "payload"))
        payload, error = _validate_payload(payload_value)
        if payload_key is None:
            errors.append("missing payload_hex")
        elif error:
            errors.append(error)

        if errors or conn_id is None or direction is None or sequence is None:
            rejects.append(
                Rejection(
                    source=SOURCE_RAW,
                    record_index=line_number,
                    code="E_MESSAGE_FIELD",
                    message="; ".join(errors),
                    record=_safe_record(record),
                    record_ref=f"{source_ref}:{line_number}",
                )
            )
            continue

        # A message stream has two endpoint directions, never the "both"
        # pseudo-direction used by rules.
        if direction not in ("request", "response"):
            rejects.append(
                Rejection(
                    source=SOURCE_RAW,
                    record_index=line_number,
                    code="E_MESSAGE_DIRECTION",
                    message="message direction must resolve to request or response",
                    record=_safe_record(record),
                    record_ref=f"{source_ref}:{line_number}",
                )
            )
            continue

        message = RawMessage(
            connection_id=conn_id,
            direction=direction,
            sequence=sequence,
            timestamp_ms=timestamp_ms if timestamp_ms is not None else 0,
            payload=payload if payload is not None else b"",
            source_index=line_number,
            arrival_index=len(messages),
            sync_marker=record.get("sync_marker")
            if isinstance(record.get("sync_marker"), str)
            else None,
        )
        key = (conn_id, direction, sequence)
        prior = first_by_key.get(key)
        if prior is not None:
            prior.duplicate_count += 1
            prior.duplicate_bytes += len(message.payload)
            if prior.payload != message.payload:
                prior.duplicate_conflicts += 1
            continue
        first_by_key[key] = message
        messages.append(message)

    return messages, rejects


def _missing_ranges(sequences: list[int], first_sequence: int) -> list[list[int]]:
    if not sequences:
        return []
    present = set(sequences)
    maximum = max(sequences)
    result: list[list[int]] = []
    start: int | None = None
    for sequence in range(first_sequence, maximum + 1):
        if sequence not in present:
            if start is None:
                start = sequence
        elif start is not None:
            result.append([start, sequence - 1])
            start = None
    if start is not None:
        result.append([start, maximum])
    return result


def assemble_direction(
    direction: str,
    messages: list[RawMessage],
    *,
    first_sequence: int = 1,
) -> DirectionStream:
    ordered = sorted(messages, key=lambda item: item.sequence)
    sequences = [item.sequence for item in ordered]
    missing = _missing_ranges(sequences, first_sequence)
    fragments: list[dict[str, Any]] = []
    parts: list[bytes] = []
    cursor = 0
    for message in ordered:
        start = cursor
        cursor += len(message.payload)
        parts.append(message.payload)
        fragments.append(
            {
                "sequence": message.sequence,
                "timestamp_ms": message.timestamp_ms,
                "byte_range": {"start": start, "end": cursor},
                "payload_hex": message.payload_hex,
                "payload_sha256": hashlib.sha256(message.payload).hexdigest(),
                "duplicate_count": message.duplicate_count,
                "duplicate_conflicts": message.duplicate_conflicts,
                "duplicate_bytes": message.duplicate_bytes,
                "source_index": message.source_index,
            }
        )
    return DirectionStream(
        direction=direction,
        messages=ordered,
        sequence_span={
            "first": min(sequences) if sequences else None,
            "last": max(sequences) if sequences else None,
        },
        missing_ranges=missing,
        duplicate_count=sum(item.duplicate_count for item in ordered),
        duplicate_conflicts=sum(item.duplicate_conflicts for item in ordered),
        total_bytes=sum(len(item.payload) for item in ordered),
        reconstructed=b"".join(parts),
        fragments=fragments,
    )


def group_connections(
    messages: list[RawMessage],
    *,
    first_sequence: int = 1,
) -> tuple[list[ConnectionGroup], list[int]]:
    """Group messages by connection and detect cross-connection duplicates.

    A cross-connection mixup is identified by an identical
    ``(direction, sequence, payload)`` appearing under two connection IDs.
    This is conservative: ordinary repeated bodies with different sequence
    numbers are not treated as mixups.
    """

    signatures: dict[tuple[str, int, str], set[str]] = {}
    for message in messages:
        signature = (
            message.direction,
            message.sequence,
            hashlib.sha256(message.payload).hexdigest(),
        )
        signatures.setdefault(signature, set()).add(message.connection_id)

    mixed_signatures = {
        signature: sorted(connections)
        for signature, connections in signatures.items()
        if len(connections) > 1
    }

    by_connection: dict[str, list[RawMessage]] = {}
    for message in messages:
        by_connection.setdefault(message.connection_id, []).append(message)

    groups: list[ConnectionGroup] = []
    for connection_id in sorted(by_connection):
        connection_messages = sorted(
            by_connection[connection_id],
            key=lambda item: (item.arrival_index, item.source_index),
        )
        group = ConnectionGroup(connection_id=connection_id, messages=connection_messages)
        by_direction: dict[str, list[RawMessage]] = {}
        for message in connection_messages:
            by_direction.setdefault(message.direction, []).append(message)
        for direction in sorted(by_direction):
            group.streams[direction] = assemble_direction(
                direction,
                by_direction[direction],
                first_sequence=first_sequence,
            )
        connection_signatures = {
            (
                message.direction,
                message.sequence,
                hashlib.sha256(message.payload).hexdigest(),
            )
            for message in connection_messages
        }
        mixed_sequences = {
            sequence
            for direction, sequence, digest in mixed_signatures
            if (direction, sequence, digest) in connection_signatures
        }
        group.mixup_sequences = sorted(mixed_sequences)
        groups.append(group)
    return groups, sorted({seq for group in groups for seq in group.mixup_sequences})


def parse_and_group(
    source: str | Path | Iterable[str],
    *,
    first_sequence: int = 1,
) -> tuple[list[ConnectionGroup], list[Rejection]]:
    messages, rejects = parse_messages(source)
    groups, _ = group_connections(messages, first_sequence=first_sequence)
    return groups, rejects

