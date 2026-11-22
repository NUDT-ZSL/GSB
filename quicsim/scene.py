from __future__ import annotations

import json
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

from .errors import E_BAD_SCENE, EngineError

PACKET_SPACES = ("initial", "handshake", "application")
EVENT_TYPES = {
    "write_stream",
    "send",
    "ack",
    "start_probe",
    "probe_timeout",
    "abandon_probe",
    "path_mtu",
    "max_data",
    "max_stream_data",
    "connection_close",
}
QUIC_MIN_UDP_PAYLOAD = 1200
QUIC_MAX_UDP_PAYLOAD = 65527


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _fail(message: str, event_seq: int | None = None, **details: Any) -> None:
    raise EngineError(E_BAD_SCENE, message, event_seq=event_seq, details=details)


def _as_obj(value: Any, name: str, seq: int | None = None) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(f"{name} must be an object", seq)
    return value


def _as_list(value: Any, name: str, seq: int | None = None) -> list[Any]:
    if not isinstance(value, list):
        _fail(f"{name} must be an array", seq)
    return value


def _as_int(
    value: Any,
    name: str,
    seq: int | None = None,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    if type(value) is not int:
        _fail(f"{name} must be an integer", seq, value=value)
    if minimum is not None and value < minimum:
        _fail(f"{name} must be >= {minimum}", seq, value=value)
    if maximum is not None and value > maximum:
        _fail(f"{name} must be <= {maximum}", seq, value=value)
    return value


def _as_str(value: Any, name: str, seq: int | None = None, choices: set[str] | None = None) -> str:
    if not isinstance(value, str) or not value:
        _fail(f"{name} must be a non-empty string", seq)
    if choices is not None and value not in choices:
        _fail(f"{name} must be one of {sorted(choices)}", seq, value=value)
    return value


def _as_bool(value: Any, name: str, seq: int | None = None) -> bool:
    if type(value) is not bool:
        _fail(f"{name} must be a boolean", seq)
    return value


@dataclass(frozen=True)
class ConnectionConfig:
    initial_max_data: int
    initial_max_streams: int
    initial_max_stream_data: int
    max_udp_payload_size: int
    packet_overhead_bytes: int
    ack_delay_factor_num: int
    ack_delay_factor_den: int


@dataclass(frozen=True)
class CongestionConfig:
    initial_cwnd_packets: int
    initial_ssthresh_packets: int
    min_cwnd_packets: int
    ca_alpha_num: int
    ca_alpha_den: int
    loss_reduction_num: int
    loss_reduction_den: int
    persistent_threshold_ms: int
    packet_threshold: int
    pto_initial_rtt_ms: int
    pto_min_rtt_ms: int
    pto_granularity_ms: int
    max_backoff: int


@dataclass(frozen=True)
class Scene:
    scene_id: str
    connection: ConnectionConfig
    congestion: CongestionConfig
    observations: dict[str, Any]
    events: list[dict[str, Any]]
    source: dict[str, Any]

    @property
    def event_count(self) -> int:
        return len(self.events)

    @property
    def scene_hash(self) -> str:
        from hashlib import sha256

        return sha256(canonical_json(self.source).encode("utf-8")).hexdigest()

    def to_source_dict(self) -> dict[str, Any]:
        return json.loads(canonical_json(self.source))


def _parse_fraction(value: Any, name: str) -> tuple[int, int]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(f"{name} must be a non-negative number")
    if value < 0:
        _fail(f"{name} must be non-negative")
    try:
        fraction = Fraction(str(value))
    except (ValueError, ZeroDivisionError):
        _fail(f"{name} must be finite")
    return fraction.numerator, fraction.denominator


def _parse_connection(raw: Any) -> ConnectionConfig:
    obj = _as_obj(raw, "connection")
    max_payload = _as_int(
        obj.get("max_udp_payload_size"),
        "connection.max_udp_payload_size",
        minimum=QUIC_MIN_UDP_PAYLOAD,
        maximum=QUIC_MAX_UDP_PAYLOAD,
    )
    overhead = _as_int(
        obj.get("packet_overhead_bytes", 100),
        "connection.packet_overhead_bytes",
        minimum=1,
        maximum=max_payload - 1,
    )
    factor_num, factor_den = _parse_fraction(
        obj.get("ack_delay_factor", 1), "connection.ack_delay_factor"
    )
    return ConnectionConfig(
        initial_max_data=_as_int(
            obj.get("initial_max_data"), "connection.initial_max_data", minimum=1
        ),
        initial_max_streams=_as_int(
            obj.get("initial_max_streams"), "connection.initial_max_streams", minimum=1
        ),
        initial_max_stream_data=_as_int(
            obj.get("initial_max_stream_data"),
            "connection.initial_max_stream_data",
            minimum=1,
        ),
        max_udp_payload_size=max_payload,
        packet_overhead_bytes=overhead,
        ack_delay_factor_num=factor_num,
        ack_delay_factor_den=factor_den,
    )


def _parse_congestion(raw: Any) -> CongestionConfig:
    obj = _as_obj(raw, "congestion")
    ca = _as_obj(obj.get("congestion_avoidance", {}), "congestion.congestion_avoidance")
    persistent = _as_obj(
        obj.get("persistent_congestion", {}), "congestion.persistent_congestion"
    )
    pto = _as_obj(obj.get("pto", {}), "congestion.pto")

    ca_alpha_num = _as_int(
        ca.get("alpha_num", 1), "congestion.congestion_avoidance.alpha_num", minimum=1
    )
    ca_alpha_den = _as_int(
        ca.get("alpha_den", 1), "congestion.congestion_avoidance.alpha_den", minimum=1
    )
    reduction_num = _as_int(
        persistent.get("loss_reduction_num", 1),
        "congestion.persistent_congestion.loss_reduction_num",
        minimum=1,
    )
    reduction_den = _as_int(
        persistent.get("loss_reduction_den", 2),
        "congestion.persistent_congestion.loss_reduction_den",
        minimum=1,
    )
    if reduction_num > reduction_den:
        _fail("loss reduction factor must be <= 1")

    initial_rtt = _as_int(
        pto.get("initial_rtt_ms", 100), "congestion.pto.initial_rtt_ms", minimum=1
    )
    min_rtt = _as_int(
        pto.get("min_rtt_ms", 1), "congestion.pto.min_rtt_ms", minimum=1
    )
    if min_rtt > initial_rtt:
        _fail("pto.min_rtt_ms must be <= pto.initial_rtt_ms")

    return CongestionConfig(
        initial_cwnd_packets=_as_int(
            obj.get("initial_cwnd_packets"),
            "congestion.initial_cwnd_packets",
            minimum=1,
        ),
        initial_ssthresh_packets=_as_int(
            obj.get("initial_ssthresh_packets"),
            "congestion.initial_ssthresh_packets",
            minimum=1,
        ),
        min_cwnd_packets=_as_int(
            obj.get("min_cwnd_packets", 2),
            "congestion.min_cwnd_packets",
            minimum=1,
        ),
        ca_alpha_num=ca_alpha_num,
        ca_alpha_den=ca_alpha_den,
        loss_reduction_num=reduction_num,
        loss_reduction_den=reduction_den,
        persistent_threshold_ms=_as_int(
            persistent.get("threshold_ms", 0),
            "congestion.persistent_congestion.threshold_ms",
            minimum=0,
        ),
        packet_threshold=_as_int(
            obj.get("packet_threshold", 3),
            "congestion.packet_threshold",
            minimum=1,
        ),
        pto_initial_rtt_ms=initial_rtt,
        pto_min_rtt_ms=min_rtt,
        pto_granularity_ms=_as_int(
            pto.get("granularity_ms", 1), "congestion.pto.granularity_ms", minimum=1
        ),
        max_backoff=_as_int(
            pto.get("max_backoff", 8), "congestion.pto.max_backoff", minimum=1
        ),
    )


def _normalize_ack_ranges(
    event: dict[str, Any], seq: int, fallback_space: str | None = None
) -> list[dict[str, int | str]]:
    ranges_raw = _as_list(event.get("ranges"), "ack.ranges", seq)
    if not ranges_raw:
        _fail("ack.ranges must not be empty", seq)
    normalized: list[dict[str, int | str]] = []
    for index, raw in enumerate(ranges_raw):
        item = _as_obj(raw, f"ack.ranges[{index}]", seq)
        space = item.get("space", fallback_space)
        if space is None:
            _fail(f"ack.ranges[{index}].space is required", seq)
        space = _as_str(
            space, f"ack.ranges[{index}].space", seq, choices=set(PACKET_SPACES)
        )
        smallest = _as_int(
            item.get("smallest"), f"ack.ranges[{index}].smallest", seq, minimum=0
        )
        largest = _as_int(
            item.get("largest"), f"ack.ranges[{index}].largest", seq, minimum=0
        )
        if smallest > largest:
            from .errors import E_ACK_RANGE

            raise EngineError(
                E_ACK_RANGE,
                "ACK range lower bound must be <= upper bound",
                event_seq=seq,
                details={"space": space, "smallest": smallest, "largest": largest},
            )
        normalized.append({"space": space, "smallest": smallest, "largest": largest})
    return normalized


def _normalize_packet_ref(event: dict[str, Any], seq: int) -> dict[str, int | str] | None:
    raw = event.get("packet")
    if raw is None:
        space = event.get("space")
        number = event.get("packet_number")
        if space is None and number is None:
            return None
        raw = {"space": space, "number": number}
    obj = _as_obj(raw, "packet", seq)
    space = _as_str(
        obj.get("space"), "packet.space", seq, choices=set(PACKET_SPACES)
    )
    number = _as_int(obj.get("number"), "packet.number", seq, minimum=0)
    return {"space": space, "number": number}


def _normalize_event(raw: Any, index: int, previous_time: int) -> dict[str, Any]:
    obj = _as_obj(raw, f"events[{index}]")
    seq = _as_int(obj.get("seq", index + 1), "event.seq", minimum=1)
    time_ms = _as_int(obj.get("time_ms"), "event.time_ms", seq, minimum=0)
    if time_ms < previous_time:
        _fail("event time_ms must be non-decreasing", seq)
    event_type = _as_str(obj.get("type"), "event.type", seq, choices=EVENT_TYPES)
    normalized: dict[str, Any] = {"seq": seq, "time_ms": time_ms, "type": event_type}

    if event_type == "write_stream":
        stream_id = _as_int(obj.get("stream_id"), "write_stream.stream_id", seq, minimum=0)
        offset = _as_int(obj.get("offset", 0), "write_stream.offset", seq, minimum=0)
        length = _as_int(obj.get("length"), "write_stream.length", seq, minimum=0)
        fin = _as_bool(obj.get("fin", False), "write_stream.fin", seq)
        if length == 0 and not fin:
            _fail("write_stream length must be positive unless fin is true", seq)
        normalized.update(
            {"stream_id": stream_id, "offset": offset, "length": length, "fin": fin}
        )
    elif event_type == "send":
        space = _as_str(
            obj.get("space", "application"),
            "send.space",
            seq,
            choices=set(PACKET_SPACES),
        )
        count = _as_int(obj.get("count", 1), "send.count", seq, minimum=1)
        normalized.update({"space": space, "count": count})
        if "ack_ranges" in obj:
            ack_ranges = _normalize_ack_ranges(
                {"ranges": obj["ack_ranges"]}, seq, fallback_space=space
            )
            normalized["ack_ranges"] = ack_ranges
    elif event_type == "ack":
        fallback = obj.get("space")
        if fallback is not None:
            fallback = _as_str(fallback, "ack.space", seq, choices=set(PACKET_SPACES))
        ranges = _normalize_ack_ranges(obj, seq, fallback_space=fallback)
        ack_delay_ms = _as_int(obj.get("ack_delay_ms", 0), "ack.ack_delay_ms", seq, minimum=0)
        received_at_ms = _as_int(
            obj.get("received_at_ms", time_ms), "ack.received_at_ms", seq, minimum=0
        )
        if received_at_ms > time_ms:
            _fail("ack.received_at_ms must not exceed event time_ms", seq)
        normalized.update(
            {
                "ranges": ranges,
                "ack_delay_ms": ack_delay_ms,
                "received_at_ms": received_at_ms,
            }
        )
    elif event_type in {"start_probe", "probe_timeout"}:
        packet = _normalize_packet_ref(obj, seq)
        if packet is not None:
            normalized["packet"] = packet
    elif event_type == "abandon_probe":
        reason = obj.get("reason")
        if reason is not None:
            normalized["reason"] = _as_str(reason, "abandon_probe.reason", seq)
    elif event_type == "path_mtu":
        normalized["mtu"] = _as_int(
            obj.get("mtu"),
            "path_mtu.mtu",
            seq,
            minimum=QUIC_MIN_UDP_PAYLOAD,
            maximum=QUIC_MAX_UDP_PAYLOAD,
        )
    elif event_type == "max_data":
        normalized["max_data"] = _as_int(
            obj.get("max_data"), "max_data.max_data", seq, minimum=0
        )
    elif event_type == "max_stream_data":
        normalized["stream_id"] = _as_int(
            obj.get("stream_id"), "max_stream_data.stream_id", seq, minimum=0
        )
        normalized["max_stream_data"] = _as_int(
            obj.get("max_stream_data"),
            "max_stream_data.max_stream_data",
            seq,
            minimum=0,
        )
    elif event_type == "connection_close":
        normalized["error_code"] = _as_int(
            obj.get("error_code", 0), "connection_close.error_code", seq, minimum=0
        )
        if "reason" in obj:
            normalized["reason"] = _as_str(
                obj["reason"], "connection_close.reason", seq
            )
    return normalized


def parse_scene(raw: Any) -> Scene:
    obj = _as_obj(raw, "scene")
    scene_id = _as_str(obj.get("scene_id"), "scene_id")
    connection = _parse_connection(obj.get("connection"))
    congestion = _parse_congestion(obj.get("congestion"))
    observations = _as_obj(obj.get("observations", {}), "observations")
    events_raw = _as_list(obj.get("events"), "events")
    if not events_raw:
        _fail("events must not be empty")

    events: list[dict[str, Any]] = []
    previous_time = 0
    previous_seq = 0
    for index, raw_event in enumerate(events_raw):
        event = _normalize_event(raw_event, index, previous_time)
        if event["seq"] != index + 1:
            _fail(
                "event seq values must be contiguous starting at 1",
                event["seq"],
                expected_seq=index + 1,
            )
        if event["seq"] <= previous_seq:
            _fail(
                "event seq values must be strictly increasing",
                event["seq"],
                previous_seq=previous_seq,
            )
        previous_time = event["time_ms"]
        previous_seq = event["seq"]
        events.append(event)

    return Scene(
        scene_id=scene_id,
        connection=connection,
        congestion=congestion,
        observations=json.loads(canonical_json(observations)),
        events=events,
        source=json.loads(canonical_json(obj)),
    )


def load_scene(path: str | Path) -> Scene:
    scene_path = Path(path)
    try:
        raw = json.loads(scene_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise EngineError(E_BAD_SCENE, f"scene file not found: {scene_path}") from None
    except json.JSONDecodeError as exc:
        raise EngineError(
            E_BAD_SCENE,
            f"invalid JSON in scene file: {exc.msg}",
            details={"line": exc.lineno, "column": exc.colno},
        ) from None
    return parse_scene(raw)


def scene_from_json(text: str) -> Scene:
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise EngineError(
            E_BAD_SCENE,
            f"invalid stored scene JSON: {exc.msg}",
            details={"line": exc.lineno, "column": exc.colno},
        ) from None
    return parse_scene(raw)
