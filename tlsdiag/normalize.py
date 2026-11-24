"""Parsing and normalisation for the JSONL handshake record stream."""

from __future__ import annotations

import json
import re
from collections import OrderedDict
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import codes
from .models import Diagnostic, RawRecord


_CONNECTION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_HEX_RE = re.compile(r"^[0-9a-fA-F]*$")

_DIRECTION_MAP = {
    "client_to_server": "client_to_server",
    "c2s": "client_to_server",
    "client->server": "client_to_server",
    "client-server": "client_to_server",
    "server_to_client": "server_to_client",
    "s2c": "server_to_client",
    "server->client": "server_to_client",
    "server-client": "server_to_client",
    "outbound": "server_to_client",
    "inbound": "client_to_server",
}


def normalize_direction(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    key = value.strip().lower().replace(" ", "_")
    return _DIRECTION_MAP.get(key)


def valid_connection_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_CONNECTION_RE.match(value))


def _as_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        try:
            return int(text, 10)
        except ValueError:
            return None
    return None


def _first(mapping: Dict[str, Any], names: Sequence[str]) -> Any:
    for name in names:
        if name in mapping:
            return mapping[name]
    return None


def parse_handshake_jsonl(
    data: bytes, source_file: str
) -> Tuple[List[RawRecord], List[Diagnostic]]:
    """Parse a JSONL byte stream.

    Invalid lines are skipped and described by a diagnostic that carries the
    source file and the 1-based line (record) index.  Valid later records are
    still returned.
    """

    records: List[RawRecord] = []
    diagnostics: List[Diagnostic] = []
    arrival = 0

    text = data.decode("utf-8-sig", errors="replace")
    lines = text.splitlines()
    if not lines:
        diagnostics.append(
            Diagnostic(
                code=codes.E_EMPTY_INPUT,
                message="handshake record stream is empty",
                source_file=source_file,
            )
        )
        return records, diagnostics

    for line_no, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        arrival += 1  # Arrival order is the file order of non-empty lines.
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            diagnostics.append(
                Diagnostic(
                    code=codes.E_JSON,
                    message=f"invalid JSON: {exc.msg}",
                    source_file=source_file,
                    record_index=line_no,
                    detail={"column": exc.colno},
                )
            )
            continue
        if not isinstance(obj, dict):
            diagnostics.append(
                Diagnostic(
                    code=codes.E_JSON,
                    message="record must be a JSON object",
                    source_file=source_file,
                    record_index=line_no,
                )
            )
            continue

        connection_id = _first(obj, ("connection_id", "conn_id", "connection"))
        if not valid_connection_id(connection_id):
            diagnostics.append(
                Diagnostic(
                    code=codes.E_CONN_ID,
                    message="connection id is missing or illegal",
                    source_file=source_file,
                    record_index=line_no,
                    detail={"value": connection_id},
                )
            )
            continue

        direction = normalize_direction(obj.get("direction"))
        if direction is None:
            diagnostics.append(
                Diagnostic(
                    code=codes.E_DIRECTION,
                    message="direction must be client_to_server or server_to_client",
                    source_file=source_file,
                    record_index=line_no,
                    connection_id=connection_id,
                    detail={"value": obj.get("direction")},
                )
            )
            continue

        record_seq = _as_int(_first(obj, ("record_seq", "seq", "record_sequence")))
        if record_seq is None or record_seq < 0:
            diagnostics.append(
                Diagnostic(
                    code=codes.E_SEQ,
                    message="record sequence must be a non-negative integer",
                    source_file=source_file,
                    record_index=line_no,
                    connection_id=connection_id,
                    detail={"value": obj.get("record_seq")},
                )
            )
            continue

        timestamp_ms = _as_int(
            _first(obj, ("timestamp_ms", "logical_ms", "observed_ms", "timestamp"))
        )
        if timestamp_ms is None or timestamp_ms < 0:
            diagnostics.append(
                Diagnostic(
                    code=codes.E_TIMESTAMP,
                    message="logical timestamp must be a non-negative integer number of milliseconds",
                    source_file=source_file,
                    record_index=line_no,
                    connection_id=connection_id,
                    message_seq=record_seq,
                    detail={"value": obj.get("timestamp_ms")},
                )
            )
            continue

        record_type = obj.get("record_type", obj.get("type", "fragment"))
        if not isinstance(record_type, str) or not record_type.strip():
            diagnostics.append(
                Diagnostic(
                    code=codes.E_RECORD_TYPE,
                    message="record type must be a non-empty string",
                    source_file=source_file,
                    record_index=line_no,
                    connection_id=connection_id,
                    message_seq=record_seq,
                )
            )
            continue

        payload_hex = _first(obj, ("payload_hex", "fragment_hex", "payload"))
        if not isinstance(payload_hex, str):
            diagnostics.append(
                Diagnostic(
                    code=codes.E_HEX,
                    message="payload must be a hexadecimal string",
                    source_file=source_file,
                    record_index=line_no,
                    connection_id=connection_id,
                    message_seq=record_seq,
                )
            )
            continue
        payload_hex = payload_hex.strip()
        if len(payload_hex) % 2 != 0 or not _HEX_RE.match(payload_hex):
            diagnostics.append(
                Diagnostic(
                    code=codes.E_HEX,
                    message="payload hex must be even-length and contain only [0-9a-f]",
                    source_file=source_file,
                    record_index=line_no,
                    connection_id=connection_id,
                    message_seq=record_seq,
                    detail={"length": len(payload_hex)},
                )
            )
            continue

        try:
            payload = bytes.fromhex(payload_hex)
        except ValueError:
            diagnostics.append(
                Diagnostic(
                    code=codes.E_HEX,
                    message="payload could not be decoded as hexadecimal",
                    source_file=source_file,
                    record_index=line_no,
                    connection_id=connection_id,
                    message_seq=record_seq,
                )
            )
            continue

        embedded = _first(
            obj,
            (
                "origin_connection_id",
                "embedded_connection_id",
                "stream_connection_id",
                "fragment_connection_id",
            ),
        )
        if embedded is not None and not isinstance(embedded, str):
            embedded = str(embedded)

        records.append(
            RawRecord(
                connection_id=connection_id,
                direction=direction,
                record_seq=record_seq,
                timestamp_ms=timestamp_ms,
                record_type=record_type.strip().lower(),
                payload_hex=payload_hex.lower(),
                payload=payload,
                source_file=source_file,
                line_no=line_no,
                arrival_index=arrival,
                embedded_connection_id=embedded,
            )
        )

    return records, diagnostics


def group_records(
    records: Iterable[RawRecord],
) -> "OrderedDict[str, List[RawRecord]]":
    """Group records by connection while preserving input arrival order."""

    groups: "OrderedDict[str, List[RawRecord]]" = OrderedDict()
    for record in records:
        groups.setdefault(record.connection_id, []).append(record)
    for conn_id in groups:
        groups[conn_id].sort(key=lambda r: (r.line_no, r.arrival_index))
    return groups


def records_sha256(records: Iterable[RawRecord]) -> str:
    from .models import digest_value

    return digest_value(
        [
            {
                "connection_id": r.connection_id,
                "direction": r.direction,
                "record_seq": r.record_seq,
                "timestamp_ms": r.timestamp_ms,
                "record_type": r.record_type,
                "payload": r.payload_hex,
                "line_no": r.line_no,
            }
            for r in records
        ]
    )
