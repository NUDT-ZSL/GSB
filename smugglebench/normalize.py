"""Strict JSONL/JSON/SQLite normalization for the local sample workspace."""

from __future__ import annotations

import json
import re
import sqlite3
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable

from .codes import E_INPUT, E_SCHEMA
from .models import (
    FlowRecord,
    MutationSpec,
    Rejection,
    Rule,
    TransitionConstraint,
    WorkspaceData,
)
from .util import BusinessError, canonical_rule_name, read_json

DIRECTION_ALIASES = {
    "c2s": "C2S",
    "client_to_server": "C2S",
    "client-to-server": "C2S",
    "request": "C2S",
    "req": "C2S",
    "upstream": "C2S",
    "s2c": "S2C",
    "server_to_client": "S2C",
    "server-to-client": "S2C",
    "response": "S2C",
    "resp": "S2C",
    "downstream": "S2C",
}

STATE_ALIASES = {
    "START": "START_LINE",
    "STARTLINE": "START_LINE",
    "REQUEST_LINE": "START_LINE",
    "HEADER": "HEADERS",
    "HEADER_FIELDS": "HEADERS",
    "BODY": "BODY",
    "CHUNK": "CHUNK_SIZE",
    "CHUNK_LENGTH": "CHUNK_SIZE",
    "TRAILER": "TRAILERS",
    "END": "MESSAGE_END",
    "DONE": "MESSAGE_END",
}

EVENT_ALIASES = {
    "HEADER": "HEADER_FIELD",
    "HEADERS": "HEADERS_DONE",
    "BODY": "BODY_COMPLETE",
    "CHUNK": "CHUNK_SIZE",
    "CHUNK_LENGTH": "CHUNK_SIZE",
    "TRAILER": "TRAILER_FIELD",
    "END": "MESSAGE_END",
    "DONE": "MESSAGE_END",
}

ACTION_ALIASES = {
    "BLOCK": "REJECT",
    "DENY": "REJECT",
    "DROP": "REJECT",
    "TERMINATE": "REJECT",
    "QUARANTINE": "REJECT",
    "ALERT": "FLAG",
    "WARN": "FLAG",
    "MARK": "FLAG",
}

_CONDITION_LIST_KEYS = {
    "header_name",
    "header_names",
    "header_name_any",
    "header_names_any",
    "header_name_all",
    "header_names_all",
    "also_header",
    "state",
    "states",
    "event",
    "framing",
    "anomaly",
    "anomaly_any",
}


def _field(
    record: dict[str, Any],
    names: Iterable[str],
    *,
    default: Any = None,
) -> Any:
    for name in names:
        if name in record:
            return record[name]
    folded = {name.casefold() for name in names}
    for key, value in record.items():
        if str(key).casefold() in folded:
            return value
    return default


def _as_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("must be an integer")
    return value


def _as_bool(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().casefold()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    raise ValueError("must be a boolean")


def _as_string_list(value: Any, *, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        if not value.strip():
            return ()
        return tuple(part.strip() for part in re.split(r"[,;]", value) if part.strip())
    if isinstance(value, (list, tuple)) and all(isinstance(item, str) for item in value):
        return tuple(item.strip() for item in value if item.strip())
    raise ValueError(f"{field_name} must be a string or string list")


def _normalize_direction(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("direction must be a string")
    key = value.strip().casefold().replace(" ", "_")
    if key not in DIRECTION_ALIASES:
        raise ValueError(f"unsupported direction {value!r}")
    return DIRECTION_ALIASES[key]


def _normalize_state(value: str) -> str:
    key = value.strip().upper().replace("-", "_")
    return STATE_ALIASES.get(key, key)


def _normalize_event(value: str) -> str:
    key = value.strip().upper().replace("-", "_")
    return EVENT_ALIASES.get(key, key)


def _normalize_action(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        return "FLAG"
    key = value.strip().upper().replace("-", "_")
    return ACTION_ALIASES.get(key, key)


def _parse_flat_match(match: Any) -> dict[str, Any]:
    if isinstance(match, dict):
        return dict(match)
    if not isinstance(match, str) or not match.strip():
        raise ValueError("match must be a non-empty string or object")
    text = match.strip()
    if text.startswith("{"):
        parsed = json.loads(text)
        if not isinstance(parsed, dict):
            raise ValueError("JSON match must be an object")
        return parsed
    if "=" in text:
        result: dict[str, Any] = {}
        for part in re.split(r"[;|]", text):
            if not part.strip():
                continue
            if "=" not in part:
                raise ValueError(f"malformed match token {part!r}")
            key, value = part.split("=", 1)
            key = re.sub(r"[-\s]+", "_", key.strip()).casefold()
            result[key] = value.strip()
        return result

    lowered = text.casefold()
    if "content-length" in lowered and "transfer-encoding" in lowered:
        return {
            "state": "HEADERS",
            "event": "HEADERS_DONE",
            "header_names_all": ["content-length", "transfer-encoding"],
            "action": "REJECT",
            "code": "D_TE_CL",
        }
    raise ValueError(
        "flat match must be a state/event/header/action DSL; raw byte matching is not supported"
    )


def _normalize_conditions(raw: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in raw.items():
        normalized_key = re.sub(r"[-\s]+", "_", str(key)).casefold()
        if normalized_key in {"state", "states", "event"}:
            continue
        if normalized_key in {"action", "code"}:
            continue
        if normalized_key == "header":
            normalized_key = "header_names_any"
        if normalized_key == "also_header":
            existing = result.setdefault("header_names_all", [])
            if isinstance(existing, str):
                existing = [existing]
                result["header_names_all"] = existing
            values = _as_string_list(value, field_name="also_header")
            existing.extend(item.casefold() for item in values)
            continue
        if normalized_key in _CONDITION_LIST_KEYS:
            values = _as_string_list(value, field_name=normalized_key)
            if normalized_key in {"state", "states"}:
                result["from_states"] = [_normalize_state(item) for item in values]
            elif normalized_key == "event":
                result["event"] = _normalize_event(values[0]) if values else None
            elif normalized_key.startswith("header_name"):
                result[normalized_key] = [item.casefold() for item in values]
            elif normalized_key == "framing":
                result[normalized_key] = [item.upper() for item in values]
            else:
                result[normalized_key] = list(values)
        elif normalized_key in {
            "duplicate_header",
            "duplicate",
            "same_name_header",
            "repeated_header",
            "trailer_present",
        }:
            target_key = (
                "duplicate_header"
                if normalized_key
                in {
                    "duplicate_header",
                    "duplicate",
                    "same_name_header",
                    "repeated_header",
                }
                else normalized_key
            )
            result[target_key] = _as_bool(value)
        elif normalized_key in {
            "chunk_pattern",
            "body_pattern",
            "framing_pattern",
            "te_token",
            "method",
            "request_target",
        }:
            result[normalized_key] = str(value)
        else:
            result[normalized_key] = value
    return result


def _parse_transitions(value: Any) -> tuple[TransitionConstraint, ...]:
    if value is None:
        raise ValueError("transition constraints are required")
    if isinstance(value, str):
        value = json.loads(value)
    if isinstance(value, dict) and "transitions" in value:
        value = value["transitions"]
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list) or not value:
        raise ValueError("transitions must be a non-empty list")

    transitions: list[TransitionConstraint] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(f"transition {index} must be an object")
        raw_from = _field(
            item,
            ("from_states", "states", "from", "state", "from_state"),
            default=[],
        )
        from_values = _as_string_list(raw_from, field_name="from_states")
        if not from_values:
            raise ValueError(f"transition {index} has no from state")
        raw_event = _field(item, ("event", "on", "transition_event"))
        event = _normalize_event(raw_event) if isinstance(raw_event, str) else None
        raw_conditions = _field(
            item,
            ("conditions", "when", "match", "constraints"),
            default={},
        )
        if isinstance(raw_conditions, str):
            raw_conditions = _parse_flat_match(raw_conditions)
        if not isinstance(raw_conditions, dict):
            raise ValueError(f"transition {index} conditions must be an object")
        conditions = _normalize_conditions(raw_conditions)
        if "from_states" in conditions:
            from_values = tuple(conditions.pop("from_states"))
        if conditions.get("event") is not None:
            event = conditions.pop("event")
        action = _normalize_action(_field(item, ("action", "verdict"), default="FLAG"))
        code = str(_field(item, ("code", "business_code", "discrepancy"), default="RULE_HIT"))
        transitions.append(
            TransitionConstraint(
                from_states=tuple(_normalize_state(state) for state in from_values),
                event=event,
                conditions=conditions,
                action=action,
                code=code,
                index=index,
            )
        )
    return tuple(transitions)


def _parse_transitions_from_match(match: Any) -> tuple[TransitionConstraint, ...]:
    parsed = _parse_flat_match(match)
    states = [
        _normalize_state(item)
        for item in _as_string_list(
            _field(parsed, ("state", "states", "from_states"), default="HEADERS"),
            field_name="state",
        )
    ]
    event_value = _field(parsed, ("event", "on"), default="HEADERS_DONE")
    event = _normalize_event(str(event_value))
    action = _normalize_action(_field(parsed, ("action", "verdict"), default="FLAG"))
    code = str(_field(parsed, ("code", "business_code"), default="RULE_HIT"))
    conditions = _normalize_conditions(parsed)
    if "from_states" in conditions:
        states = conditions.pop("from_states")
    if conditions.get("event") is not None:
        event = conditions.pop("event")
    return (
        TransitionConstraint(
            from_states=tuple(states),
            event=event,
            conditions=conditions,
            action=action,
            code=code,
            index=0,
        ),
    )


def _relative_seconds_to_ms(value: Any, clock_ms: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError("relative effective seconds must be numeric")
    try:
        seconds = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("relative effective seconds must be numeric") from exc
    if not seconds.is_finite() or seconds < 0:
        raise ValueError("relative effective seconds must be non-negative")
    delta_ms = int((seconds * Decimal(1000)).to_integral_value(rounding=ROUND_HALF_UP))
    return clock_ms + delta_ms


def _record_rule(
    *,
    record: dict[str, Any],
    source_kind: str,
    source_file: str,
    record_index: int,
    clock_ms: int,
    defaults: dict[str, Any] | None = None,
) -> Rule:
    defaults = defaults or {}
    name = _field(record, ("canonical_name", "rule_name", "name", "rule", "id"))
    if not isinstance(name, str) or not name.strip():
        raise ValueError("rule name must be a non-empty string")
    canonical = canonical_rule_name(name)
    if not canonical:
        raise ValueError("canonical rule name is empty")

    generation_value = _field(
        record,
        ("generation", "rule_generation", "gen", "version"),
        default=defaults.get("generation", 1),
    )
    generation = _as_int(generation_value)
    if generation < 0:
        raise ValueError("rule generation must be non-negative")

    relative = _field(
        record,
        (
            "relative_effective_s",
            "relative_effective_seconds",
            "effective_relative_seconds",
            "effective_s",
            "offset_s",
        ),
    )
    absolute_from = _field(
        record,
        ("effective_from_ms", "from_ms", "start_ms", "effective_at_ms"),
    )
    if absolute_from is None and relative is not None:
        if clock_ms < 0:
            raise ValueError("sample observed clock is required for relative v1 rules")
        effective_from_ms = _relative_seconds_to_ms(relative, clock_ms)
    elif absolute_from is not None:
        effective_from_ms = _as_int(absolute_from)
        if effective_from_ms < 0:
            raise ValueError("effective_from_ms must be non-negative")
    else:
        effective_from_ms = clock_ms

    to_value = _field(
        record,
        ("effective_to_ms", "to_ms", "end_ms", "effective_until_ms"),
    )
    effective_to_ms = None if to_value is None else _as_int(to_value)
    if effective_to_ms is not None and effective_to_ms < effective_from_ms:
        raise ValueError("effective_to_ms precedes effective_from_ms")

    enabled = _as_bool(
        _field(record, ("enabled", "is_enabled"), default=defaults.get("enabled", True))
    )
    tombstone = _as_bool(
        _field(
            record,
            ("tombstone", "is_tombstone", "deleted"),
            default=defaults.get("tombstone", False),
        )
    )
    depends_on = tuple(
        canonical_rule_name(item)
        for item in _as_string_list(
            _field(record, ("depends_on", "dependencies", "depends")),
            field_name="depends_on",
        )
    )

    transitions_value = _field(
        record,
        ("transitions", "transition_constraints", "constraints"),
    )
    if transitions_value is None:
        transitions_value = defaults.get("transitions")
    if transitions_value is None:
        match = _field(record, ("match", "match_string", "selector"))
        if match is None and tombstone:
            transitions = ()
        elif match is None:
            raise ValueError("rule has neither transitions nor a match selector")
        else:
            transitions = _parse_transitions_from_match(match)
    else:
        transitions = _parse_transitions(transitions_value)

    return Rule(
        canonical_name=canonical,
        display_name=name,
        generation=generation,
        effective_from_ms=effective_from_ms,
        effective_to_ms=effective_to_ms,
        enabled=enabled,
        tombstone=tombstone,
        transitions=transitions,
        source_kind=source_kind,
        source_file=source_file,
        record_index=record_index,
        depends_on=depends_on,
        raw_match=str(_field(record, ("match", "match_string"), default=""))
        or None,
    )


def _load_flow_file(
    path: Path,
    *,
    start_arrival: int,
    rejections: list[Rejection],
) -> tuple[list[FlowRecord], int]:
    flows: list[FlowRecord] = []
    arrival = start_arrival
    with path.open("rb") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            record_index = line_number - 1
            if not raw_line.strip():
                continue
            try:
                decoded = json.loads(raw_line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                rejections.append(
                    Rejection(
                        source_file=str(path),
                        record_index=record_index,
                        line_number=line_number,
                        code=E_INPUT,
                        message=f"invalid JSON: {exc}",
                    )
                )
                continue
            if not isinstance(decoded, dict):
                rejections.append(
                    Rejection(
                        source_file=str(path),
                        record_index=record_index,
                        line_number=line_number,
                        code=E_INPUT,
                        message="flow record must be a JSON object",
                    )
                )
                continue
            try:
                connection_id = _field(
                    decoded,
                    ("connection_id", "connection", "conn_id", "stream_id"),
                )
                if not isinstance(connection_id, str) or not connection_id.strip():
                    raise ValueError("connection_id must be a non-empty string")
                if any(ord(character) < 32 for character in connection_id):
                    raise ValueError("connection_id contains control characters")
                direction = _normalize_direction(
                    _field(decoded, ("direction", "dir", "flow_direction"))
                )
                seq = _as_int(_field(decoded, ("seq", "sequence", "message_seq", "packet_seq")))
                if seq < 0:
                    raise ValueError("seq must be non-negative")
                logical_ms = _as_int(
                    _field(
                        decoded,
                        ("logical_ms", "logical_timestamp_ms", "timestamp_ms", "logical_time_ms"),
                    )
                )
                if logical_ms < 0:
                    raise ValueError("logical_ms must be non-negative")
                payload_hex = _field(
                    decoded,
                    ("payload_hex", "payload", "hex", "raw_hex"),
                )
                if not isinstance(payload_hex, str):
                    raise ValueError("payload_hex must be a string")
                compact_hex = "".join(payload_hex.split())
                if len(compact_hex) % 2 != 0:
                    raise ValueError("payload_hex length must be even")
                try:
                    payload = bytes.fromhex(compact_hex)
                except ValueError as exc:
                    raise ValueError("payload_hex is not valid hexadecimal") from exc
                hint = _field(decoded, ("payload_connection_id", "connection_hint"))
                if hint is not None and (
                    not isinstance(hint, str) or not hint.strip()
                ):
                    raise ValueError("payload_connection_id must be a non-empty string")
            except (ValueError, TypeError) as exc:
                rejections.append(
                    Rejection(
                        source_file=str(path),
                        record_index=record_index,
                        line_number=line_number,
                        code=E_INPUT,
                        message=str(exc),
                        raw=decoded,
                    )
                )
                continue

            flows.append(
                FlowRecord(
                    connection_id=connection_id,
                    direction=direction,
                    seq=seq,
                    logical_ms=logical_ms,
                    payload=payload,
                    source_file=str(path),
                    record_index=record_index,
                    line_number=line_number,
                    arrival_index=arrival,
                    connection_hint=hint,
                )
            )
            arrival += 1
    return flows, arrival


def _load_v1(
    path: Path,
    *,
    clock_ms: int,
    rejections: list[Rejection],
) -> list[Rule]:
    try:
        payload = read_json(path)
    except (OSError, json.JSONDecodeError) as exc:
        rejections.append(
            Rejection(
                source_file=str(path),
                record_index=0,
                line_number=1,
                code=E_SCHEMA,
                message=f"cannot read v1 rule snapshot: {exc}",
            )
        )
        return []
    if isinstance(payload, dict):
        records = _field(payload, ("rules", "items", "snapshot"))
        if records is None:
            if _field(payload, ("rule_name", "name", "rule", "id")) is not None:
                records = [payload]
            else:
                records = []
    else:
        records = payload
    if not isinstance(records, list):
        rejections.append(
            Rejection(
                source_file=str(path),
                record_index=0,
                line_number=1,
                code=E_SCHEMA,
                message="v1 snapshot must be a rule list or an object containing rules",
            )
        )
        return []
    rules: list[Rule] = []
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            rejections.append(
                Rejection(
                    source_file=str(path),
                    record_index=index,
                    line_number=index + 1,
                    code=E_SCHEMA,
                    message="v1 rule must be an object",
                )
            )
            continue
        try:
            rules.append(
                _record_rule(
                    record=record,
                    source_kind="v1",
                    source_file=str(path),
                    record_index=index,
                    clock_ms=clock_ms,
                )
            )
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            rejections.append(
                Rejection(
                    source_file=str(path),
                    record_index=index,
                    line_number=index + 1,
                    code=E_SCHEMA,
                    message=str(exc),
                    raw=record,
                )
            )
    return rules


def _identity_key(record: dict[str, Any]) -> tuple[str, int, int] | None:
    name = _field(record, ("canonical_name", "rule_name", "name", "rule", "id"))
    generation = _field(record, ("generation", "rule_generation", "gen", "version"))
    effective_from = _field(
        record,
        ("effective_from_ms", "from_ms", "start_ms", "effective_at_ms"),
    )
    if not isinstance(name, str) or generation is None or effective_from is None:
        return None
    try:
        return canonical_rule_name(name), _as_int(generation), _as_int(effective_from)
    except ValueError:
        return None


def _load_v2_shards(
    paths: list[Path],
    *,
    clock_ms: int,
    rejections: list[Rejection],
) -> dict[tuple[str, int, int], dict[str, Any]]:
    by_identity: dict[tuple[str, int, int], dict[str, Any]] = {}
    for path in paths:
        try:
            payload = read_json(path)
        except (OSError, json.JSONDecodeError) as exc:
            rejections.append(
                Rejection(
                    source_file=str(path),
                    record_index=0,
                    line_number=1,
                    code=E_SCHEMA,
                    message=f"cannot read v2 shard: {exc}",
                )
            )
            continue
        records: Any
        if isinstance(payload, dict):
            records = _field(payload, ("rules", "items", "shard"))
            if records is None:
                records = [payload]
        elif isinstance(payload, list):
            records = payload
        else:
            records = []
        if not isinstance(records, list):
            rejections.append(
                Rejection(
                    source_file=str(path),
                    record_index=0,
                    line_number=1,
                    code=E_SCHEMA,
                    message="v2 shard must contain a rule list",
                )
            )
            continue
        for index, record in enumerate(records):
            if not isinstance(record, dict):
                rejections.append(
                    Rejection(
                        source_file=str(path),
                        record_index=index,
                        line_number=index + 1,
                        code=E_SCHEMA,
                        message="v2 shard rule must be an object",
                    )
                )
                continue
            identity = _identity_key(record)
            if identity is None:
                rejections.append(
                    Rejection(
                        source_file=str(path),
                        record_index=index,
                        line_number=index + 1,
                        code=E_SCHEMA,
                        message=(
                            "v2 shard rule needs canonical name, generation and "
                            "absolute effective_from_ms"
                        ),
                        raw=record,
                    )
                )
                continue
            if identity in by_identity:
                rejections.append(
                    Rejection(
                        source_file=str(path),
                        record_index=index,
                        line_number=index + 1,
                        code=E_SCHEMA,
                        message=f"duplicate v2 shard identity {identity}",
                        raw=record,
                    )
                )
                continue
            by_identity[identity] = {
                "record": record,
                "source_file": str(path),
                "record_index": index,
            }
    return by_identity


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    rows = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
    return {str(row[1]).casefold() for row in rows}


def _load_v2_database(
    path: Path,
    shards: dict[tuple[str, int, int], dict[str, Any]],
    *,
    clock_ms: int,
    rejections: list[Rejection],
) -> list[Rule]:
    uri = path.as_uri() + "?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise BusinessError(
            E_SCHEMA,
            "cannot open v2 SQLite metadata",
            {"source_file": str(path), "error": str(exc)},
        ) from exc
    try:
        tables = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        ]
        table = None
        table_by_folded = {candidate.casefold(): candidate for candidate in tables}
        for candidate in ("rules", "rule_metadata", "v2_rules"):
            if candidate in table_by_folded:
                table = table_by_folded[candidate]
                break
        if table is None:
            for candidate in tables:
                columns = _table_columns(connection, candidate)
                if {"canonical_name", "generation"} <= columns:
                    table = candidate
                    break
        if table is None:
            raise BusinessError(
                E_SCHEMA,
                "v2 SQLite database has no rules table",
                {"source_file": str(path), "tables": tables},
            )
        columns = _table_columns(connection, table)
        rows = connection.execute(f'SELECT * FROM "{table}"').fetchall()
        names = [description[0] for description in connection.execute(
            f'SELECT * FROM "{table}" LIMIT 0'
        ).description]
    except sqlite3.Error as exc:
        raise BusinessError(
            E_SCHEMA,
            "cannot read v2 SQLite metadata",
            {"source_file": str(path), "error": str(exc)},
        ) from exc
    finally:
        pass

    rules: list[Rule] = []
    matched_shards: set[tuple[str, int, int]] = set()
    try:
        for row_index, row in enumerate(rows):
            raw = {str(name): row[position] for position, name in enumerate(names)}
            lowered = {key.casefold(): value for key, value in raw.items()}

            def get(*keys: str, default: Any = None) -> Any:
                for key in keys:
                    if key.casefold() in lowered:
                        return lowered[key.casefold()]
                return default

            identity = _identity_key(raw)
            if identity is None:
                rejections.append(
                    Rejection(
                        source_file=str(path),
                        record_index=row_index,
                        line_number=row_index + 1,
                        code=E_SCHEMA,
                        message=(
                            "v2 metadata row needs canonical_name, generation and "
                            "effective_from_ms"
                        ),
                        raw=raw,
                    )
                )
                continue
            merged = dict(raw)
            shard = shards.get(identity)
            shard_source_file = str(path)
            shard_record_index = row_index
            if shard is not None:
                matched_shards.add(identity)
                shard_source_file = shard["source_file"]
                shard_record_index = shard["record_index"]
                for key, value in shard["record"].items():
                    if key not in merged or merged[key] is None:
                        merged[key] = value
                if get("transitions_json", "transitions", "constraints") is None:
                    merged["transitions"] = shard["record"].get(
                        "transitions",
                        shard["record"].get("transition_constraints"),
                    )
            transitions_json = get("transitions_json", "transitions", "constraints")
            if "transitions" not in merged and transitions_json is not None:
                merged["transitions"] = transitions_json

            merged["canonical_name"] = identity[0]
            merged["rule_name"] = get("display_name", "rule_name", "name", default=identity[0])
            merged["generation"] = identity[1]
            merged["effective_from_ms"] = identity[2]
            if get("effective_to_ms", "to_ms", "end_ms") is not None:
                merged["effective_to_ms"] = get("effective_to_ms", "to_ms", "end_ms")
            merged["enabled"] = get("enabled", "is_enabled", default=1)
            merged["tombstone"] = get("tombstone", "is_tombstone", "deleted", default=0)
            if get("depends_on", "dependencies") is not None:
                merged["depends_on"] = get("depends_on", "dependencies")
            try:
                rules.append(
                    _record_rule(
                        record=merged,
                        source_kind="v2",
                        source_file=shard_source_file,
                        record_index=shard_record_index,
                        clock_ms=clock_ms,
                    )
                )
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                rejections.append(
                    Rejection(
                        source_file=shard_source_file,
                        record_index=shard_record_index,
                        line_number=shard_record_index + 1,
                        code=E_SCHEMA,
                        message=str(exc),
                        raw=raw,
                    )
                )

        for identity, shard in sorted(shards.items()):
            if identity in matched_shards:
                continue
            try:
                rules.append(
                    _record_rule(
                        record=shard["record"],
                        source_kind="v2",
                        source_file=shard["source_file"],
                        record_index=shard["record_index"],
                        clock_ms=clock_ms,
                    )
                )
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                rejections.append(
                    Rejection(
                        source_file=shard["source_file"],
                        record_index=shard["record_index"],
                        line_number=shard["record_index"] + 1,
                        code=E_SCHEMA,
                        message=str(exc),
                        raw=shard["record"],
                    )
                )
    finally:
        connection.close()
    return rules


def _load_mutations(
    path: Path,
    *,
    seed: int,
    rejections: list[Rejection],
) -> list[MutationSpec]:
    try:
        payload = read_json(path)
    except (OSError, json.JSONDecodeError) as exc:
        raise BusinessError(
            E_INPUT,
            "cannot read mutation manifest",
            {"source_file": str(path), "error": str(exc)},
        ) from exc
    if isinstance(payload, dict):
        records = _field(payload, ("mutations", "items", "cases"))
    else:
        records = payload
    if not isinstance(records, list):
        raise BusinessError(
            E_INPUT,
            "mutation manifest must contain a list",
            {"source_file": str(path)},
        )
    mutations: list[MutationSpec] = []
    seen_ids: set[str] = set()
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            rejections.append(
                Rejection(
                    source_file=str(path),
                    record_index=index,
                    line_number=index + 1,
                    code=E_INPUT,
                    message="mutation must be an object",
                )
            )
            continue
        try:
            mutation_id = _field(record, ("mutation_id", "id", "case_id", "name"))
            if not isinstance(mutation_id, str) or not mutation_id.strip():
                raise ValueError("mutation_id must be a non-empty string")
            if mutation_id in seen_ids:
                raise ValueError(f"duplicate mutation_id {mutation_id!r}")
            connection_id = _field(
                record,
                ("connection_id", "connection", "conn_id"),
            )
            if not isinstance(connection_id, str) or not connection_id.strip():
                raise ValueError("mutation connection_id must be a non-empty string")
            request_index = _as_int(
                _field(record, ("request_index", "message_index", "ordinal"), default=0)
            )
            if request_index < 0:
                raise ValueError("request_index must be non-negative")
            target = _field(record, ("target", "mutation_target"))
            if not isinstance(target, str) or not target.strip():
                raise ValueError("mutation target must be a non-empty string")
            operation = _field(record, ("operation", "op", "kind"), default="")
            if not isinstance(operation, str):
                raise ValueError("mutation operation must be a string")
            parameters = _field(record, ("parameters", "params", "args"), default={})
            if parameters is None:
                parameters = {}
            if not isinstance(parameters, dict):
                raise ValueError("mutation parameters must be an object")
            delay_ms = _as_int(
                _field(record, ("delay_ms", "echo_delay_ms", "profile_delay_ms"), default=0)
            )
            if delay_ms < 0:
                raise ValueError("delay_ms must be non-negative")
            mutation_seed = _as_int(
                _field(record, ("seed", "mutation_seed"), default=seed)
            )
        except (ValueError, TypeError) as exc:
            rejections.append(
                Rejection(
                    source_file=str(path),
                    record_index=index,
                    line_number=index + 1,
                    code=E_INPUT,
                    message=str(exc),
                    raw=record,
                )
            )
            continue
        seen_ids.add(mutation_id)
        mutations.append(
            MutationSpec(
                mutation_id=mutation_id,
                connection_id=connection_id,
                request_index=request_index,
                target=target.strip().casefold().replace("-", "_"),
                operation=(
                    operation.strip().casefold().replace("-", "_")
                    if operation.strip()
                    else target.strip().casefold().replace("-", "_")
                ),
                parameters=dict(parameters),
                delay_ms=delay_ms,
                source_file=str(path),
                record_index=index,
                seed=mutation_seed,
            )
        )
    return mutations


def _discover_meta(root: Path) -> Path | None:
    for name in (
        "sample_meta.json",
        "metadata.json",
        "sample.json",
        "manifest.json",
    ):
        candidate = root / name
        if candidate.is_file():
            return candidate
    return None


def _discover_flow_files(root: Path) -> list[Path]:
    preferred = [root / "flows.jsonl", root / "flow.jsonl"]
    found = [path for path in preferred if path.is_file()]
    if found:
        return found
    return sorted(
        path
        for path in root.glob("*.jsonl")
        if "mutation" not in path.name.casefold()
    )


def _discover_v2(root: Path) -> tuple[Path | None, list[Path]]:
    database_candidates = [
        root / "rules_v2.sqlite",
        root / "rules_v2.sqlite3",
        root / "rules_v2.db",
        root / "rules.sqlite",
    ]
    database = next((path for path in database_candidates if path.is_file()), None)
    if database is None:
        database = next(
            iter(sorted(root.glob("*.sqlite*")) + sorted(root.glob("rules_v2/*.sqlite*"))),
            None,
        )
    shard_dir = root / "rules_v2"
    shards = sorted(shard_dir.glob("*.json")) if shard_dir.is_dir() else []
    if not shards:
        shards = sorted(
            path
            for path in root.glob("rules_v2*.json")
            if path.name not in {"rules_v1.json"}
        )
    return database, shards


def load_workspace(root: str | Path) -> WorkspaceData:
    workspace = Path(root).resolve()
    if not workspace.is_dir():
        raise BusinessError(
            E_INPUT,
            "sample workspace does not exist",
            {"root": str(workspace)},
        )

    rejections: list[Rejection] = []
    meta_path = _discover_meta(workspace)
    meta: dict[str, Any] = {}
    if meta_path is not None:
        try:
            loaded_meta = read_json(meta_path)
            if not isinstance(loaded_meta, dict):
                raise ValueError("metadata must be an object")
            meta = loaded_meta
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            rejections.append(
                Rejection(
                    source_file=str(meta_path),
                    record_index=0,
                    line_number=1,
                    code=E_SCHEMA,
                    message=f"invalid sample metadata: {exc}",
                )
            )

    clock_value = _field(
        meta,
        ("observed_clock_ms", "observation_clock_ms", "clock_ms"),
    )
    if clock_value is None:
        fallback_v1 = next(
            (
                path
                for path in (
                    workspace / "rules_v1.json",
                    workspace / "rules-v1.json",
                    workspace / "rules.v1.json",
                )
                if path.is_file()
            ),
            None,
        )
        if fallback_v1 is not None:
            try:
                fallback_payload = read_json(fallback_v1)
                if isinstance(fallback_payload, dict):
                    clock_value = _field(
                        fallback_payload,
                        ("observed_clock_ms", "observation_clock_ms", "clock_ms"),
                    )
            except (OSError, json.JSONDecodeError):
                pass
    if clock_value is None:
        raise BusinessError(
            E_INPUT,
            "sample metadata must declare observed_clock_ms; system time is never used",
            {"metadata_file": str(meta_path) if meta_path else None},
        )
    try:
        clock_ms = _as_int(clock_value)
        if clock_ms < 0:
            raise ValueError("observed_clock_ms must be non-negative")
    except ValueError as exc:
        raise BusinessError(
            E_INPUT,
            "invalid observed clock",
            {"metadata_file": str(meta_path), "error": str(exc)},
        ) from exc

    seed_value = _field(meta, ("seed", "sample_seed"), default=0)
    try:
        sample_seed = _as_int(seed_value)
    except ValueError as exc:
        raise BusinessError(E_INPUT, "invalid sample seed", {"error": str(exc)}) from exc

    flow_files = _discover_flow_files(workspace)
    if not flow_files:
        raise BusinessError(
            E_INPUT,
            "no JSONL flow file found",
            {"root": str(workspace)},
        )
    flows: list[FlowRecord] = []
    arrival = 0
    source_files: dict[str, str] = {}
    for flow_file in flow_files:
        source_files.setdefault("flows", str(flow_file))
        loaded, arrival = _load_flow_file(
            flow_file,
            start_arrival=arrival,
            rejections=rejections,
        )
        flows.extend(loaded)

    v1_candidates = [
        workspace / "rules_v1.json",
        workspace / "rules-v1.json",
        workspace / "rules.v1.json",
    ]
    v1_path = next((path for path in v1_candidates if path.is_file()), v1_candidates[0])
    rules: list[Rule] = []
    if v1_path.is_file():
        source_files["rules_v1"] = str(v1_path)
        rules.extend(_load_v1(v1_path, clock_ms=clock_ms, rejections=rejections))

    database_path, shard_paths = _discover_v2(workspace)
    if database_path is not None:
        source_files["rules_v2_db"] = str(database_path)
        shards = _load_v2_shards(
            shard_paths,
            clock_ms=clock_ms,
            rejections=rejections,
        )
        rules.extend(
            _load_v2_database(
                database_path,
                shards,
                clock_ms=clock_ms,
                rejections=rejections,
            )
        )
    elif shard_paths:
        source_files["rules_v2_shards"] = ",".join(str(path) for path in shard_paths)
        shards = _load_v2_shards(
            shard_paths,
            clock_ms=clock_ms,
            rejections=rejections,
        )
        for identity, shard in sorted(shards.items()):
            try:
                rules.append(
                    _record_rule(
                        record=shard["record"],
                        source_kind="v2",
                        source_file=shard["source_file"],
                        record_index=shard["record_index"],
                        clock_ms=clock_ms,
                    )
                )
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                rejections.append(
                    Rejection(
                        source_file=shard["source_file"],
                        record_index=shard["record_index"],
                        line_number=shard["record_index"] + 1,
                        code=E_SCHEMA,
                        message=str(exc),
                    )
                )

    if not rules and not (v1_path.is_file() or database_path is not None or shard_paths):
        raise BusinessError(
            E_INPUT,
            "no v1 or v2 rule snapshot found",
            {"root": str(workspace)},
        )

    mutation_candidates = [
        workspace / "mutations.json",
        workspace / "mutation_manifest.json",
        workspace / "mutations" / "mutations.json",
    ]
    mutation_path = next(
        (path for path in mutation_candidates if path.is_file()),
        mutation_candidates[0],
    )
    if not mutation_path.is_file():
        raise BusinessError(
            E_INPUT,
            "mutation manifest not found",
            {"expected": str(mutation_path)},
        )
    source_files["mutations"] = str(mutation_path)
    mutations = _load_mutations(
        mutation_path,
        seed=sample_seed,
        rejections=rejections,
    )

    return WorkspaceData(
        root=str(workspace),
        observed_clock_ms=clock_ms,
        flows=tuple(flows),
        mutations=tuple(mutations),
        rules=tuple(rules),
        rejections=tuple(rejections),
        sample_seed=sample_seed,
        source_files=source_files,
    )


def rejections_to_dict(rejections: Iterable[Rejection]) -> list[dict[str, Any]]:
    return [rejection.to_dict() for rejection in rejections]
