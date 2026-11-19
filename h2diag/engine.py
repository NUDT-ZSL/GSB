from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Iterable

from .canonical import digest_value
from .errors import (
    E_BAD_SCENARIO,
    E_FLOW_CONTROL,
    E_GOAWAY_STREAM,
    E_HEADERS_INCOMPLETE,
    E_HEADERS_SEQUENCE,
    E_PING_STATE,
    E_SETTINGS_STATE,
    E_STREAM_STATE,
    E_UNKNOWN_STREAM,
    E_WINDOW_OVERFLOW,
    EngineError,
)
from .scenario import Scenario


SENDABLE_STATES = {"open", "half-closed-remote"}
ACTIVE_STATES = {"open", "half-closed-local", "half-closed-remote"}
TERMINAL_STATES = {"closed", "error"}
SCHEDULER_SCALE = 1_000_000
SCHEDULER_QUANTUM_PER_WEIGHT = 128


def _engine_error(code: str, message: str, **details: Any) -> None:
    raise EngineError(code, message, details)


@dataclass
class StreamState:
    stream_id: int
    weight: int
    state: str
    initial_send_window: int
    initial_recv_window: int
    send_window: int
    recv_window: int
    send_window_updates: int = 0
    recv_window_updates: int = 0
    bytes_sent: int = 0
    bytes_received: int = 0
    unacked_conn: int = 0
    unacked_stream: int = 0
    first_unacked_seq: int = 0
    pending_outbound: int = 0
    pending_end_stream: bool = False
    header_blocks: list[dict[str, Any]] = field(default_factory=list)
    reset_error_code: int | None = None
    terminal_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "bytes_received": self.bytes_received,
            "bytes_sent": self.bytes_sent,
            "first_unacked_seq": self.first_unacked_seq,
            "header_blocks": self.header_blocks,
            "initial_recv_window": self.initial_recv_window,
            "initial_send_window": self.initial_send_window,
            "pending_end_stream": self.pending_end_stream,
            "pending_outbound": self.pending_outbound,
            "recv_window": self.recv_window,
            "recv_window_updates": self.recv_window_updates,
            "reset_error_code": self.reset_error_code,
            "send_window": self.send_window,
            "send_window_updates": self.send_window_updates,
            "state": self.state,
            "stream_id": self.stream_id,
            "terminal_reason": self.terminal_reason,
            "unacked_conn": self.unacked_conn,
            "unacked_stream": self.unacked_stream,
            "weight": self.weight,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "StreamState":
        return cls(
            stream_id=int(value["stream_id"]),
            weight=int(value["weight"]),
            state=str(value["state"]),
            initial_send_window=int(value["initial_send_window"]),
            initial_recv_window=int(value["initial_recv_window"]),
            send_window=int(value["send_window"]),
            recv_window=int(value["recv_window"]),
            send_window_updates=int(value["send_window_updates"]),
            recv_window_updates=int(value["recv_window_updates"]),
            bytes_sent=int(value["bytes_sent"]),
            bytes_received=int(value["bytes_received"]),
            unacked_conn=int(value["unacked_conn"]),
            unacked_stream=int(value["unacked_stream"]),
            first_unacked_seq=int(value["first_unacked_seq"]),
            pending_outbound=int(value["pending_outbound"]),
            pending_end_stream=bool(value["pending_end_stream"]),
            header_blocks=copy.deepcopy(value["header_blocks"]),
            reset_error_code=value["reset_error_code"],
            terminal_reason=value["terminal_reason"],
        )


@dataclass
class HeaderAssembly:
    stream_id: int
    direction: str
    started_seq: int
    fragments: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "direction": self.direction,
            "fragments_hex": "".join(self.fragments),
            "started_seq": self.started_seq,
            "stream_id": self.stream_id,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> "HeaderAssembly | None":
        if value is None:
            return None
        return cls(
            stream_id=int(value["stream_id"]),
            direction=str(value["direction"]),
            started_seq=int(value["started_seq"]),
            fragments=[str(value.get("fragments_hex", ""))],
        )


@dataclass
class ConnectionState:
    initial_send_window: int
    initial_recv_window: int
    send_window: int
    recv_window: int
    max_window: int
    max_concurrent_streams: int
    send_window_updates: int = 0
    recv_window_updates: int = 0
    unacked_send: int = 0
    reset_reclaimed: int = 0
    discarded_reset_credit: int = 0
    bytes_sent_total: int = 0
    bytes_received_total: int = 0
    settings: dict[str, int] = field(default_factory=dict)
    settings_ack_out: int = 0
    settings_ack_in: int = 0
    pending_pings_out: list[str] = field(default_factory=list)
    pending_pings_in: list[str] = field(default_factory=list)
    goaway: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "bytes_received_total": self.bytes_received_total,
            "bytes_sent_total": self.bytes_sent_total,
            "discarded_reset_credit": self.discarded_reset_credit,
            "goaway": self.goaway,
            "initial_recv_window": self.initial_recv_window,
            "initial_send_window": self.initial_send_window,
            "max_concurrent_streams": self.max_concurrent_streams,
            "max_window": self.max_window,
            "pending_pings_in": self.pending_pings_in,
            "pending_pings_out": self.pending_pings_out,
            "recv_window": self.recv_window,
            "recv_window_updates": self.recv_window_updates,
            "reset_reclaimed": self.reset_reclaimed,
            "send_window": self.send_window,
            "send_window_updates": self.send_window_updates,
            "settings": self.settings,
            "settings_ack_in": self.settings_ack_in,
            "settings_ack_out": self.settings_ack_out,
            "unacked_send": self.unacked_send,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ConnectionState":
        return cls(
            initial_send_window=int(value["initial_send_window"]),
            initial_recv_window=int(value["initial_recv_window"]),
            send_window=int(value["send_window"]),
            recv_window=int(value["recv_window"]),
            max_window=int(value["max_window"]),
            max_concurrent_streams=int(value["max_concurrent_streams"]),
            send_window_updates=int(value["send_window_updates"]),
            recv_window_updates=int(value["recv_window_updates"]),
            unacked_send=int(value["unacked_send"]),
            reset_reclaimed=int(value["reset_reclaimed"]),
            discarded_reset_credit=int(value["discarded_reset_credit"]),
            bytes_sent_total=int(value["bytes_sent_total"]),
            bytes_received_total=int(value["bytes_received_total"]),
            settings={str(k): int(v) for k, v in value["settings"].items()},
            settings_ack_out=int(value["settings_ack_out"]),
            settings_ack_in=int(value["settings_ack_in"]),
            pending_pings_out=list(value["pending_pings_out"]),
            pending_pings_in=list(value["pending_pings_in"]),
            goaway=copy.deepcopy(value["goaway"]),
        )


@dataclass
class EngineState:
    scenario_id: str
    initiator: str
    logical_clock_ms: int
    committed_seq: int
    connection: ConnectionState
    streams: dict[int, StreamState]
    header_assembly: HeaderAssembly | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "committed_seq": self.committed_seq,
            "connection": self.connection.to_dict(),
            "header_assembly": (
                self.header_assembly.to_dict() if self.header_assembly else None
            ),
            "initiator": self.initiator,
            "logical_clock_ms": self.logical_clock_ms,
            "scenario_id": self.scenario_id,
            "streams": [
                self.streams[stream_id].to_dict()
                for stream_id in sorted(self.streams)
            ],
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "EngineState":
        streams = {
            int(item["stream_id"]): StreamState.from_dict(item)
            for item in value["streams"]
        }
        return cls(
            scenario_id=str(value["scenario_id"]),
            initiator=str(value["initiator"]),
            logical_clock_ms=int(value["logical_clock_ms"]),
            committed_seq=int(value["committed_seq"]),
            connection=ConnectionState.from_dict(value["connection"]),
            streams=streams,
            header_assembly=HeaderAssembly.from_dict(value.get("header_assembly")),
        )

    def digest(self) -> str:
        return digest_value(self.to_dict())


@dataclass
class FrameOutcome:
    seq: int
    at_ms: int
    event_type: str
    allocations: list[dict[str, int]]
    state_digest: str
    event_digest: str
    ledger: dict[str, Any]


def build_initial_state(scenario: Scenario) -> EngineState:
    connection = ConnectionState(
        initial_send_window=scenario.connection["send_window"],
        initial_recv_window=scenario.connection["recv_window"],
        send_window=scenario.connection["send_window"],
        recv_window=scenario.connection["recv_window"],
        max_window=scenario.connection["max_window"],
        max_concurrent_streams=scenario.connection["max_concurrent_streams"],
    )
    streams = {
        declaration.stream_id: StreamState(
            stream_id=declaration.stream_id,
            weight=declaration.weight,
            state="idle",
            initial_send_window=declaration.initial_send_window,
            initial_recv_window=declaration.initial_recv_window,
            send_window=declaration.initial_send_window,
            recv_window=declaration.initial_recv_window,
        )
        for declaration in scenario.streams
    }
    return EngineState(
        scenario_id=scenario.scenario_id,
        initiator=scenario.initiator,
        logical_clock_ms=0,
        committed_seq=0,
        connection=connection,
        streams=streams,
    )


class Engine:
    def __init__(self, scenario: Scenario, state: EngineState | None = None):
        self.scenario = scenario
        self.state = copy.deepcopy(state) if state is not None else build_initial_state(
            scenario
        )
        self._declaration_by_id = {
            declaration.stream_id: declaration for declaration in scenario.streams
        }

    def _stream(self, stream_id: int) -> StreamState:
        try:
            return self.state.streams[stream_id]
        except KeyError:
            raise EngineError(
                E_UNKNOWN_STREAM,
                f"unknown stream {stream_id}",
                {"stream_id": stream_id, "seq": self.state.committed_seq + 1},
            ) from None

    def _context(self, event: dict[str, Any]) -> dict[str, Any]:
        context: dict[str, Any] = {
            "seq": event["seq"],
            "type": event["type"],
            "at_ms": event["at_ms"],
        }
        if event.get("stream_id") is not None:
            context["stream_id"] = event["stream_id"]
        return context

    def _fail_event(
        self, event: dict[str, Any], code: str, message: str, **details: Any
    ) -> None:
        context = self._context(event)
        context.update(details)
        raise EngineError(code, message, context)

    def _check_header_interlock(self, event: dict[str, Any]) -> None:
        assembly = self.state.header_assembly
        if assembly is None:
            if event["type"] == "CONTINUATION":
                self._fail_event(
                    event,
                    E_HEADERS_SEQUENCE,
                    "CONTINUATION received without an open header block",
                )
            return
        if event["type"] != "CONTINUATION":
            self._fail_event(
                event,
                E_HEADERS_SEQUENCE,
                "only CONTINUATION may follow a HEADERS block before END_HEADERS",
                open_stream_id=assembly.stream_id,
                open_direction=assembly.direction,
            )
        if (
            event.get("stream_id") != assembly.stream_id
            or event.get("direction") != assembly.direction
        ):
            self._fail_event(
                event,
                E_HEADERS_SEQUENCE,
                "CONTINUATION stream/direction does not match the open header block",
                open_stream_id=assembly.stream_id,
                open_direction=assembly.direction,
            )

    def _require_direction_state(
        self, event: dict[str, Any], stream: StreamState
    ) -> None:
        direction = event["direction"]
        allowed = (
            {"open", "half-closed-local"}
            if direction == "inbound"
            else SENDABLE_STATES
        )
        if stream.state not in allowed:
            self._fail_event(
                event,
                E_STREAM_STATE,
                f"{event['type']} is not allowed for {direction} "
                f"on stream state {stream.state}",
                state=stream.state,
                direction=direction,
            )

    def _active_stream_count(self) -> int:
        return sum(
            1 for stream in self.state.streams.values() if stream.state in ACTIVE_STATES
        )

    def _check_goaway_new_stream(self, event: dict[str, Any], stream: StreamState) -> None:
        goaway = self.state.connection.goaway
        if goaway is not None and stream.state == "idle":
            if stream.stream_id > int(goaway["last_stream_id"]):
                self._fail_event(
                    event,
                    E_GOAWAY_STREAM,
                    "new stream exceeds GOAWAY last_stream_id",
                    last_stream_id=int(goaway["last_stream_id"]),
                )

    def _apply_end_stream(
        self, event: dict[str, Any], stream: StreamState, direction: str
    ) -> None:
        if direction == "inbound":
            if stream.state == "open":
                stream.state = "half-closed-remote"
            elif stream.state == "half-closed-local":
                stream.state = "closed"
                stream.terminal_reason = "end_stream_both_sides"
            else:
                self._fail_event(
                    event,
                    E_STREAM_STATE,
                    "inbound END_STREAM is invalid in the current state",
                    state=stream.state,
                )
        else:
            if stream.state == "open":
                stream.state = "half-closed-local"
            elif stream.state == "half-closed-remote":
                stream.state = "closed"
                stream.terminal_reason = "end_stream_both_sides"
            else:
                self._fail_event(
                    event,
                    E_STREAM_STATE,
                    "outbound END_STREAM is invalid in the current state",
                    state=stream.state,
                )

    def _open_stream(
        self, event: dict[str, Any], stream: StreamState, direction: str, end_stream: bool
    ) -> None:
        self._check_goaway_new_stream(event, stream)
        if self._active_stream_count() >= self.state.connection.max_concurrent_streams:
            self._fail_event(
                event,
                E_STREAM_STATE,
                "max_concurrent_streams would be exceeded",
                max_concurrent_streams=self.state.connection.max_concurrent_streams,
            )
        stream.state = "open"
        if end_stream:
            self._apply_end_stream(event, stream, direction)

    def _start_or_continue_headers(
        self, event: dict[str, Any], stream: StreamState
    ) -> None:
        direction = event["direction"]
        end_stream = bool(event.get("end_stream", False))
        if stream.state == "idle":
            self._open_stream(event, stream, direction, end_stream)
        else:
            self._require_direction_state(event, stream)
            if end_stream:
                self._apply_end_stream(event, stream, direction)

        fragment = str(event.get("fragment_hex", ""))
        if event["type"] == "HEADERS":
            if event.get("end_headers", False):
                self._complete_header_block(
                    event, stream, [fragment], event["seq"], event["seq"]
                )
            else:
                self.state.header_assembly = HeaderAssembly(
                    stream_id=stream.stream_id,
                    direction=direction,
                    started_seq=event["seq"],
                    fragments=[fragment],
                )
        else:
            assert self.state.header_assembly is not None
            self.state.header_assembly.fragments.append(fragment)
            if event.get("end_headers", False):
                assembly = self.state.header_assembly
                self._complete_header_block(
                    event,
                    stream,
                    assembly.fragments,
                    assembly.started_seq,
                    event["seq"],
                )
                self.state.header_assembly = None

    def _complete_header_block(
        self,
        event: dict[str, Any],
        stream: StreamState,
        fragments: Iterable[str],
        started_seq: int,
        ended_seq: int,
    ) -> None:
        try:
            block = bytes.fromhex("".join(fragments))
        except ValueError:
            self._fail_event(
                event, E_HEADERS_SEQUENCE, "header block is not valid hexadecimal"
            )
        stream.header_blocks.append(
            {
                "block_digest": digest_value(block.hex()),
                "block_length": len(block),
                "direction": event["direction"],
                "ended_seq": ended_seq,
                "started_seq": started_seq,
            }
        )

    def _send_bytes(
        self, stream: StreamState, amount: int, event_seq: int
    ) -> dict[str, int]:
        if amount <= 0:
            return {"bytes": 0, "stream_id": stream.stream_id}
        if stream.unacked_conn == 0:
            stream.first_unacked_seq = event_seq
        stream.send_window -= amount
        stream.bytes_sent += amount
        stream.unacked_conn += amount
        stream.unacked_stream += amount
        self.state.connection.send_window -= amount
        self.state.connection.unacked_send += amount
        self.state.connection.bytes_sent_total += amount
        return {"bytes": amount, "stream_id": stream.stream_id}

    def _drain_scheduler(self) -> list[dict[str, int]]:
        allocations: list[dict[str, int]] = []
        while self.state.connection.send_window > 0:
            candidates = [
                stream
                for stream in self.state.streams.values()
                if stream.state in SENDABLE_STATES
                and stream.pending_outbound > 0
                and stream.send_window > 0
            ]
            if not candidates:
                break
            candidates.sort(
                key=lambda stream: (
                    (stream.bytes_sent * SCHEDULER_SCALE) // stream.weight,
                    stream.stream_id,
                )
            )
            stream = candidates[0]
            quantum = stream.weight * SCHEDULER_QUANTUM_PER_WEIGHT
            amount = min(
                quantum,
                stream.pending_outbound,
                stream.send_window,
                self.state.connection.send_window,
            )
            if amount <= 0:
                break
            allocations.append(
                self._send_bytes(
                    stream, amount, event_seq=self.state.committed_seq + 1
                )
            )
            stream.pending_outbound -= amount
            if stream.pending_outbound == 0 and stream.pending_end_stream:
                stream.pending_end_stream = False
                end_event = {
                    "seq": self.state.committed_seq + 1,
                    "type": "DATA",
                    "direction": "outbound",
                    "stream_id": stream.stream_id,
                }
                self._apply_end_stream(end_event, stream, "outbound")
        return allocations

    def _apply_data(self, event: dict[str, Any]) -> list[dict[str, int]]:
        stream = self._stream(event["stream_id"])
        self._require_direction_state(event, stream)
        length = int(event["length"])
        direction = event["direction"]
        if direction == "inbound":
            available = min(
                self.state.connection.recv_window, stream.recv_window
            )
            if length > available:
                self._fail_event(
                    event,
                    E_FLOW_CONTROL,
                    "inbound DATA exceeds connection or stream receive window",
                    available=available,
                    requested=length,
                )
            self.state.connection.recv_window -= length
            self.state.connection.bytes_received_total += length
            stream.recv_window -= length
            stream.bytes_received += length
            if event.get("end_stream", False):
                self._apply_end_stream(event, stream, "inbound")
            return []

        if stream.pending_end_stream:
            self._fail_event(
                event,
                E_STREAM_STATE,
                "outbound DATA follows a pending END_STREAM",
                state=stream.state,
            )
        stream.pending_outbound += length
        if event.get("end_stream", False):
            stream.pending_end_stream = True
            if stream.pending_outbound == 0:
                stream.pending_end_stream = False
                self._apply_end_stream(event, stream, "outbound")
        return self._drain_scheduler()

    def _apply_rst_stream(self, event: dict[str, Any]) -> None:
        stream = self._stream(event["stream_id"])
        if stream.state == "idle":
            self._fail_event(
                event,
                E_STREAM_STATE,
                "RST_STREAM on an idle stream is invalid",
                state=stream.state,
            )
        if stream.state in TERMINAL_STATES:
            self._fail_event(
                event,
                E_STREAM_STATE,
                "RST_STREAM cannot revive or retransition a terminal stream",
                state=stream.state,
            )

        reclaimed = stream.unacked_conn
        self.state.connection.unacked_send = max(
            0, self.state.connection.unacked_send - reclaimed
        )
        stream.unacked_conn = 0
        stream.unacked_stream = 0
        stream.pending_outbound = 0
        stream.pending_end_stream = False
        room = self.state.connection.max_window - self.state.connection.send_window
        credited = min(reclaimed, max(0, room))
        self.state.connection.send_window += credited
        self.state.connection.discarded_reset_credit += reclaimed - credited
        self.state.connection.reset_reclaimed += credited
        stream.state = "closed"
        stream.reset_error_code = int(event["error_code"])
        stream.terminal_reason = "rst_stream"
        self._drain_scheduler()

    def _ack_connection_credit(self, amount: int) -> int:
        remaining = min(amount, self.state.connection.unacked_send)
        acknowledged = remaining
        ordered = sorted(
            self.state.streams.values(),
            key=lambda stream: (stream.first_unacked_seq, stream.stream_id),
        )
        for stream in ordered:
            if remaining <= 0:
                break
            if stream.unacked_conn <= 0:
                continue
            consumed = min(stream.unacked_conn, remaining)
            stream.unacked_conn -= consumed
            remaining -= consumed
            if stream.unacked_conn == 0:
                stream.first_unacked_seq = 0
        self.state.connection.unacked_send -= acknowledged - remaining
        return acknowledged

    def _apply_window_update(self, event: dict[str, Any]) -> list[dict[str, int]]:
        level = event["level"]
        increment = int(event["increment"])
        direction = event["direction"]
        connection = self.state.connection

        if level == "connection":
            if direction == "inbound":
                target = connection.send_window
                if target + increment > connection.max_window:
                    self._fail_event(
                        event,
                        E_WINDOW_OVERFLOW,
                        "connection send window would overflow",
                        current=target,
                        increment=increment,
                        max_window=connection.max_window,
                    )
                connection.send_window += increment
                connection.send_window_updates += increment
                self._ack_connection_credit(increment)
            else:
                target = connection.recv_window
                if target + increment > connection.max_window:
                    self._fail_event(
                        event,
                        E_WINDOW_OVERFLOW,
                        "connection receive window would overflow",
                        current=target,
                        increment=increment,
                        max_window=connection.max_window,
                    )
                connection.recv_window += increment
                connection.recv_window_updates += increment
            return self._drain_scheduler()

        stream = self._stream(event["stream_id"])
        if stream.state in {"idle", "closed", "error"}:
            self._fail_event(
                event,
                E_STREAM_STATE,
                "stream WINDOW_UPDATE is invalid for the current stream state",
                state=stream.state,
            )
        if direction == "inbound":
            if stream.send_window + increment > connection.max_window:
                self._fail_event(
                    event,
                    E_WINDOW_OVERFLOW,
                    "stream send window would overflow",
                    current=stream.send_window,
                    increment=increment,
                    max_window=connection.max_window,
                )
            stream.send_window += increment
            stream.send_window_updates += increment
            stream.unacked_stream = max(0, stream.unacked_stream - increment)
        else:
            if stream.recv_window + increment > connection.max_window:
                self._fail_event(
                    event,
                    E_WINDOW_OVERFLOW,
                    "stream receive window would overflow",
                    current=stream.recv_window,
                    increment=increment,
                    max_window=connection.max_window,
                )
            stream.recv_window += increment
            stream.recv_window_updates += increment
        return self._drain_scheduler()

    def _apply_settings(self, event: dict[str, Any]) -> None:
        ack = bool(event.get("ack", False))
        direction = event["direction"]
        connection = self.state.connection
        if ack:
            counter = (
                "settings_ack_out" if direction == "outbound" else "settings_ack_in"
            )
            if getattr(connection, counter) <= 0:
                self._fail_event(
                    event,
                    E_SETTINGS_STATE,
                    "SETTINGS ACK has no matching outstanding SETTINGS",
                    counter=counter,
                )
            setattr(connection, counter, getattr(connection, counter) - 1)
            return

        settings = {
            str(key).upper(): int(value)
            for key, value in event.get("settings", {}).items()
        }
        connection.settings.update(settings)
        if "MAX_CONCURRENT_STREAMS" in settings:
            connection.max_concurrent_streams = max(
                1, settings["MAX_CONCURRENT_STREAMS"]
            )
        if direction == "inbound":
            connection.settings_ack_out += 1
        else:
            connection.settings_ack_in += 1

    def _apply_ping(self, event: dict[str, Any]) -> None:
        payload = str(event.get("payload_hex", ""))
        ack = bool(event.get("ack", False))
        connection = self.state.connection
        if not ack:
            if event["direction"] == "inbound":
                connection.pending_pings_out.append(payload)
            else:
                connection.pending_pings_in.append(payload)
            return

        pending = (
            connection.pending_pings_in
            if event["direction"] == "inbound"
            else connection.pending_pings_out
        )
        if not pending or pending[0] != payload:
            self._fail_event(
                event,
                E_PING_STATE,
                "PING ACK does not match the oldest outstanding PING",
                expected=(pending[0] if pending else None),
                actual=payload,
            )
        pending.pop(0)

    def _abandon_stream(self, stream: StreamState, reason: str) -> None:
        reclaimed = stream.unacked_conn
        self.state.connection.unacked_send = max(
            0, self.state.connection.unacked_send - reclaimed
        )
        stream.unacked_conn = 0
        stream.unacked_stream = 0
        room = self.state.connection.max_window - self.state.connection.send_window
        credited = min(reclaimed, max(0, room))
        self.state.connection.send_window += credited
        self.state.connection.discarded_reset_credit += reclaimed - credited
        self.state.connection.reset_reclaimed += credited
        stream.pending_outbound = 0
        stream.pending_end_stream = False
        stream.state = "error"
        stream.terminal_reason = reason

    def _apply_goaway(self, event: dict[str, Any]) -> None:
        last_stream_id = int(event["last_stream_id"])
        goaway = {
            "debug_hex": str(event.get("debug_hex", "")),
            "direction": event["direction"],
            "error_code": int(event.get("error_code", 0)),
            "last_stream_id": last_stream_id,
            "seq": event["seq"],
        }
        existing = self.state.connection.goaway
        if existing is not None and existing["last_stream_id"] == last_stream_id:
            return
        self.state.connection.goaway = goaway
        for stream_id in sorted(self.state.streams):
            stream = self.state.streams[stream_id]
            if stream.stream_id > last_stream_id and stream.state not in TERMINAL_STATES:
                self._abandon_stream(stream, "goaway_not_processed")

    def apply_event(self, event: dict[str, Any]) -> FrameOutcome:
        if int(event["seq"]) != self.state.committed_seq + 1:
            raise EngineError(
                E_BAD_SCENARIO,
                "event sequence does not follow the committed watermark",
                {
                    "committed_seq": self.state.committed_seq,
                    "seq": event["seq"],
                },
            )
        if int(event["at_ms"]) < self.state.logical_clock_ms:
            raise EngineError(
                E_BAD_SCENARIO,
                "event logical clock regressed",
                {
                    "committed_seq": self.state.committed_seq,
                    "at_ms": event["at_ms"],
                    "watermark": self.state.logical_clock_ms,
                },
            )

        self._check_header_interlock(event)
        frame_type = event["type"]
        allocations: list[dict[str, int]] = []
        if frame_type in {"HEADERS", "CONTINUATION"}:
            stream = self._stream(event["stream_id"])
            self._start_or_continue_headers(event, stream)
        elif frame_type == "DATA":
            allocations = self._apply_data(event)
        elif frame_type == "RST_STREAM":
            self._apply_rst_stream(event)
        elif frame_type == "WINDOW_UPDATE":
            allocations = self._apply_window_update(event)
        elif frame_type == "SETTINGS":
            self._apply_settings(event)
        elif frame_type == "PING":
            self._apply_ping(event)
        elif frame_type == "GOAWAY":
            self._apply_goaway(event)
        else:
            raise EngineError(
                E_BAD_SCENARIO,
                f"unsupported frame type {frame_type}",
                {"seq": event["seq"], "type": frame_type},
            )

        self.state.logical_clock_ms = int(event["at_ms"])
        self.state.committed_seq = int(event["seq"])
        self._assert_invariants()
        event_digest = digest_value(event)
        return FrameOutcome(
            seq=int(event["seq"]),
            at_ms=int(event["at_ms"]),
            event_type=frame_type,
            allocations=allocations,
            state_digest=self.state.digest(),
            event_digest=event_digest,
            ledger=self.ledger(),
        )

    def _assert_invariants(self) -> None:
        connection = self.state.connection
        if connection.send_window < 0 or connection.recv_window < 0:
            raise EngineError(
                E_FLOW_CONTROL,
                "connection window became negative",
                {"seq": self.state.committed_seq},
            )
        if (
            connection.send_window > connection.max_window
            or connection.recv_window > connection.max_window
        ):
            raise EngineError(
                E_WINDOW_OVERFLOW,
                "connection window exceeded max_window",
                {"seq": self.state.committed_seq},
            )
        unacked_sum = 0
        sent_sum = 0
        received_sum = 0
        expected_connection_send = (
            connection.initial_send_window
            + connection.send_window_updates
            - connection.bytes_sent_total
            + connection.reset_reclaimed
        )
        expected_connection_recv = (
            connection.initial_recv_window
            + connection.recv_window_updates
            - connection.bytes_received_total
        )
        if connection.send_window != expected_connection_send:
            raise EngineError(
                E_FLOW_CONTROL,
                "connection send-window ledger does not reconcile",
                {
                    "actual": connection.send_window,
                    "expected": expected_connection_send,
                    "seq": self.state.committed_seq,
                },
            )
        if connection.recv_window != expected_connection_recv:
            raise EngineError(
                E_FLOW_CONTROL,
                "connection receive-window ledger does not reconcile",
                {
                    "actual": connection.recv_window,
                    "expected": expected_connection_recv,
                    "seq": self.state.committed_seq,
                },
            )
        for stream in self.state.streams.values():
            if stream.send_window < 0 or stream.recv_window < 0:
                raise EngineError(
                    E_FLOW_CONTROL,
                    "stream window became negative",
                    {"seq": self.state.committed_seq, "stream_id": stream.stream_id},
                )
            if (
                stream.send_window > connection.max_window
                or stream.recv_window > connection.max_window
            ):
                raise EngineError(
                    E_WINDOW_OVERFLOW,
                    "stream window exceeded max_window",
                    {"seq": self.state.committed_seq, "stream_id": stream.stream_id},
                )
            if stream.unacked_conn < 0 or stream.unacked_stream < 0:
                raise EngineError(
                    E_FLOW_CONTROL,
                    "stream unacknowledged byte counter became negative",
                    {"seq": self.state.committed_seq, "stream_id": stream.stream_id},
                )
            unacked_sum += stream.unacked_conn
            sent_sum += stream.bytes_sent
            received_sum += stream.bytes_received
            expected_stream_send = (
                stream.initial_send_window
                + stream.send_window_updates
                - stream.bytes_sent
            )
            expected_stream_recv = (
                stream.initial_recv_window
                + stream.recv_window_updates
                - stream.bytes_received
            )
            if stream.send_window != expected_stream_send:
                raise EngineError(
                    E_FLOW_CONTROL,
                    "stream send-window ledger does not reconcile",
                    {
                        "actual": stream.send_window,
                        "expected": expected_stream_send,
                        "seq": self.state.committed_seq,
                        "stream_id": stream.stream_id,
                    },
                )
            if stream.recv_window != expected_stream_recv:
                raise EngineError(
                    E_FLOW_CONTROL,
                    "stream receive-window ledger does not reconcile",
                    {
                        "actual": stream.recv_window,
                        "expected": expected_stream_recv,
                        "seq": self.state.committed_seq,
                        "stream_id": stream.stream_id,
                    },
                )
        if unacked_sum != connection.unacked_send:
            raise EngineError(
                E_FLOW_CONTROL,
                "connection and stream unacknowledged ledgers disagree",
                {
                    "connection_unacked_send": connection.unacked_send,
                    "stream_unacked_sum": unacked_sum,
                    "seq": self.state.committed_seq,
                },
            )
        if sent_sum != connection.bytes_sent_total:
            raise EngineError(
                E_FLOW_CONTROL,
                "connection and stream sent-byte ledgers disagree",
                {
                    "connection_bytes_sent_total": connection.bytes_sent_total,
                    "stream_bytes_sent_sum": sent_sum,
                    "seq": self.state.committed_seq,
                },
            )
        if received_sum != connection.bytes_received_total:
            raise EngineError(
                E_FLOW_CONTROL,
                "connection and stream received-byte ledgers disagree",
                {
                    "connection_bytes_received_total": (
                        connection.bytes_received_total
                    ),
                    "stream_bytes_received_sum": received_sum,
                    "seq": self.state.committed_seq,
                },
            )

    def ledger(self) -> dict[str, Any]:
        connection = self.state.connection
        streams: list[dict[str, Any]] = []
        for stream_id in sorted(self.state.streams):
            stream = self.state.streams[stream_id]
            sendable = (
                min(connection.send_window, stream.send_window)
                if stream.state in SENDABLE_STATES
                else 0
            )
            streams.append(
                {
                    "bytes_received": stream.bytes_received,
                    "bytes_sent": stream.bytes_sent,
                    "initial_recv_window": stream.initial_recv_window,
                    "initial_send_window": stream.initial_send_window,
                    "pending_outbound": stream.pending_outbound,
                    "recv_window": stream.recv_window,
                    "recv_window_updates": stream.recv_window_updates,
                    "send_window": stream.send_window,
                    "send_window_updates": stream.send_window_updates,
                    "sendable": sendable,
                    "state": stream.state,
                    "stream_id": stream.stream_id,
                    "unacked_conn": stream.unacked_conn,
                    "unacked_stream": stream.unacked_stream,
                }
            )
        return {
            "connection": {
                "bytes_received_total": connection.bytes_received_total,
                "bytes_sent_total": connection.bytes_sent_total,
                "initial_recv_window": connection.initial_recv_window,
                "initial_send_window": connection.initial_send_window,
                "recv_window": connection.recv_window,
                "recv_window_updates": connection.recv_window_updates,
                "reset_reclaimed": connection.reset_reclaimed,
                "send_window": connection.send_window,
                "send_window_updates": connection.send_window_updates,
                "sendable": connection.send_window,
                "unacked_send": connection.unacked_send,
            },
            "logical_clock_ms": self.state.logical_clock_ms,
            "seq": self.state.committed_seq,
            "streams": streams,
        }

    def trace_record(
        self,
        outcome: FrameOutcome,
        state_digest: str,
        trajectory_digest: str,
        ledger_digest: str,
    ) -> dict[str, Any]:
        return {
            "allocations": outcome.allocations,
            "at_ms": outcome.at_ms,
            "connection": outcome.ledger["connection"],
            "event_digest": outcome.event_digest,
            "ledger_digest": ledger_digest,
            "seq": outcome.seq,
            "state_digest": state_digest,
            "streams": outcome.ledger["streams"],
            "trajectory_digest": trajectory_digest,
            "type": outcome.event_type,
        }

    def finish(self) -> None:
        if self.state.header_assembly is not None:
            assembly = self.state.header_assembly
            raise EngineError(
                E_HEADERS_INCOMPLETE,
                "scenario ended before END_HEADERS completed a header block",
                {
                    "seq": self.state.committed_seq,
                    "started_seq": assembly.started_seq,
                    "stream_id": assembly.stream_id,
                },
            )

    def final_state_mismatches(self) -> list[dict[str, Any]]:
        mismatches: list[dict[str, Any]] = []
        for stream_id in sorted(self.state.streams):
            actual = self.state.streams[stream_id].state
            expected = self._declaration_by_id[stream_id].expected_final_state
            if actual != expected:
                mismatches.append(
                    {
                        "actual": actual,
                        "expected": expected,
                        "stream_id": stream_id,
                    }
                )
        return mismatches

    def status_streams(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        connection = self.state.connection
        for stream_id in sorted(self.state.streams):
            stream = self.state.streams[stream_id]
            declaration = self._declaration_by_id[stream_id]
            sendable = (
                min(connection.send_window, stream.send_window)
                if stream.state in SENDABLE_STATES
                else 0
            )
            result.append(
                {
                    "bytes_received": stream.bytes_received,
                    "bytes_sent": stream.bytes_sent,
                    "expected_final_state": declaration.expected_final_state,
                    "initial_recv_window": stream.initial_recv_window,
                    "initial_send_window": stream.initial_send_window,
                    "pending_outbound": stream.pending_outbound,
                    "recv_window": stream.recv_window,
                    "recv_window_updates": stream.recv_window_updates,
                    "reset_error_code": stream.reset_error_code,
                    "send_window": stream.send_window,
                    "send_window_updates": stream.send_window_updates,
                    "sendable": sendable,
                    "state": stream.state,
                    "stream_id": stream.stream_id,
                    "terminal_reason": stream.terminal_reason,
                    "unacked_conn": stream.unacked_conn,
                    "unacked_stream": stream.unacked_stream,
                    "weight": stream.weight,
                }
            )
        return result


def initial_ledger(state: EngineState) -> dict[str, Any]:
    connection = state.connection
    streams = []
    for stream_id in sorted(state.streams):
        stream = state.streams[stream_id]
        streams.append(
            {
                "bytes_received": stream.bytes_received,
                "bytes_sent": stream.bytes_sent,
                "initial_recv_window": stream.initial_recv_window,
                "initial_send_window": stream.initial_send_window,
                "pending_outbound": stream.pending_outbound,
                "recv_window": stream.recv_window,
                "recv_window_updates": stream.recv_window_updates,
                "send_window": stream.send_window,
                "send_window_updates": stream.send_window_updates,
                "sendable": 0,
                "state": stream.state,
                "stream_id": stream.stream_id,
                "unacked_conn": stream.unacked_conn,
                "unacked_stream": stream.unacked_stream,
            }
        )
    return {
        "connection": {
            "bytes_received_total": connection.bytes_received_total,
            "bytes_sent_total": connection.bytes_sent_total,
            "initial_recv_window": connection.initial_recv_window,
            "initial_send_window": connection.initial_send_window,
            "recv_window": connection.recv_window,
            "recv_window_updates": connection.recv_window_updates,
            "reset_reclaimed": connection.reset_reclaimed,
            "send_window": connection.send_window,
            "send_window_updates": connection.send_window_updates,
            "sendable": connection.send_window,
            "unacked_send": connection.unacked_send,
        },
        "logical_clock_ms": state.logical_clock_ms,
        "seq": state.committed_seq,
        "streams": streams,
    }

