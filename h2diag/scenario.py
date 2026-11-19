from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .canonical import digest_value, sha256_hex
from .errors import (
    E_BAD_SCENARIO,
    E_GOAWAY_STREAM,
    E_UNKNOWN_STREAM,
    ScenarioError,
)


STREAM_STATES = {
    "idle",
    "open",
    "half-closed-local",
    "half-closed-remote",
    "closed",
    "error",
}
DIRECTIONS = {"inbound", "outbound"}
FRAME_TYPES = {
    "HEADERS",
    "CONTINUATION",
    "DATA",
    "RST_STREAM",
    "WINDOW_UPDATE",
    "SETTINGS",
    "PING",
    "GOAWAY",
}
MAX_STREAM_ID = 2**31 - 1
MAX_HTTP2_WINDOW = 2**31 - 1
MAX_WEIGHT = 256


def _fail(message: str, **details: Any) -> None:
    raise ScenarioError(E_BAD_SCENARIO, message, details)


def _require_mapping(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(f"{path} must be an object", path=path)
    return value


def _require_list(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        _fail(f"{path} must be an array", path=path)
    return value


def _require_int(value: Any, path: str, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(f"{path} must be an integer", path=path)
    if minimum is not None and value < minimum:
        _fail(f"{path} must be >= {minimum}", path=path, value=value)
    return value


def _require_str(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value:
        _fail(f"{path} must be a non-empty string", path=path)
    return value


def _require_bool(value: Any, path: str, default: bool = False) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        _fail(f"{path} must be a boolean", path=path)
    return value


@dataclass(frozen=True)
class StreamDeclaration:
    stream_id: int
    initial_send_window: int
    initial_recv_window: int
    weight: int
    expected_final_state: str


@dataclass(frozen=True)
class Scenario:
    schema_version: int
    scenario_id: str
    initiator: str
    connection: dict[str, int]
    streams: tuple[StreamDeclaration, ...]
    events: tuple[dict[str, Any], ...]
    raw: dict[str, Any]
    canonical_hash: str
    event_digests: tuple[str, ...]

    @property
    def max_stream_id(self) -> int:
        return max(stream.stream_id for stream in self.streams)

    def stream_by_id(self) -> dict[int, StreamDeclaration]:
        return {stream.stream_id: stream for stream in self.streams}


def _validate_fragment(event: dict[str, Any], path: str) -> None:
    fragment = event.get("fragment_hex")
    if fragment is None:
        fragment = ""
    if not isinstance(fragment, str):
        _fail(f"{path}.fragment_hex must be a string", path=path)
    if len(fragment) % 2:
        _fail(
            f"{path}.fragment_hex must contain an even number of hex digits",
            path=path,
        )
    try:
        bytes.fromhex(fragment)
    except ValueError as exc:
        _fail(f"{path}.fragment_hex is not valid hex: {exc}", path=path)


def _validate_event(event: dict[str, Any], path: str, declared: set[int]) -> None:
    frame_type = _require_str(event.get("type"), f"{path}.type")
    if frame_type not in FRAME_TYPES:
        _fail(f"unsupported frame type {frame_type!r}", path=path, type=frame_type)

    direction = _require_str(event.get("direction"), f"{path}.direction")
    if direction not in DIRECTIONS:
        _fail(
            f"{path}.direction must be one of {sorted(DIRECTIONS)}",
            path=path,
            direction=direction,
        )

    stream_id = event.get("stream_id")
    if stream_id is not None:
        stream_id = _require_int(stream_id, f"{path}.stream_id", 1)
        if stream_id > MAX_STREAM_ID:
            _fail(f"{path}.stream_id is out of range", path=path)
        if stream_id not in declared:
            raise ScenarioError(
                E_UNKNOWN_STREAM,
                f"{path} references undeclared stream {stream_id}",
                {"path": path, "stream_id": stream_id},
            )

    if frame_type in {"HEADERS", "CONTINUATION", "DATA", "RST_STREAM"}:
        if stream_id is None:
            _fail(f"{path}.stream_id is required for {frame_type}", path=path)

    if frame_type in {"HEADERS", "CONTINUATION"}:
        _validate_fragment(event, path)
        _require_bool(event.get("end_headers"), f"{path}.end_headers")
        if frame_type == "CONTINUATION" and event.get("end_stream") is True:
            _fail(f"{path}.end_stream is not valid on CONTINUATION", path=path)
        _require_bool(event.get("end_stream"), f"{path}.end_stream")

    elif frame_type == "DATA":
        _require_int(event.get("length"), f"{path}.length", 0)
        _require_bool(event.get("end_stream"), f"{path}.end_stream")

    elif frame_type == "RST_STREAM":
        _require_int(event.get("error_code"), f"{path}.error_code", 0)

    elif frame_type == "WINDOW_UPDATE":
        level = _require_str(event.get("level"), f"{path}.level")
        if level not in {"connection", "stream"}:
            _fail(f"{path}.level must be connection or stream", path=path)
        increment = _require_int(event.get("increment"), f"{path}.increment", 1)
        if increment > MAX_HTTP2_WINDOW:
            _fail(f"{path}.increment exceeds HTTP/2 limit", path=path)
        if level == "stream":
            if stream_id is None:
                _fail(f"{path}.stream_id is required for stream WINDOW_UPDATE", path=path)
        elif stream_id is not None:
            _fail(
                f"{path}.stream_id must be omitted for connection WINDOW_UPDATE",
                path=path,
            )

    elif frame_type == "SETTINGS":
        ack = _require_bool(event.get("ack"), f"{path}.ack")
        settings = event.get("settings", {})
        if not isinstance(settings, dict):
            _fail(f"{path}.settings must be an object", path=path)
        if ack and settings:
            _fail(f"{path}.settings must be empty for SETTINGS ACK", path=path)
        for key, value in settings.items():
            if not isinstance(key, str) or not key:
                _fail(f"{path}.settings keys must be non-empty strings", path=path)
            _require_int(value, f"{path}.settings.{key}", 0)

    elif frame_type == "PING":
        payload = event.get("payload_hex", "")
        if not isinstance(payload, str) or len(payload) != 16:
            _fail(f"{path}.payload_hex must contain exactly 8 bytes", path=path)
        try:
            bytes.fromhex(payload)
        except ValueError:
            _fail(f"{path}.payload_hex is not valid hex", path=path)
        _require_bool(event.get("ack"), f"{path}.ack")

    elif frame_type == "GOAWAY":
        last_stream_id = _require_int(
            event.get("last_stream_id"), f"{path}.last_stream_id", 0
        )
        if last_stream_id > MAX_STREAM_ID:
            _fail(f"{path}.last_stream_id is out of range", path=path)
        _require_int(event.get("error_code", 0), f"{path}.error_code", 0)
        debug = event.get("debug_hex", "")
        if not isinstance(debug, str) or len(debug) % 2:
            _fail(f"{path}.debug_hex must be an even-length string", path=path)
        try:
            bytes.fromhex(debug)
        except ValueError:
            _fail(f"{path}.debug_hex is not valid hex", path=path)


def parse_scenario(raw: dict[str, Any]) -> Scenario:
    raw = _require_mapping(raw, "$")
    schema_version = _require_int(
        raw.get("schema_version", 1), "$.schema_version", 1
    )
    if schema_version != 1:
        _fail("only schema_version 1 is supported", path="$.schema_version")

    scenario_id = _require_str(raw.get("scenario_id"), "$.scenario_id")
    initiator = _require_str(raw.get("initiator"), "$.initiator")
    if initiator not in {"client", "server"}:
        _fail("initiator must be client or server", path="$.initiator")

    connection_raw = _require_mapping(raw.get("connection"), "$.connection")
    max_window = _require_int(
        connection_raw.get("max_window", MAX_HTTP2_WINDOW),
        "$.connection.max_window",
        1,
    )
    if max_window > MAX_HTTP2_WINDOW:
        _fail("max_window exceeds HTTP/2 limit", path="$.connection.max_window")
    send_window = _require_int(
        connection_raw.get("send_window"), "$.connection.send_window", 1
    )
    recv_window = _require_int(
        connection_raw.get("recv_window"), "$.connection.recv_window", 1
    )
    if send_window > max_window or recv_window > max_window:
        _fail(
            "initial connection windows must not exceed max_window",
            path="$.connection",
        )
    max_concurrent = _require_int(
        connection_raw.get("max_concurrent_streams", 100),
        "$.connection.max_concurrent_streams",
        1,
    )
    connection = {
        "max_concurrent_streams": max_concurrent,
        "max_window": max_window,
        "recv_window": recv_window,
        "send_window": send_window,
    }

    streams_raw = _require_list(raw.get("streams"), "$.streams")
    if not streams_raw:
        _fail("$.streams must not be empty", path="$.streams")

    streams: list[StreamDeclaration] = []
    previous_id = 0
    for index, item in enumerate(streams_raw):
        path = f"$.streams[{index}]"
        item = _require_mapping(item, path)
        stream_id = _require_int(item.get("stream_id"), f"{path}.stream_id", 1)
        if stream_id > MAX_STREAM_ID:
            _fail(f"{path}.stream_id is out of range", path=path)
        if stream_id <= previous_id:
            _fail(
                "stream IDs must be strictly increasing",
                path=f"{path}.stream_id",
                previous=previous_id,
                value=stream_id,
            )
        expected_parity = 1 if initiator == "client" else 0
        if stream_id % 2 != expected_parity:
            _fail(
                f"{initiator}-initiated streams must use "
                f"{'odd' if expected_parity else 'even'} IDs",
                path=f"{path}.stream_id",
                initiator=initiator,
                value=stream_id,
            )
        previous_id = stream_id

        initial_send_window = _require_int(
            item.get("initial_send_window"), f"{path}.initial_send_window", 1
        )
        initial_recv_window = _require_int(
            item.get("initial_recv_window"), f"{path}.initial_recv_window", 1
        )
        if initial_send_window > max_window or initial_recv_window > max_window:
            _fail(
                "stream initial windows must not exceed max_window",
                path=path,
            )
        weight = _require_int(item.get("weight"), f"{path}.weight", 1)
        if weight > MAX_WEIGHT:
            _fail(f"{path}.weight must be in [1, {MAX_WEIGHT}]", path=path)
        expected = _require_str(
            item.get("expected_final_state"), f"{path}.expected_final_state"
        )
        if expected not in STREAM_STATES:
            _fail(
                f"{path}.expected_final_state is not a valid stream state",
                path=path,
                state=expected,
            )
        streams.append(
            StreamDeclaration(
                stream_id=stream_id,
                initial_send_window=initial_send_window,
                initial_recv_window=initial_recv_window,
                weight=weight,
                expected_final_state=expected,
            )
        )

    if len(streams) > max_concurrent:
        _fail(
            "declared stream count exceeds max_concurrent_streams",
            path="$.streams",
        )

    declared = {stream.stream_id for stream in streams}
    events_raw = _require_list(raw.get("events"), "$.events")
    events: list[dict[str, Any]] = []
    previous_clock: int | None = None
    goaway_seen = False
    for index, item in enumerate(events_raw):
        path = f"$.events[{index}]"
        item = _require_mapping(item, path)
        expected_seq = index + 1
        seq = _require_int(item.get("seq"), f"{path}.seq", 1)
        if seq != expected_seq:
            _fail(
                f"{path}.seq must equal {expected_seq}",
                path=f"{path}.seq",
                value=seq,
            )
        at_ms = _require_int(item.get("at_ms"), f"{path}.at_ms", 0)
        if previous_clock is not None and at_ms < previous_clock:
            _fail(
                "event logical clock must be non-decreasing",
                path=f"{path}.at_ms",
                previous=previous_clock,
                value=at_ms,
            )
        previous_clock = at_ms
        _validate_event(item, path, declared)
        if item["type"] == "GOAWAY":
            goaway_seen = True
            last_stream_id = item["last_stream_id"]
            if last_stream_id > previous_id:
                _fail(
                    "GOAWAY last_stream_id exceeds the declared stream ID range",
                    path=f"{path}.last_stream_id",
                    last_stream_id=last_stream_id,
                    max_declared_stream_id=previous_id,
                )
        elif goaway_seen and item["type"] == "HEADERS":
            stream_id = item.get("stream_id")
            if stream_id is not None and stream_id > max(
                evt.get("last_stream_id", 0)
                for evt in events
                if evt["type"] == "GOAWAY"
            ):
                raise ScenarioError(
                    E_GOAWAY_STREAM,
                    "HEADERS after GOAWAY exceeds last_stream_id",
                    {"path": path, "stream_id": stream_id},
                )
        events.append(copy.deepcopy(item))

    if not events:
        _fail("$.events must not be empty", path="$.events")

    canonical_hash = digest_value(raw)
    event_digests = tuple(digest_value(event) for event in events)
    return Scenario(
        schema_version=schema_version,
        scenario_id=scenario_id,
        initiator=initiator,
        connection=connection,
        streams=tuple(streams),
        events=tuple(events),
        raw=copy.deepcopy(raw),
        canonical_hash=canonical_hash,
        event_digests=event_digests,
    )


def load_scenario(path: str | Path) -> Scenario:
    scenario_path = Path(path)
    try:
        with scenario_path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except OSError as exc:
        raise ScenarioError(
            E_BAD_SCENARIO,
            f"cannot read scenario: {exc}",
            {"path": str(scenario_path)},
        ) from exc
    except json.JSONDecodeError as exc:
        raise ScenarioError(
            E_BAD_SCENARIO,
            f"invalid scenario JSON: {exc}",
            {"path": str(scenario_path), "line": exc.lineno, "column": exc.colno},
        ) from exc
    return parse_scenario(raw)

