from __future__ import annotations

import base64
import copy
import json
from pathlib import Path
from typing import Any

from .canonical import digest_without_fields
from .errors import (
    CLOCK_001,
    EVENT_SEQ_001,
    FLOW_INITIAL_001,
    FRAME_001,
    GOAWAY_001,
    HEADER_INCOMPLETE_001,
    HEADER_ORDER_001,
    PATCH_DIGEST_001,
    PATCH_JSON_001,
    PATCH_OP_001,
    PATCH_SCHEMA_001,
    PING_001,
    SCENARIO_JSON_001,
    SCENARIO_SCHEMA_001,
    SETTINGS_001,
    STREAM_ID_001,
    STREAM_PARITY_001,
    WEIGHT_001,
    WINDOW_UPDATE_001,
    error,
)

MAX_STREAM_ID = 2**31 - 1
MAX_WINDOW = 2**31 - 1
VALID_STATES = {
    "idle",
    "open",
    "half-closed-local",
    "half-closed-remote",
    "closed",
}
VALID_TYPES = {
    "HEADERS",
    "CONTINUATION",
    "DATA",
    "RST_STREAM",
    "WINDOW_UPDATE",
    "SETTINGS",
    "SETTINGS_ACK",
    "PING",
    "PING_ACK",
    "GOAWAY",
}
DEFAULT_DIRECTIONS = {
    "HEADERS": "send",
    "CONTINUATION": "send",
    "DATA": "send",
    "RST_STREAM": "recv",
    "WINDOW_UPDATE": "recv",
    "SETTINGS": "recv",
    "SETTINGS_ACK": "send",
    "PING": "send",
    "PING_ACK": "recv",
    "GOAWAY": "send",
}


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _int(
    value: Any,
    *,
    field: str,
    code: str,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    if not _is_int(value):
        raise error(code, f"{field} must be an integer", details={"field": field})
    if minimum is not None and value < minimum:
        raise error(
            code,
            f"{field} must be at least {minimum}",
            details={"field": field, "value": value, "minimum": minimum},
        )
    if maximum is not None and value > maximum:
        raise error(
            code,
            f"{field} must be at most {maximum}",
            details={"field": field, "value": value, "maximum": maximum},
        )
    return value


def _decode_fragment(event: dict[str, Any], seq: int) -> str:
    if "fragment_hex" in event:
        fragment_hex = event["fragment_hex"]
        if not isinstance(fragment_hex, str):
            raise error(
                FRAME_001,
                "fragment_hex must be a string",
                details={"seq": seq},
            )
        try:
            bytes.fromhex(fragment_hex)
        except ValueError as exc:
            raise error(
                FRAME_001,
                "fragment_hex is not valid hexadecimal",
                details={"seq": seq},
            ) from exc
        return fragment_hex.lower()
    fragment = event.get("fragment", "")
    encoding = event.get("fragment_encoding", "utf-8")
    if not isinstance(fragment, str):
        raise error(FRAME_001, "fragment must be a string", details={"seq": seq})
    if encoding in {"utf-8", "utf8", "text"}:
        raw = fragment.encode("utf-8")
    elif encoding == "hex":
        try:
            raw = bytes.fromhex(fragment)
        except ValueError as exc:
            raise error(FRAME_001, "hex fragment is invalid", details={"seq": seq}) from exc
    elif encoding == "base64":
        try:
            raw = base64.b64decode(fragment, validate=True)
        except ValueError as exc:
            raise error(FRAME_001, "base64 fragment is invalid", details={"seq": seq}) from exc
    else:
        raise error(
            FRAME_001,
            "unsupported fragment encoding",
            details={"seq": seq, "encoding": encoding},
        )
    return raw.hex()


def _normalize_settings(value: Any, *, code: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise error(code, "SETTINGS payload must be an object")
    normalized = copy.deepcopy(value)
    if "initial_window_size" in normalized:
        normalized["initial_window_size"] = _int(
            normalized["initial_window_size"],
            field="settings.initial_window_size",
            code=code,
            minimum=0,
            maximum=MAX_WINDOW,
        )
    if "max_frame_size" in normalized:
        normalized["max_frame_size"] = _int(
            normalized["max_frame_size"],
            field="settings.max_frame_size",
            code=code,
            minimum=16384,
            maximum=16777215,
        )
    if "max_concurrent_streams" in normalized:
        normalized["max_concurrent_streams"] = _int(
            normalized["max_concurrent_streams"],
            field="settings.max_concurrent_streams",
            code=code,
            minimum=1,
            maximum=MAX_STREAM_ID,
        )
    if "enable_push" in normalized and normalized["enable_push"] not in {0, 1}:
        raise error(code, "settings.enable_push must be 0 or 1")
    return normalized


def normalize_event(raw: Any, *, expected_seq: int) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise error(FRAME_001, "each frame event must be a JSON object")
    event = copy.deepcopy(raw)
    seq = _int(
        event.get("seq"),
        field="event.seq",
        code=EVENT_SEQ_001,
        minimum=1,
        maximum=MAX_STREAM_ID,
    )
    if seq != expected_seq:
        raise error(
            EVENT_SEQ_001,
            "event seq values must be contiguous and start at 1",
            details={"expected": expected_seq, "actual": seq},
        )
    clock_ms = _int(
        event.get("clock_ms"),
        field="event.clock_ms",
        code=CLOCK_001,
        minimum=0,
    )
    event_type = event.get("type")
    if not isinstance(event_type, str):
        raise error(FRAME_001, "event.type must be a string", details={"seq": seq})
    event_type = event_type.upper()
    if event_type not in VALID_TYPES:
        raise error(
            FRAME_001,
            "unsupported event type",
            details={"seq": seq, "type": event_type},
        )
    if event_type == "SETTINGS" and event.get("ack") is True:
        event_type = "SETTINGS_ACK"
    if event_type == "PING" and event.get("ack") is True:
        event_type = "PING_ACK"
    event["type"] = event_type
    direction = event.get("direction", DEFAULT_DIRECTIONS[event_type])
    if direction not in {"send", "recv"}:
        raise error(
            FRAME_001,
            "event.direction must be 'send' or 'recv'",
            details={"seq": seq, "direction": direction},
        )
    event["direction"] = direction

    stream_id = event.get("stream_id")
    if stream_id is not None:
        stream_id = _int(
            stream_id,
            field="event.stream_id",
            code=STREAM_ID_001,
            minimum=1,
            maximum=MAX_STREAM_ID,
        )
        event["stream_id"] = stream_id

    if event_type in {"HEADERS", "CONTINUATION", "DATA", "RST_STREAM"} and stream_id is None:
        raise error(
            FRAME_001,
            f"{event_type} requires stream_id",
            details={"seq": seq},
        )
    if event_type in {"SETTINGS", "SETTINGS_ACK", "GOAWAY"} and stream_id is not None:
        raise error(
            FRAME_001,
            f"{event_type} is a connection-level frame and must not carry stream_id",
            details={"seq": seq, "stream_id": stream_id},
        )

    if event_type == "HEADERS":
        event["end_headers"] = bool(event.get("end_headers", False))
        event["end_stream"] = bool(event.get("end_stream", False))
        event["fragment_hex"] = _decode_fragment(event, seq)
    elif event_type == "CONTINUATION":
        if event.get("end_stream"):
            raise error(FRAME_001, "CONTINUATION cannot carry END_STREAM", details={"seq": seq})
        event["end_headers"] = bool(event.get("end_headers", False))
        event["fragment_hex"] = _decode_fragment(event, seq)
    elif event_type == "DATA":
        data_hex = event.get("data_hex")
        if data_hex is not None:
            if not isinstance(data_hex, str):
                raise error(FRAME_001, "data_hex must be a string", details={"seq": seq})
            try:
                data_length = len(bytes.fromhex(data_hex))
            except ValueError as exc:
                raise error(FRAME_001, "data_hex is invalid", details={"seq": seq}) from exc
            if "length" in event and event["length"] != data_length:
                raise error(
                    FRAME_001,
                    "DATA length does not match data_hex",
                    details={"seq": seq, "length": event["length"], "data_length": data_length},
                )
            event["length"] = data_length
        else:
            event["length"] = _int(
                event.get("length"),
                field="DATA.length",
                code=FRAME_001,
                minimum=0,
                maximum=MAX_WINDOW,
            )
        event["end_stream"] = bool(event.get("end_stream", False))
    elif event_type == "WINDOW_UPDATE":
        level = event.get("level")
        if level is None:
            level = "stream" if stream_id is not None else "connection"
        if level not in {"connection", "stream"}:
            raise error(
                WINDOW_UPDATE_001,
                "WINDOW_UPDATE level must be 'connection' or 'stream'",
                details={"seq": seq},
            )
        if level == "connection" and stream_id is not None:
            raise error(
                WINDOW_UPDATE_001,
                "connection WINDOW_UPDATE must not carry stream_id",
                details={"seq": seq},
            )
        if level == "stream" and stream_id is None:
            raise error(
                WINDOW_UPDATE_001,
                "stream WINDOW_UPDATE requires stream_id",
                details={"seq": seq},
            )
        increment = event.get("window_increment", event.get("increment"))
        event["window_increment"] = _int(
            increment,
            field="WINDOW_UPDATE.window_increment",
            code=WINDOW_UPDATE_001,
            minimum=0,
            maximum=MAX_WINDOW,
        )
        event["level"] = level
    elif event_type == "SETTINGS":
        event["settings"] = _normalize_settings(event.get("settings", {}), code=SETTINGS_001)
        event["ack"] = False
    elif event_type == "SETTINGS_ACK":
        if event.get("settings"):
            raise error(SETTINGS_001, "SETTINGS ACK must not carry a settings payload")
        event["ack"] = True
    elif event_type in {"PING", "PING_ACK"}:
        payload_hex = event.get("payload_hex", "0000000000000000")
        if not isinstance(payload_hex, str):
            raise error(PING_001, "PING payload_hex must be a string", details={"seq": seq})
        try:
            payload = bytes.fromhex(payload_hex)
        except ValueError as exc:
            raise error(PING_001, "PING payload_hex is invalid", details={"seq": seq}) from exc
        if len(payload) != 8:
            raise error(
                PING_001,
                "PING payload must be exactly 8 bytes",
                details={"seq": seq, "length": len(payload)},
            )
        event["payload_hex"] = payload_hex.lower()
        event["ack"] = event_type == "PING_ACK"
    elif event_type == "GOAWAY":
        event["last_stream_id"] = _int(
            event.get("last_stream_id"),
            field="GOAWAY.last_stream_id",
            code=GOAWAY_001,
            minimum=0,
            maximum=MAX_STREAM_ID,
        )
        event["error_code"] = _int(
            event.get("error_code", 0),
            field="GOAWAY.error_code",
            code=GOAWAY_001,
            minimum=0,
        )
    return event


def normalize_scenario(document: Any, *, preflight: bool = True) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise error(SCENARIO_JSON_001, "scenario must be a JSON object")
    scenario = copy.deepcopy(document)
    fmt = scenario.get("format", "h2diag.scenario/v1")
    if fmt != "h2diag.scenario/v1":
        raise error(
            SCENARIO_SCHEMA_001,
            "unsupported scenario format",
            details={"format": fmt},
        )
    scenario_id = scenario.get("scenario_id")
    if not isinstance(scenario_id, str) or not scenario_id:
        raise error(SCENARIO_SCHEMA_001, "scenario_id must be a non-empty string")
    scenario["scenario_id"] = scenario_id
    connection_id = scenario.get("connection_id", scenario_id)
    if not isinstance(connection_id, str) or not connection_id:
        raise error(SCENARIO_SCHEMA_001, "connection_id must be a non-empty string")
    scenario["connection_id"] = connection_id
    local_role = scenario.get("local_role", "client")
    if local_role not in {"client", "server"}:
        raise error(SCENARIO_SCHEMA_001, "local_role must be 'client' or 'server'")
    scenario["local_role"] = local_role

    connection = scenario.get("connection")
    if not isinstance(connection, dict):
        raise error(SCENARIO_SCHEMA_001, "connection must be an object")
    connection = copy.deepcopy(connection)
    settings = _normalize_settings(connection.get("settings", {}), code=SETTINGS_001)
    default_initial = settings.get(
        "initial_window_size",
        connection.get("initial_stream_send_window", 65535),
    )
    default_initial = _int(
        default_initial,
        field="settings.initial_window_size",
        code=FLOW_INITIAL_001,
        minimum=1,
        maximum=MAX_WINDOW,
    )
    settings.setdefault("initial_window_size", default_initial)
    settings.setdefault("max_frame_size", 16384)
    initial_send = _int(
        connection.get("initial_send_window", connection.get("initial_connection_window", 65535)),
        field="connection.initial_send_window",
        code=FLOW_INITIAL_001,
        minimum=1,
        maximum=MAX_WINDOW,
    )
    initial_recv = _int(
        connection.get("initial_recv_window", connection.get("initial_connection_window", 65535)),
        field="connection.initial_recv_window",
        code=FLOW_INITIAL_001,
        minimum=1,
        maximum=MAX_WINDOW,
    )
    max_send = _int(
        connection.get("max_send_window", MAX_WINDOW),
        field="connection.max_send_window",
        code=FLOW_INITIAL_001,
        minimum=initial_send,
        maximum=MAX_WINDOW,
    )
    max_recv = _int(
        connection.get("max_recv_window", MAX_WINDOW),
        field="connection.max_recv_window",
        code=FLOW_INITIAL_001,
        minimum=initial_recv,
        maximum=MAX_WINDOW,
    )
    connection.update(
        {
            "initial_send_window": initial_send,
            "initial_recv_window": initial_recv,
            "max_send_window": max_send,
            "max_recv_window": max_recv,
            "settings": settings,
        }
    )
    scenario["connection"] = connection

    raw_streams = scenario.get("streams")
    if not isinstance(raw_streams, list):
        raise error(SCENARIO_SCHEMA_001, "streams must be an array")
    streams: list[dict[str, Any]] = []
    previous_id = 0
    seen_ids: set[int] = set()
    for index, raw_stream in enumerate(raw_streams):
        if not isinstance(raw_stream, dict):
            raise error(
                SCENARIO_SCHEMA_001,
                "each stream declaration must be an object",
                details={"index": index},
            )
        stream = copy.deepcopy(raw_stream)
        stream_id = _int(
            stream.get("stream_id"),
            field="stream.stream_id",
            code=STREAM_ID_001,
            minimum=1,
            maximum=MAX_STREAM_ID,
        )
        if stream_id in seen_ids:
            raise error(
                STREAM_ID_001,
                "stream ids must be unique",
                details={"stream_id": stream_id},
            )
        if stream_id <= previous_id:
            raise error(
                STREAM_ID_001,
                "stream ids must be strictly increasing",
                details={"previous": previous_id, "stream_id": stream_id},
            )
        previous_id = stream_id
        seen_ids.add(stream_id)
        initiator = stream.get("initiator")
        if initiator is None:
            initiator = local_role
        if initiator not in {"client", "server"}:
            raise error(
                SCENARIO_SCHEMA_001,
                "stream.initiator must be 'client' or 'server'",
                details={"stream_id": stream_id},
            )
        expected_odd = initiator == "client"
        if (stream_id % 2 == 1) != expected_odd:
            raise error(
                STREAM_PARITY_001,
                "stream id parity does not match its initiator",
                details={
                    "stream_id": stream_id,
                    "initiator": initiator,
                    "expected": "odd" if expected_odd else "even",
                },
            )
        initial_stream_send = _int(
            stream.get("initial_send_window", settings["initial_window_size"]),
            field="stream.initial_send_window",
            code=FLOW_INITIAL_001,
            minimum=1,
            maximum=MAX_WINDOW,
        )
        initial_stream_recv = _int(
            stream.get("initial_recv_window", settings["initial_window_size"]),
            field="stream.initial_recv_window",
            code=FLOW_INITIAL_001,
            minimum=1,
            maximum=MAX_WINDOW,
        )
        stream_send_max = _int(
            stream.get("max_send_window", MAX_WINDOW),
            field="stream.max_send_window",
            code=FLOW_INITIAL_001,
            minimum=initial_stream_send,
            maximum=MAX_WINDOW,
        )
        stream_recv_max = _int(
            stream.get("max_recv_window", MAX_WINDOW),
            field="stream.max_recv_window",
            code=FLOW_INITIAL_001,
            minimum=initial_stream_recv,
            maximum=MAX_WINDOW,
        )
        weight = _int(
            stream.get("weight", 16),
            field="stream.weight",
            code=WEIGHT_001,
            minimum=1,
            maximum=256,
        )
        expected = stream.get("expected_final_state")
        if expected is not None and expected not in VALID_STATES:
            raise error(
                SCENARIO_SCHEMA_001,
                "expected_final_state is not a valid HTTP/2 stream state",
                details={"stream_id": stream_id, "expected": expected},
            )
        stream.update(
            {
                "initiator": initiator,
                "initial_send_window": initial_stream_send,
                "initial_recv_window": initial_stream_recv,
                "max_send_window": stream_send_max,
                "max_recv_window": stream_recv_max,
                "weight": weight,
                "expected_final_state": expected,
            }
        )
        streams.append(stream)
    scenario["streams"] = streams

    raw_events = scenario.get("events")
    if not isinstance(raw_events, list):
        raise error(SCENARIO_SCHEMA_001, "events must be an array")
    events: list[dict[str, Any]] = []
    pending_header: dict[str, Any] | None = None
    previous_clock = -1
    max_stream_id = max(seen_ids, default=0)
    for expected_seq, raw_event in enumerate(raw_events, start=1):
        event = normalize_event(raw_event, expected_seq=expected_seq)
        if event["clock_ms"] < previous_clock:
            raise error(
                CLOCK_001,
                "event logical clocks must be non-decreasing",
                details={
                    "seq": event["seq"],
                    "previous": previous_clock,
                    "actual": event["clock_ms"],
                },
            )
        previous_clock = event["clock_ms"]
        if pending_header is not None:
            if (
                event["type"] != "CONTINUATION"
                or event.get("stream_id") != pending_header["stream_id"]
                or event["direction"] != pending_header["direction"]
            ):
                raise error(
                    HEADER_ORDER_001,
                    "CONTINUATION must immediately follow an unfinished HEADERS block",
                    details={
                        "pending_start_seq": pending_header["start_seq"],
                        "pending_stream_id": pending_header["stream_id"],
                        "actual_seq": event["seq"],
                        "actual_type": event["type"],
                    },
                )
        elif event["type"] == "CONTINUATION":
            raise error(
                HEADER_ORDER_001,
                "CONTINUATION has no preceding HEADERS block",
                details={"seq": event["seq"]},
            )
        if event["type"] == "HEADERS":
            if not event["end_headers"]:
                pending_header = {
                    "stream_id": event["stream_id"],
                    "direction": event["direction"],
                    "start_seq": event["seq"],
                }
        elif event["type"] == "CONTINUATION" and event["end_headers"]:
            pending_header = None
        if event.get("stream_id") is not None and event["stream_id"] not in seen_ids:
            raise error(
                SCENARIO_SCHEMA_001,
                "event references an undeclared stream",
                details={"seq": event["seq"], "stream_id": event["stream_id"]},
            )
        if event["type"] == "GOAWAY" and event["last_stream_id"] > max_stream_id:
            raise error(
                GOAWAY_001,
                "GOAWAY last_stream_id is beyond the declared stream range",
                details={
                    "seq": event["seq"],
                    "last_stream_id": event["last_stream_id"],
                    "max_stream_id": max_stream_id,
                },
            )
        events.append(event)
    if pending_header is not None:
        raise error(
            HEADER_INCOMPLETE_001,
            "scenario ends with an unfinished HEADERS block",
            details=pending_header,
        )
    scenario["events"] = events

    expected_root = scenario.get("expected", {})
    if not isinstance(expected_root, dict):
        raise error(SCENARIO_SCHEMA_001, "expected must be an object when present")
    scenario["expected"] = copy.deepcopy(expected_root)

    if preflight:
        from .engine import Engine

        Engine(scenario).run_all(check_expected=True)
    return scenario


def load_json(path: str | Path, *, kind: str = "scenario") -> Any:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except json.JSONDecodeError as exc:
        code = SCENARIO_JSON_001 if kind == "scenario" else PATCH_JSON_001
        raise error(
            code,
            "invalid JSON document",
            details={"path": str(path), "line": exc.lineno, "column": exc.colno},
        ) from exc
    except OSError as exc:
        raise error(
            SCENARIO_JSON_001 if kind == "scenario" else PATCH_JSON_001,
            "cannot read JSON document",
            details={"path": str(path), "reason": str(exc)},
        ) from exc


def validate_patch_document(document: Any) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise error(PATCH_JSON_001, "patch must be a JSON object")
    actual_digest = digest_without_fields(document, ["content_digest"])
    declared_digest = document.get("content_digest")
    if not isinstance(declared_digest, str) or not declared_digest:
        raise error(PATCH_DIGEST_001, "patch.content_digest is required")
    if declared_digest != actual_digest:
        raise error(
            PATCH_DIGEST_001,
            "patch content digest does not match the declared value",
            details={"declared": declared_digest, "computed": actual_digest},
        )
    patch = copy.deepcopy(document)
    fmt = patch.get("format", "h2diag.patch/v1")
    if fmt != "h2diag.patch/v1":
        raise error(PATCH_SCHEMA_001, "unsupported patch format", details={"format": fmt})
    patch_id = patch.get("patch_id")
    if not isinstance(patch_id, str) or not patch_id:
        raise error(PATCH_SCHEMA_001, "patch_id must be a non-empty string")
    base_version = _int(
        patch.get("base_version"),
        field="patch.base_version",
        code=PATCH_SCHEMA_001,
        minimum=1,
    )
    target_version = _int(
        patch.get("target_version"),
        field="patch.target_version",
        code=PATCH_SCHEMA_001,
        minimum=1,
    )
    mode = patch.get("mode", "incremental")
    if mode not in {"incremental", "append", "replace", "noop"}:
        raise error(PATCH_SCHEMA_001, "patch.mode is not supported", details={"mode": mode})
    clock_ms = patch.get("clock_ms")
    if clock_ms is not None:
        patch["clock_ms"] = _int(
            clock_ms,
            field="patch.clock_ms",
            code=CLOCK_001,
            minimum=0,
        )
    operations = patch.get("operations", [])
    if not isinstance(operations, list):
        raise error(PATCH_SCHEMA_001, "patch.operations must be an array")
    normalized_operations: list[dict[str, Any]] = []
    for index, raw_operation in enumerate(operations):
        if not isinstance(raw_operation, dict):
            raise error(PATCH_OP_001, "patch operation must be an object", details={"index": index})
        operation = copy.deepcopy(raw_operation)
        op = operation.get("op")
        if op == "set_weight":
            operation["stream_id"] = _int(
                operation.get("stream_id"),
                field="operation.stream_id",
                code=PATCH_OP_001,
                minimum=1,
            )
            operation["weight"] = _int(
                operation.get("weight"),
                field="operation.weight",
                code=WEIGHT_001,
                minimum=1,
                maximum=256,
            )
        elif op == "set_settings":
            operation["settings"] = _normalize_settings(
                operation.get("settings"), code=SETTINGS_001
            )
            direction = operation.get("direction", "recv")
            if direction not in {"send", "recv"}:
                raise error(PATCH_OP_001, "set_settings.direction must be send or recv")
            operation["direction"] = direction
        elif op == "set_stream_end":
            operation["stream_id"] = _int(
                operation.get("stream_id"),
                field="operation.stream_id",
                code=PATCH_OP_001,
                minimum=1,
            )
            side = operation.get("side", "both")
            if side not in {"local", "remote", "both"}:
                raise error(PATCH_OP_001, "set_stream_end.side must be local, remote, or both")
            operation["side"] = side
            if operation.get("ended", True) is not True:
                raise error(PATCH_OP_001, "ended=false would reverse a monotonic stream end")
            operation["ended"] = True
        elif op == "set_expected_final_state":
            operation["stream_id"] = _int(
                operation.get("stream_id"),
                field="operation.stream_id",
                code=PATCH_OP_001,
                minimum=1,
            )
            expected = operation.get("expected_final_state")
            if expected not in VALID_STATES:
                raise error(PATCH_OP_001, "invalid expected_final_state", details={"value": expected})
        elif op == "window_update":
            level = operation.get("level")
            if level not in {"connection", "stream"}:
                raise error(PATCH_OP_001, "window_update.level must be connection or stream")
            direction = operation.get("direction", "recv")
            if direction not in {"send", "recv"}:
                raise error(PATCH_OP_001, "window_update.direction must be send or recv")
            increment = operation.get("increment", operation.get("window_increment"))
            operation["increment"] = _int(
                increment,
                field="operation.increment",
                code=WINDOW_UPDATE_001,
                minimum=0,
                maximum=MAX_WINDOW,
            )
            if level == "stream":
                operation["stream_id"] = _int(
                    operation.get("stream_id"),
                    field="operation.stream_id",
                    code=PATCH_OP_001,
                    minimum=1,
                )
            elif operation.get("stream_id") is not None:
                raise error(PATCH_OP_001, "connection window_update must not carry stream_id")
            operation["level"] = level
            operation["direction"] = direction
        elif op == "append_events":
            events = operation.get("events")
            if not isinstance(events, list):
                raise error(PATCH_OP_001, "append_events.events must be an array")
        else:
            raise error(PATCH_OP_001, "unsupported patch operation", details={"op": op})
        normalized_operations.append(operation)
    patch["operations"] = normalized_operations

    top_events = patch.get("events", [])
    if not isinstance(top_events, list):
        raise error(PATCH_SCHEMA_001, "patch.events must be an array when present")
    if mode == "replace":
        if not isinstance(patch.get("scenario"), dict):
            raise error(PATCH_SCHEMA_001, "replace patch requires a full scenario object")
    patch["base_version"] = base_version
    patch["target_version"] = target_version
    patch["mode"] = mode
    patch["patch_id"] = patch_id
    patch["events"] = copy.deepcopy(top_events)
    patch["_computed_digest"] = actual_digest
    return patch
