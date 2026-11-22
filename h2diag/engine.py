from __future__ import annotations

import copy
import hashlib
from typing import Any

from .canonical import digest_json, hash_chain
from .errors import (
    CLOCK_001,
    EVENT_SEQ_001,
    EXPECTED_STATE_001,
    FLOW_001,
    FRAME_001,
    FRAME_STATE_001,
    GOAWAY_001,
    HEADER_ORDER_001,
    PING_001,
    SETTINGS_001,
    SETTINGS_ACK_001,
    WINDOW_UPDATE_001,
    error,
)
from .scheduler import apply_ranks

FLOW_STATES = {
    "idle",
    "open",
    "half-closed-local",
    "half-closed-remote",
    "closed",
}

SEND_ALLOWED_STATES = {"open", "half-closed-remote"}
RECV_ALLOWED_STATES = {"open", "half-closed-local"}


def _as_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise error(FRAME_001, f"{name} must be an integer", details={"field": name})
    return value


def _int_field(event: dict[str, Any], name: str, default: int = 0) -> int:
    value = event.get(name, default)
    return _as_int(value, name)


def _bool_field(event: dict[str, Any], name: str, default: bool = False) -> bool:
    value = event.get(name, default)
    if not isinstance(value, bool):
        raise error(FRAME_001, f"{name} must be a boolean", details={"field": name})
    return value


def _stream_key(stream_id: int) -> str:
    return str(stream_id)


def _get_stream(state: dict[str, Any], stream_id: int) -> dict[str, Any]:
    key = _stream_key(stream_id)
    if key not in state["streams"]:
        raise error(
            FRAME_STATE_001,
            "frame references an undeclared stream",
            details={"stream_id": stream_id},
        )
    return state["streams"][key]


def _assert_goaway_allows(state: dict[str, Any], stream_id: int) -> None:
    last = state["connection"].get("goaway", {}).get("last_stream_id")
    if last is not None and stream_id > last:
        raise error(
            GOAWAY_001,
            "stream is above the final GOAWAY stream id",
            details={"stream_id": stream_id, "last_stream_id": last},
        )


def _assert_can_send(stream: dict[str, Any]) -> None:
    if stream["reset"]:
        raise error(
            FRAME_STATE_001,
            "a reset stream cannot be reused",
            details={"stream_id": stream["stream_id"], "state": stream["state"]},
        )
    if stream["local_end"] or stream["state"] == "half-closed-local":
        raise error(
            FRAME_STATE_001,
            "stream is locally closed",
            details={"stream_id": stream["stream_id"], "state": stream["state"]},
        )
    if stream["state"] not in SEND_ALLOWED_STATES:
        raise error(
            FRAME_STATE_001,
            "stream cannot send in its current state",
            details={"stream_id": stream["stream_id"], "state": stream["state"]},
        )


def _assert_can_receive(stream: dict[str, Any]) -> None:
    if stream["reset"]:
        raise error(
            FRAME_STATE_001,
            "a reset stream cannot be reused",
            details={"stream_id": stream["stream_id"], "state": stream["state"]},
        )
    if stream["remote_end"] or stream["state"] == "half-closed-remote":
        raise error(
            FRAME_STATE_001,
            "stream is remotely closed",
            details={"stream_id": stream["stream_id"], "state": stream["state"]},
        )
    if stream["state"] not in RECV_ALLOWED_STATES:
        raise error(
            FRAME_STATE_001,
            "stream cannot receive in its current state",
            details={"stream_id": stream["stream_id"], "state": stream["state"]},
        )


def _end_side(stream: dict[str, Any], side: str) -> None:
    if side == "local":
        if stream["local_end"]:
            return
        stream["local_end"] = True
        if stream["state"] == "idle":
            stream["state"] = "half-closed-local"
        elif stream["state"] == "open":
            stream["state"] = "half-closed-local"
        elif stream["state"] == "half-closed-remote":
            stream["state"] = "closed"
        return
    if stream["remote_end"]:
        return
    stream["remote_end"] = True
    if stream["state"] == "idle":
        stream["state"] = "half-closed-remote"
    elif stream["state"] == "open":
        stream["state"] = "half-closed-remote"
    elif stream["state"] == "half-closed-local":
        stream["state"] = "closed"


def _open_stream(stream: dict[str, Any]) -> None:
    if stream["state"] == "idle":
        stream["state"] = "open"
        stream["opened"] = True
    elif stream["state"] == "closed":
        raise error(
            FRAME_STATE_001,
            "closed stream cannot be opened",
            details={"stream_id": stream["stream_id"]},
        )


def _reclaim_stream_credit(state: dict[str, Any], stream: dict[str, Any]) -> dict[str, int]:
    connection = state["connection"]
    send_refund = min(int(stream["send_in_flight"]), int(connection["send_in_flight"]))
    recv_refund = min(int(stream["recv_in_flight"]), int(connection["recv_in_flight"]))
    connection["send_in_flight"] -= send_refund
    connection["recv_in_flight"] -= recv_refund
    before_send = connection["send_window"]
    before_recv = connection["recv_window"]
    connection["send_window"] = min(
        connection["max_send_window"], connection["send_window"] + send_refund
    )
    connection["recv_window"] = min(
        connection["max_recv_window"], connection["recv_window"] + recv_refund
    )
    stream["send_in_flight"] = 0
    stream["recv_in_flight"] = 0
    stream["send_window"] = 0
    stream["recv_window"] = 0
    stream["reclaimed_to_connection"] += send_refund + recv_refund
    return {
        "send_refunded": connection["send_window"] - before_send,
        "recv_refunded": connection["recv_window"] - before_recv,
    }


def _close_stream(
    state: dict[str, Any],
    stream: dict[str, Any],
    *,
    reset: bool,
    reclaim: bool,
) -> dict[str, int]:
    refunds = {"send_refunded": 0, "recv_refunded": 0}
    if reclaim:
        refunds = _reclaim_stream_credit(state, stream)
    stream["state"] = "closed"
    stream["local_end"] = True
    stream["remote_end"] = True
    if reset:
        stream["reset"] = True
    return refunds


def finalize_state(state: dict[str, Any], event_digest: str | None = None) -> dict[str, Any]:
    previous_chain = state.get("chain_digest")
    state.pop("state_digest", None)
    state["state_digest"] = None
    state["chain_digest"] = None
    result_digest = digest_json(state)
    chain = hash_chain(previous_chain, event_digest or "", result_digest)
    state["state_digest"] = result_digest
    state["chain_digest"] = chain
    return state


def initial_state(scenario: dict[str, Any], *, patch_version: int = 1) -> dict[str, Any]:
    connection_doc = scenario["connection"]
    settings = copy.deepcopy(connection_doc.get("settings", {}))
    streams: dict[str, dict[str, Any]] = {}
    for stream in scenario["streams"]:
        stream_id = stream["stream_id"]
        entry: dict[str, Any] = {
            "stream_id": stream_id,
            "initiator": stream["initiator"],
            "state": "idle",
            "weight": stream["weight"],
            "send_window": stream["initial_send_window"],
            "recv_window": stream["initial_recv_window"],
            "max_send_window": stream["max_send_window"],
            "max_recv_window": stream["max_recv_window"],
            "send_in_flight": 0,
            "recv_in_flight": 0,
            "bytes_sent": 0,
            "bytes_received": 0,
            "local_end": False,
            "remote_end": False,
            "reset": False,
            "opened": False,
            "stream_seq": 0,
            "last_event_seq": 0,
            "reclaimed_to_connection": 0,
            "schedule_rank": None,
            "header_complete": False,
            "header_digest": None,
            "header_size": 0,
            "header_end_stream": False,
            "header_end_seq": None,
            "expected_final_state": stream.get("expected_final_state"),
            "forced_end": {"local": False, "remote": False},
        }
        streams[_stream_key(stream_id)] = entry
    state: dict[str, Any] = {
        "format": "h2diag.state/v1",
        "connection_id": scenario["connection_id"],
        "scenario_id": scenario["scenario_id"],
        "patch_version": patch_version,
        "committed_seq": 0,
        "clock_ms": 0,
        "event_count": 0,
        "settings": {
            "peer": copy.deepcopy(settings),
            "local": copy.deepcopy(settings),
        },
        "settings_ack_pending_send": 0,
        "settings_ack_pending_recv": 0,
        "connection": {
            "send_window": connection_doc["initial_send_window"],
            "recv_window": connection_doc["initial_recv_window"],
            "max_send_window": connection_doc["max_send_window"],
            "max_recv_window": connection_doc["max_recv_window"],
            "send_in_flight": 0,
            "recv_in_flight": 0,
            "bytes_sent": 0,
            "bytes_received": 0,
            "goaway": {
                "seen": False,
                "last_stream_id": None,
                "direction": None,
                "error_code": 0,
            },
        },
        "streams": streams,
        "headers": {
            "pending": None,
            "history": [],
        },
        "ping": {
            "pending_send_acks": [],
            "pending_recv_acks": [],
        },
        "scheduler": {
            "policy": "swrr-v1",
            "tie_break": "stream_id_asc",
            "ranks": [],
            "plan": [],
            "committed_order": [],
        },
        "last_frame": None,
        "chain_digest": None,
        "state_digest": None,
    }
    apply_ranks(state)
    return finalize_state(state, None)


class Engine:
    """Pure deterministic event engine.

    The engine never reads a wall clock. The only time value carried by state
    is the non-decreasing ``clock_ms`` supplied by scenario events.
    """

    def __init__(self, scenario: dict[str, Any], state: dict[str, Any] | None = None) -> None:
        self.scenario = scenario
        self.state = copy.deepcopy(state) if state is not None else initial_state(scenario)

    def clone(self) -> "Engine":
        return Engine(self.scenario, self.state)

    def process(self, event: dict[str, Any]) -> dict[str, Any]:
        state = copy.deepcopy(self.state)
        self._process_in_place(state, event)
        self.state = state
        return state

    def run_events(self, events: list[dict[str, Any]]) -> dict[str, Any]:
        for event in events:
            self.process(event)
        return self.state

    def run_all(self, *, check_expected: bool = True) -> dict[str, Any]:
        self.run_events(self.scenario["events"])
        if check_expected:
            self.check_expected()
        return self.state

    def check_expected(self) -> None:
        for declared in self.scenario["streams"]:
            expected = declared.get("expected_final_state")
            if expected is None:
                continue
            actual = self.state["streams"][_stream_key(declared["stream_id"])]
            if actual["state"] != expected:
                raise error(
                    EXPECTED_STATE_001,
                    "simulated final state does not match the stream declaration",
                    details={
                        "stream_id": declared["stream_id"],
                        "expected": expected,
                        "actual": actual["state"],
                        "committed_seq": self.state["committed_seq"],
                    },
                )
        expected_root = self.scenario.get("expected", {})
        if not expected_root:
            return
        connection_expected = expected_root.get("connection", {})
        for key, expected in connection_expected.items():
            actual = self.state["connection"].get(key)
            if actual != expected:
                raise error(
                    EXPECTED_STATE_001,
                    "connection expectation mismatch",
                    details={"field": key, "expected": expected, "actual": actual},
                )
        for stream_id, expected_fields in expected_root.get("streams", {}).items():
            actual_stream = _get_stream(self.state, int(stream_id))
            for key, expected in expected_fields.items():
                if actual_stream.get(key) != expected:
                    raise error(
                        EXPECTED_STATE_001,
                        "stream expectation mismatch",
                        details={
                            "stream_id": int(stream_id),
                            "field": key,
                            "expected": expected,
                            "actual": actual_stream.get(key),
                        },
                    )

    def _process_in_place(self, state: dict[str, Any], event: dict[str, Any]) -> None:
        seq = _int_field(event, "seq")
        clock_ms = _int_field(event, "clock_ms")
        if seq != state["committed_seq"] + 1:
            raise error(
                EVENT_SEQ_001,
                "events must commit contiguously at the persisted watermark",
                details={"expected": state["committed_seq"] + 1, "actual": seq},
            )
        if clock_ms < state["clock_ms"]:
            raise error(
                CLOCK_001,
                "logical clock must be non-decreasing",
                details={"watermark": state["clock_ms"], "event_clock": clock_ms},
            )
        event_type = event["type"]
        direction = event.get("direction", "send")
        stream_id = event.get("stream_id")
        stream: dict[str, Any] | None = None
        if stream_id is not None:
            stream = _get_stream(state, int(stream_id))

        if event_type == "HEADERS":
            self._headers(state, stream, event, direction)
        elif event_type == "CONTINUATION":
            self._continuation(state, stream, event, direction)
        elif event_type == "DATA":
            self._data(state, stream, event, direction)
        elif event_type == "RST_STREAM":
            self._rst_stream(state, stream, event, direction)
        elif event_type == "WINDOW_UPDATE":
            self._window_update(state, stream, event, direction)
        elif event_type in {"SETTINGS", "SETTINGS_ACK"}:
            self._settings(state, event, direction)
        elif event_type in {"PING", "PING_ACK"}:
            self._ping(state, event, direction)
        elif event_type == "GOAWAY":
            self._goaway(state, event, direction)
        else:
            raise error(FRAME_001, "unsupported frame type", details={"type": event_type})

        stream_seq = None
        if stream is not None:
            stream["last_event_seq"] = seq
            stream["stream_seq"] += 1
            stream_seq = stream["stream_seq"]
        event_digest = digest_json(event)
        state["clock_ms"] = clock_ms
        state["committed_seq"] = seq
        state["event_count"] += 1
        state["last_frame"] = {
            "seq": seq,
            "clock_ms": clock_ms,
            "type": event_type,
            "direction": direction,
            "stream_id": stream_id,
            "peer_stream_seq": stream_seq,
            "event_digest": event_digest,
        }
        apply_ranks(state)
        finalize_state(state, event_digest)

    def _headers(
        self,
        state: dict[str, Any],
        stream: dict[str, Any] | None,
        event: dict[str, Any],
        direction: str,
    ) -> None:
        if stream is None:
            raise error(HEADER_ORDER_001, "HEADERS requires a stream id")
        if state["headers"]["pending"] is not None:
            raise error(
                HEADER_ORDER_001,
                "HEADERS cannot interrupt an unfinished header block",
                details={"pending": state["headers"]["pending"]["start_seq"]},
            )
        _assert_goaway_allows(state, stream["stream_id"])
        if stream["state"] == "idle" and state["connection"]["goaway"]["seen"]:
            raise error(
                GOAWAY_001,
                "GOAWAY prevents opening a new stream",
                details={"stream_id": stream["stream_id"]},
            )
        if stream["state"] != "idle":
            if direction == "send":
                _assert_can_send(stream)
            else:
                _assert_can_receive(stream)
        elif stream["reset"]:
            raise error(
                FRAME_STATE_001,
                "a reset stream cannot be reopened",
                details={"stream_id": stream["stream_id"]},
            )
        _open_stream(stream)
        end_stream = bool(event.get("end_stream", False))
        pending = {
            "stream_id": stream["stream_id"],
            "direction": direction,
            "start_seq": event["seq"],
            "fragments": [event.get("fragment_hex", "")],
            "end_stream": end_stream,
            "patch_version": state["patch_version"],
        }
        if event.get("end_headers", False):
            self._finish_headers(state, stream, pending, event["seq"])
        else:
            state["headers"]["pending"] = pending
        if end_stream:
            _end_side(stream, "local" if direction == "send" else "remote")

    def _continuation(
        self,
        state: dict[str, Any],
        stream: dict[str, Any] | None,
        event: dict[str, Any],
        direction: str,
    ) -> None:
        pending = state["headers"]["pending"]
        if stream is None or pending is None:
            raise error(
                HEADER_ORDER_001,
                "CONTINUATION has no matching HEADERS block",
                details={"stream_id": event.get("stream_id"), "seq": event["seq"]},
            )
        if (
            pending["stream_id"] != stream["stream_id"]
            or pending["direction"] != direction
            or pending["start_seq"] >= event["seq"]
        ):
            raise error(
                HEADER_ORDER_001,
                "CONTINUATION must immediately continue the same HEADERS block",
                details={
                    "pending_stream_id": pending["stream_id"],
                    "pending_start_seq": pending["start_seq"],
                    "actual_stream_id": stream["stream_id"],
                    "actual_seq": event["seq"],
                },
            )
        pending["fragments"].append(event.get("fragment_hex", ""))
        if event.get("end_headers", False):
            self._finish_headers(state, stream, pending, event["seq"])

    def _finish_headers(
        self,
        state: dict[str, Any],
        stream: dict[str, Any],
        pending: dict[str, Any],
        end_seq: int,
    ) -> None:
        raw = bytes.fromhex("".join(pending["fragments"]))
        digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        index = len(state["headers"]["history"]) + 1
        block = {
            "block_index": index,
            "stream_id": stream["stream_id"],
            "direction": pending["direction"],
            "start_seq": pending["start_seq"],
            "end_seq": end_seq,
            "fragment_count": len(pending["fragments"]),
            "fragments_hex": list(pending["fragments"]),
            "block_hex": raw.hex(),
            "size": len(raw),
            "digest": digest,
            "end_stream": pending["end_stream"],
            "patch_version": pending["patch_version"],
        }
        state["headers"]["history"].append(block)
        stream["header_complete"] = True
        stream["header_digest"] = digest
        stream["header_size"] = len(raw)
        stream["header_end_stream"] = pending["end_stream"]
        stream["header_end_seq"] = end_seq
        state["headers"]["pending"] = None

    def _data(
        self,
        state: dict[str, Any],
        stream: dict[str, Any] | None,
        event: dict[str, Any],
        direction: str,
    ) -> None:
        if stream is None:
            raise error(FRAME_001, "DATA requires a stream id")
        _assert_goaway_allows(state, stream["stream_id"])
        length = int(event.get("length", 0))
        if length < 0:
            raise error(FRAME_001, "DATA length must be non-negative")
        if direction == "send":
            _assert_can_send(stream)
            connection = state["connection"]
            if length > stream["send_window"] or length > connection["send_window"]:
                raise error(
                    FLOW_001,
                    "DATA exceeds the available stream/connection send window",
                    details={
                        "stream_id": stream["stream_id"],
                        "length": length,
                        "stream_send_window": stream["send_window"],
                        "connection_send_window": connection["send_window"],
                    },
                )
            stream["send_window"] -= length
            connection["send_window"] -= length
            stream["send_in_flight"] += length
            connection["send_in_flight"] += length
            stream["bytes_sent"] += length
            connection["bytes_sent"] += length
            state["scheduler"]["committed_order"].append(
                {
                    "seq": event["seq"],
                    "stream_id": stream["stream_id"],
                    "length": length,
                    "weight": stream["weight"],
                }
            )
            if event.get("end_stream", False):
                _end_side(stream, "local")
            return
        _assert_can_receive(stream)
        connection = state["connection"]
        if length > stream["recv_window"] or length > connection["recv_window"]:
            raise error(
                FLOW_001,
                "DATA exceeds the available stream/connection receive window",
                details={
                    "stream_id": stream["stream_id"],
                    "length": length,
                    "stream_recv_window": stream["recv_window"],
                    "connection_recv_window": connection["recv_window"],
                },
            )
        stream["recv_window"] -= length
        connection["recv_window"] -= length
        stream["recv_in_flight"] += length
        connection["recv_in_flight"] += length
        stream["bytes_received"] += length
        connection["bytes_received"] += length
        if event.get("end_stream", False):
            _end_side(stream, "remote")

    def _rst_stream(
        self,
        state: dict[str, Any],
        stream: dict[str, Any] | None,
        event: dict[str, Any],
        direction: str,
    ) -> None:
        if stream is None:
            raise error(FRAME_001, "RST_STREAM requires a stream id")
        if stream["state"] == "closed":
            return
        if stream["state"] == "idle":
            raise error(
                FRAME_STATE_001,
                "RST_STREAM cannot target an idle stream",
                details={"stream_id": stream["stream_id"]},
            )
        _close_stream(state, stream, reset=True, reclaim=True)
        stream["rst_direction"] = direction
        stream["rst_error_code"] = int(event.get("error_code", 0))

    def _window_update(
        self,
        state: dict[str, Any],
        stream: dict[str, Any] | None,
        event: dict[str, Any],
        direction: str,
    ) -> None:
        increment = int(event.get("window_increment", event.get("increment", 0)))
        if increment < 0:
            raise error(WINDOW_UPDATE_001, "WINDOW_UPDATE increment must be non-negative")
        level = event.get("level")
        if level is None:
            level = "stream" if stream is not None else "connection"
        if level == "connection":
            if stream is not None:
                raise error(
                    WINDOW_UPDATE_001,
                    "connection-level WINDOW_UPDATE must not carry a stream id",
                )
            connection = state["connection"]
            if direction == "recv":
                new_value = connection["send_window"] + increment
                if new_value > connection["max_send_window"]:
                    raise error(
                        WINDOW_UPDATE_001,
                        "connection send credit would exceed its upper bound",
                        details={
                            "current": connection["send_window"],
                            "increment": increment,
                            "max": connection["max_send_window"],
                        },
                    )
                connection["send_window"] = new_value
                connection["send_in_flight"] = max(
                    0, connection["send_in_flight"] - increment
                )
            else:
                new_value = connection["recv_window"] + increment
                if new_value > connection["max_recv_window"]:
                    raise error(
                        WINDOW_UPDATE_001,
                        "connection receive credit would exceed its upper bound",
                        details={
                            "current": connection["recv_window"],
                            "increment": increment,
                            "max": connection["max_recv_window"],
                        },
                    )
                connection["recv_window"] = new_value
                connection["recv_in_flight"] = max(
                    0, connection["recv_in_flight"] - increment
                )
            return
        if level != "stream" or stream is None:
            raise error(
                WINDOW_UPDATE_001,
                "stream-level WINDOW_UPDATE requires a declared stream",
            )
        if stream["reset"] or stream["state"] == "closed":
            raise error(
                FRAME_STATE_001,
                "closed/reset stream cannot accept stream credit",
                details={"stream_id": stream["stream_id"]},
            )
        if stream["state"] == "idle":
            raise error(
                FRAME_STATE_001,
                "idle stream cannot accept stream credit",
                details={"stream_id": stream["stream_id"]},
            )
        if direction == "recv":
            new_value = stream["send_window"] + increment
            if new_value > stream["max_send_window"]:
                raise error(
                    WINDOW_UPDATE_001,
                    "stream send credit would exceed its upper bound",
                    details={
                        "stream_id": stream["stream_id"],
                        "current": stream["send_window"],
                        "increment": increment,
                        "max": stream["max_send_window"],
                    },
                )
            stream["send_window"] = new_value
            stream["send_in_flight"] = max(0, stream["send_in_flight"] - increment)
        else:
            new_value = stream["recv_window"] + increment
            if new_value > stream["max_recv_window"]:
                raise error(
                    WINDOW_UPDATE_001,
                    "stream receive credit would exceed its upper bound",
                    details={
                        "stream_id": stream["stream_id"],
                        "current": stream["recv_window"],
                        "increment": increment,
                        "max": stream["max_recv_window"],
                    },
                )
            stream["recv_window"] = new_value
            stream["recv_in_flight"] = max(0, stream["recv_in_flight"] - increment)

    def _settings(
        self,
        state: dict[str, Any],
        event: dict[str, Any],
        direction: str,
    ) -> None:
        is_ack = event["type"] == "SETTINGS_ACK" or bool(event.get("ack", False))
        if is_ack:
            if event.get("settings"):
                raise error(SETTINGS_001, "SETTINGS ACK must not carry settings")
            if direction == "send":
                if state["settings_ack_pending_send"] <= 0:
                    raise error(
                        SETTINGS_ACK_001,
                        "SETTINGS ACK has no corresponding inbound SETTINGS",
                    )
                state["settings_ack_pending_send"] -= 1
            else:
                if state["settings_ack_pending_recv"] <= 0:
                    raise error(
                        SETTINGS_ACK_001,
                        "SETTINGS ACK has no corresponding outbound SETTINGS",
                    )
                state["settings_ack_pending_recv"] -= 1
            return
        settings = event.get("settings") or {}
        if not isinstance(settings, dict):
            raise error(SETTINGS_001, "SETTINGS payload must be an object")
        side = "peer" if direction == "recv" else "local"
        old_settings = state["settings"][side]
        old_initial = int(old_settings.get("initial_window_size", 65535))
        new_initial = int(settings.get("initial_window_size", old_initial))
        if new_initial < 0:
            raise error(SETTINGS_001, "initial_window_size must be non-negative")
        if new_initial > 2**31 - 1:
            raise error(SETTINGS_001, "initial_window_size exceeds the HTTP/2 limit")
        delta = new_initial - old_initial
        for stream in state["streams"].values():
            if stream["reset"] or stream["state"] == "closed":
                continue
            if direction == "recv":
                new_window = stream["send_window"] + delta
                maximum = stream["max_send_window"]
                field = "send_window"
            else:
                new_window = stream["recv_window"] + delta
                maximum = stream["max_recv_window"]
                field = "recv_window"
            if new_window < 0:
                raise error(
                    SETTINGS_001,
                    "SETTINGS_INITIAL_WINDOW_SIZE would make a stream window negative",
                    details={
                        "stream_id": stream["stream_id"],
                        "field": field,
                        "old": stream[field],
                        "delta": delta,
                    },
                )
            if new_window > maximum:
                raise error(
                    SETTINGS_001,
                    "SETTINGS_INITIAL_WINDOW_SIZE would exceed a stream upper bound",
                    details={
                        "stream_id": stream["stream_id"],
                        "field": field,
                        "new": new_window,
                        "max": maximum,
                    },
                )
            stream[field] = new_window
        merged = dict(old_settings)
        merged.update(settings)
        state["settings"][side] = merged
        if direction == "recv":
            state["settings_ack_pending_send"] += 1
        else:
            state["settings_ack_pending_recv"] += 1

    def _ping(
        self,
        state: dict[str, Any],
        event: dict[str, Any],
        direction: str,
    ) -> None:
        payload = event.get("payload_hex", "0000000000000000")
        try:
            raw = bytes.fromhex(payload)
        except ValueError as exc:
            raise error(PING_001, "PING payload is not valid hex") from exc
        if len(raw) != 8:
            raise error(PING_001, "PING payload must be exactly 8 bytes")
        is_ack = event["type"] == "PING_ACK" or bool(event.get("ack", False))
        if is_ack:
            if direction == "send":
                queue = state["ping"]["pending_send_acks"]
            else:
                queue = state["ping"]["pending_recv_acks"]
            if payload not in queue:
                raise error(
                    PING_001,
                    "PING ACK payload has no pending frame",
                    details={"payload_hex": payload, "direction": direction},
                )
            queue.remove(payload)
            return
        if direction == "recv":
            state["ping"]["pending_send_acks"].append(payload)
        else:
            state["ping"]["pending_recv_acks"].append(payload)

    def _goaway(
        self,
        state: dict[str, Any],
        event: dict[str, Any],
        direction: str,
    ) -> None:
        last_stream_id = int(event.get("last_stream_id", 0))
        if last_stream_id < 0:
            raise error(GOAWAY_001, "GOAWAY last_stream_id must be non-negative")
        max_declared = max(
            (stream["stream_id"] for stream in state["streams"].values()),
            default=0,
        )
        if last_stream_id > max_declared:
            raise error(
                GOAWAY_001,
                "GOAWAY last_stream_id is beyond the declared stream range",
                details={"last_stream_id": last_stream_id, "max_declared": max_declared},
            )
        state["connection"]["goaway"] = {
            "seen": True,
            "last_stream_id": last_stream_id,
            "direction": direction,
            "error_code": int(event.get("error_code", 0)),
        }
        for stream in state["streams"].values():
            if stream["stream_id"] > last_stream_id and stream["state"] != "closed":
                _close_stream(state, stream, reset=False, reclaim=True)
