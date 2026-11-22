from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any, Iterable

from .scene import PACKET_SPACES, canonical_json

SCALE = 1_000_000


def hash_json(value: Any) -> str:
    return sha256(canonical_json(value).encode("utf-8")).hexdigest()


def merge_ranges(ranges: Iterable[tuple[int, int] | list[int]]) -> list[list[int]]:
    items = sorted((int(start), int(end)) for start, end in ranges if int(start) < int(end))
    if not items:
        return []
    merged: list[list[int]] = [[items[0][0], items[0][1]]]
    for start, end in items[1:]:
        if start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1][1] = end
        else:
            merged.append([start, end])
    return merged


def subtract_ranges(
    base: Iterable[tuple[int, int] | list[int]],
    cuts: Iterable[tuple[int, int] | list[int]],
) -> list[list[int]]:
    remaining = [list(item) for item in merge_ranges(base)]
    cut_list = merge_ranges(cuts)
    for cut_start, cut_end in cut_list:
        next_remaining: list[list[int]] = []
        for start, end in remaining:
            if cut_end <= start or cut_start >= end:
                next_remaining.append([start, end])
                continue
            if start < cut_start:
                next_remaining.append([start, cut_start])
            if cut_end < end:
                next_remaining.append([cut_end, end])
        remaining = next_remaining
    return remaining


def intersect_ranges(
    left: Iterable[tuple[int, int] | list[int]],
    right: Iterable[tuple[int, int] | list[int]],
) -> list[list[int]]:
    a = merge_ranges(left)
    b = merge_ranges(right)
    result: list[list[int]] = []
    i = 0
    j = 0
    while i < len(a) and j < len(b):
        start = max(a[i][0], b[j][0])
        end = min(a[i][1], b[j][1])
        if start < end:
            result.append([start, end])
        if a[i][1] <= b[j][1]:
            i += 1
        else:
            j += 1
    return result


def range_length(ranges: Iterable[tuple[int, int] | list[int]]) -> int:
    return sum(end - start for start, end in merge_ranges(ranges))


def merge_inclusive_ranges(
    ranges: Iterable[tuple[int, int] | list[int]],
) -> list[list[int]]:
    half_open = merge_ranges(
        (int(start), int(end) + 1)
        for start, end in ranges
        if int(start) <= int(end)
    )
    return [[start, end - 1] for start, end in half_open]


@dataclass
class Packet:
    space: str
    number: int
    status: str
    sent_at_ms: int
    sent_event_seq: int
    frames: list[dict[str, Any]] = field(default_factory=list)
    acked_at_ms: int | None = None
    lost_at_ms: int | None = None
    loss_source: str | None = None
    ack_event_seq: int | None = None
    lost_event_seq: int | None = None
    transitions: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "space": self.space,
            "number": self.number,
            "status": self.status,
            "sent_at_ms": self.sent_at_ms,
            "sent_event_seq": self.sent_event_seq,
            "acked_at_ms": self.acked_at_ms,
            "acked_event_seq": self.ack_event_seq,
            "lost_at_ms": self.lost_at_ms,
            "lost_event_seq": self.lost_event_seq,
            "loss_source": self.loss_source,
            "frames": self.frames,
            "transitions": self.transitions,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Packet":
        return cls(
            space=raw["space"],
            number=raw["number"],
            status=raw["status"],
            sent_at_ms=raw["sent_at_ms"],
            sent_event_seq=raw["sent_event_seq"],
            frames=list(raw.get("frames", [])),
            acked_at_ms=raw.get("acked_at_ms"),
            lost_at_ms=raw.get("lost_at_ms"),
            loss_source=raw.get("loss_source"),
            ack_event_seq=raw.get("acked_event_seq"),
            lost_event_seq=raw.get("lost_event_seq"),
            transitions=list(raw.get("transitions", [])),
        )


@dataclass
class PacketSpace:
    name: str
    next_pn: int = 0
    largest_acked: int = -1
    acknowledged_ranges: list[list[int]] = field(default_factory=list)
    packets: dict[int, Packet] = field(default_factory=dict)

    @property
    def max_sent_pn(self) -> int:
        return self.next_pn - 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "next_pn": self.next_pn,
            "max_sent_pn": self.max_sent_pn,
            "largest_acked": self.largest_acked,
            "acknowledged_ranges": self.acknowledged_ranges,
            "packets": [self.packets[pn].to_dict() for pn in sorted(self.packets)],
        }

    @classmethod
    def from_dict(cls, name: str, raw: dict[str, Any]) -> "PacketSpace":
        space = cls(
            name=name,
            next_pn=raw["next_pn"],
            largest_acked=raw["largest_acked"],
            acknowledged_ranges=[list(item) for item in raw.get("acknowledged_ranges", [])],
        )
        for packet_raw in raw.get("packets", []):
            packet = Packet.from_dict(packet_raw)
            space.packets[packet.number] = packet
        return space


@dataclass
class StreamState:
    stream_id: int
    max_stream_data: int
    write_ranges: list[list[int]] = field(default_factory=list)
    sent_ranges: list[list[int]] = field(default_factory=list)
    retransmit_ranges: list[list[int]] = field(default_factory=list)
    highest_sent_offset: int = 0
    fin: bool = False
    fin_offset: int | None = None

    def add_write(self, offset: int, length: int, fin: bool) -> None:
        if length:
            self.write_ranges = merge_ranges(
                [*self.write_ranges, (offset, offset + length)]
            )
        if fin:
            self.fin = True
            self.fin_offset = offset + length if self.fin_offset is None else self.fin_offset

    def unsent_ranges(self) -> list[list[int]]:
        return subtract_ranges(self.write_ranges, self.sent_ranges)

    def candidate_send_ranges(self) -> list[list[int]]:
        return merge_ranges([*self.unsent_ranges(), *self.retransmit_ranges])

    def mark_sent(self, start: int, end: int) -> None:
        self.sent_ranges = merge_ranges([*self.sent_ranges, (start, end)])
        self.retransmit_ranges = subtract_ranges(self.retransmit_ranges, [(start, end)])
        self.highest_sent_offset = max(self.highest_sent_offset, end)

    def mark_retransmit_needed(self, start: int, end: int) -> None:
        unsent_overlap = intersect_ranges(self.write_ranges, [(start, end)])
        if unsent_overlap:
            self.retransmit_ranges = merge_ranges(
                [*self.retransmit_ranges, *unsent_overlap]
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "stream_id": self.stream_id,
            "max_stream_data": self.max_stream_data,
            "write_ranges": self.write_ranges,
            "sent_ranges": self.sent_ranges,
            "retransmit_ranges": self.retransmit_ranges,
            "highest_sent_offset": self.highest_sent_offset,
            "fin": self.fin,
            "fin_offset": self.fin_offset,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "StreamState":
        return cls(
            stream_id=raw["stream_id"],
            max_stream_data=raw["max_stream_data"],
            write_ranges=[list(item) for item in raw.get("write_ranges", [])],
            sent_ranges=[list(item) for item in raw.get("sent_ranges", [])],
            retransmit_ranges=[list(item) for item in raw.get("retransmit_ranges", [])],
            highest_sent_offset=raw.get("highest_sent_offset", 0),
            fin=raw.get("fin", False),
            fin_offset=raw.get("fin_offset"),
        )


@dataclass
class CongestionState:
    cwnd_fixed: int
    ssthresh_fixed: int
    carry_fixed: int = 0
    next_entry_seq: int = 1
    rounding_entries: list[dict[str, Any]] = field(default_factory=list)
    congestion_events: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "cwnd_fixed": self.cwnd_fixed,
            "ssthresh_fixed": self.ssthresh_fixed,
            "carry_fixed": self.carry_fixed,
            "next_entry_seq": self.next_entry_seq,
            "rounding_entries": self.rounding_entries,
            "congestion_events": self.congestion_events,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "CongestionState":
        return cls(
            cwnd_fixed=raw["cwnd_fixed"],
            ssthresh_fixed=raw["ssthresh_fixed"],
            carry_fixed=raw.get("carry_fixed", 0),
            next_entry_seq=raw.get("next_entry_seq", 1),
            rounding_entries=list(raw.get("rounding_entries", [])),
            congestion_events=list(raw.get("congestion_events", [])),
        )


@dataclass
class ProbeState:
    status: str = "none"
    backoff: int = 1
    deadline_ms: int | None = None
    started_at_ms: int | None = None
    target: dict[str, Any] | None = None
    last_outcome: str | None = None
    outcome_event_seq: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "backoff": self.backoff,
            "deadline_ms": self.deadline_ms,
            "started_at_ms": self.started_at_ms,
            "target": self.target,
            "last_outcome": self.last_outcome,
            "outcome_event_seq": self.outcome_event_seq,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ProbeState":
        return cls(
            status=raw.get("status", "none"),
            backoff=raw.get("backoff", 1),
            deadline_ms=raw.get("deadline_ms"),
            started_at_ms=raw.get("started_at_ms"),
            target=raw.get("target"),
            last_outcome=raw.get("last_outcome"),
            outcome_event_seq=raw.get("outcome_event_seq"),
        )


@dataclass
class ConnectionState:
    closed: bool = False
    close_error_code: int | None = None
    close_reason: str | None = None
    failure_code: str | None = None
    failure_detail: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "closed": self.closed,
            "close_error_code": self.close_error_code,
            "close_reason": self.close_reason,
            "failure_code": self.failure_code,
            "failure_detail": self.failure_detail,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ConnectionState":
        return cls(
            closed=raw.get("closed", False),
            close_error_code=raw.get("close_error_code"),
            close_reason=raw.get("close_reason"),
            failure_code=raw.get("failure_code"),
            failure_detail=raw.get("failure_detail"),
        )


@dataclass
class EngineState:
    spaces: dict[str, PacketSpace]
    streams: dict[int, StreamState]
    max_data: int
    path_mtu: int
    congestion: CongestionState
    probe: ProbeState
    connection: ConnectionState
    srtt_ms: int
    rttvar_ms: int
    latest_rtt_ms: int | None
    latest_ack_delay_ms: int
    events_processed: int = 0
    time_ms: int = 0
    persistent_signatures: list[str] = field(default_factory=list)
    counters: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "quicsim-state-v1",
            "watermark_event": self.events_processed,
            "logical_time_ms": self.time_ms,
            "inflight_digest": self.inflight_digest(),
            "packet_digest": self.packet_digest(),
            "spaces": {name: self.spaces[name].to_dict() for name in PACKET_SPACES},
            "streams": [self.streams[sid].to_dict() for sid in sorted(self.streams)],
            "max_data": self.max_data,
            "path_mtu": self.path_mtu,
            "congestion": self.congestion.to_dict(),
            "probe": self.probe.to_dict(),
            "connection": self.connection.to_dict(),
            "srtt_ms": self.srtt_ms,
            "rttvar_ms": self.rttvar_ms,
            "latest_rtt_ms": self.latest_rtt_ms,
            "latest_ack_delay_ms": self.latest_ack_delay_ms,
            "events_processed": self.events_processed,
            "time_ms": self.time_ms,
            "persistent_signatures": self.persistent_signatures,
            "counters": self.counters,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "EngineState":
        spaces = {
            name: PacketSpace.from_dict(name, raw["spaces"][name])
            for name in PACKET_SPACES
        }
        stream_map = {
            stream_raw["stream_id"]: StreamState.from_dict(stream_raw)
            for stream_raw in raw.get("streams", [])
        }
        return cls(
            spaces=spaces,
            streams=stream_map,
            max_data=raw["max_data"],
            path_mtu=raw["path_mtu"],
            congestion=CongestionState.from_dict(raw["congestion"]),
            probe=ProbeState.from_dict(raw.get("probe", {})),
            connection=ConnectionState.from_dict(raw.get("connection", {})),
            srtt_ms=raw["srtt_ms"],
            rttvar_ms=raw["rttvar_ms"],
            latest_rtt_ms=raw.get("latest_rtt_ms"),
            latest_ack_delay_ms=raw.get("latest_ack_delay_ms", 0),
            events_processed=raw.get("events_processed", 0),
            time_ms=raw.get("time_ms", 0),
            persistent_signatures=list(raw.get("persistent_signatures", [])),
            counters=dict(raw.get("counters", {})),
        )

    def inflight_packets(self) -> list[Packet]:
        packets: list[Packet] = []
        for name in PACKET_SPACES:
            packets.extend(
                packet
                for packet in self.spaces[name].packets.values()
                if packet.status == "in_flight"
            )
        return packets

    def inflight_count(self) -> int:
        return sum(
            1
            for space in self.spaces.values()
            for packet in space.packets.values()
            if packet.status == "in_flight"
        )

    def inflight_digest(self) -> str:
        summary = [
            {
                "space": packet.space,
                "number": packet.number,
                "sent_at_ms": packet.sent_at_ms,
                "frames": packet.frames,
            }
            for packet in sorted(
                self.inflight_packets(), key=lambda item: (item.space, item.number)
            )
        ]
        return hash_json(summary)

    def connection_used_bytes(self) -> int:
        return sum(stream.highest_sent_offset for stream in self.streams.values())

    def state_hash(self) -> str:
        return hash_json(self.to_dict())

    def packet_digest(self) -> str:
        return hash_json(
            [
                packet.to_dict()
                for name in PACKET_SPACES
                for packet in (
                    self.spaces[name].packets[pn]
                    for pn in sorted(self.spaces[name].packets)
                )
            ]
        )


def new_state(
    *,
    max_data: int,
    path_mtu: int,
    cwnd_packets: int,
    ssthresh_packets: int,
    srtt_ms: int,
    rttvar_ms: int,
) -> EngineState:
    return EngineState(
        spaces={name: PacketSpace(name) for name in PACKET_SPACES},
        streams={},
        max_data=max_data,
        path_mtu=path_mtu,
        congestion=CongestionState(
            cwnd_fixed=cwnd_packets * SCALE,
            ssthresh_fixed=ssthresh_packets * SCALE,
        ),
        probe=ProbeState(),
        connection=ConnectionState(),
        srtt_ms=srtt_ms,
        rttvar_ms=rttvar_ms,
        latest_rtt_ms=None,
        latest_ack_delay_ms=0,
    )
