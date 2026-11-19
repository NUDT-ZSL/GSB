from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import (
    E_BAD_CONGESTION,
    E_BAD_JSON,
    E_BAD_LINK,
    E_BAD_OBSERVATION,
    E_BAD_SCRIPT,
    EngineError,
)
from .fixed import ROUNDING_MODES


TCP_STATES = (
    "LISTEN",
    "SYN-SENT",
    "SYN-RECEIVED",
    "ESTABLISHED",
    "FIN-WAIT-1",
    "FIN-WAIT-2",
    "CLOSE-WAIT",
    "LAST-ACK",
    "TIME-WAIT",
    "CLOSED",
)

INITIAL_STATES = frozenset(
    {"LISTEN", "SYN-SENT", "SYN-RECEIVED", "ESTABLISHED", "CLOSED"}
)

EVENT_TYPES = frozenset(
    {
        "listen",
        "connect",
        "syn_received",
        "establish",
        "app_write",
        "app_read",
        "ack",
        "dup_ack",
        "timeout",
        "advertised_window",
        "peer_data",
        "app_close",
        "peer_fin",
        "fin_ack",
        "time_wait_expire",
        "reset",
    }
)


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def digest_json(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _object(value: Any, code: str, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise EngineError(code, f"{path} must be a JSON object", {"path": path})
    return value


def _int(
    value: Any,
    code: str,
    path: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise EngineError(code, f"{path} must be an integer", {"path": path})
    if minimum is not None and value < minimum:
        raise EngineError(
            code,
            f"{path} must be >= {minimum}",
            {"path": path, "value": value, "minimum": minimum},
        )
    if maximum is not None and value > maximum:
        raise EngineError(
            code,
            f"{path} must be <= {maximum}",
            {"path": path, "value": value, "maximum": maximum},
        )
    return value


def _bool(value: Any, code: str, path: str) -> bool:
    if not isinstance(value, bool):
        raise EngineError(code, f"{path} must be a boolean", {"path": path})
    return value


def _list(value: Any, code: str, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise EngineError(code, f"{path} must be a JSON array", {"path": path})
    return value


def _string(value: Any, code: str, path: str) -> str:
    if not isinstance(value, str) or not value:
        raise EngineError(code, f"{path} must be a non-empty string", {"path": path})
    return value


@dataclass(frozen=True)
class LinkConfig:
    rtt_ms: int
    bandwidth_bps: int
    receiver_window: int
    window_scaling: bool
    window_scale: int


@dataclass(frozen=True)
class CongestionConfig:
    mss: int
    initial_cwnd: int
    initial_ssthresh: int
    fast_retransmit: bool
    fast_recovery: bool
    ca_algorithm: str
    ca_increment_rounding: str
    initial_rto_ms: int
    max_rto_ms: int
    rto_backoff: int
    max_retries: int
    dup_ack_threshold: int
    timeout_cwnd: int
    fixed_point_scale: int
    clock_tolerance_ms: int
    rto_reset_on_ack: bool
    retry_scope: str


@dataclass(frozen=True)
class ObservationConfig:
    sample_every_n_events: int
    sample_times_ms: tuple[int, ...]
    sample_event_types: tuple[str, ...]
    include_all_events: bool
    round_decimals: int


@dataclass(frozen=True)
class SceneEvent:
    index: int
    event_id: str
    ts_ms: int
    kind: str
    payload: dict[str, Any]

    def canonical(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "event_id": self.event_id,
            "ts_ms": self.ts_ms,
            "type": self.kind,
            "payload": self.payload,
        }

    @property
    def digest(self) -> str:
        return digest_json(self.canonical())


@dataclass(frozen=True)
class Scene:
    name: str
    link: LinkConfig
    congestion: CongestionConfig
    observations: ObservationConfig
    initial_state: str
    events: tuple[SceneEvent, ...]
    source_digest: str
    source_objects: dict[str, Any]

    def event_at(self, index: int) -> SceneEvent:
        return self.events[index]


def _load_json(path: str, label: str) -> Any:
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise EngineError(
            E_BAD_JSON,
            f"cannot read {label} file: {exc}",
            {"path": path, "label": label},
        ) from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise EngineError(
            E_BAD_JSON,
            f"invalid JSON in {label} file: {exc.msg}",
            {"path": path, "label": label, "line": exc.lineno, "column": exc.colno},
        ) from exc


def _parse_link(raw: Any) -> LinkConfig:
    obj = _object(raw, E_BAD_LINK, "link")
    rtt_ms = _int(obj.get("rtt_ms"), E_BAD_LINK, "link.rtt_ms", minimum=1)
    bandwidth_bps = _int(
        obj.get("bandwidth_bps"), E_BAD_LINK, "link.bandwidth_bps", minimum=1
    )
    receiver_window = _int(
        obj.get("receiver_window"),
        E_BAD_LINK,
        "link.receiver_window",
        minimum=1,
    )
    window_scaling = _bool(
        obj.get("window_scaling", False), E_BAD_LINK, "link.window_scaling"
    )
    window_scale = _int(
        obj.get("window_scale", 0),
        E_BAD_LINK,
        "link.window_scale",
        minimum=0,
        maximum=14,
    )
    if not window_scaling and window_scale != 0:
        raise EngineError(
            E_BAD_LINK,
            "link.window_scale must be 0 when window_scaling is disabled",
            {"window_scale": window_scale},
        )
    return LinkConfig(
        rtt_ms=rtt_ms,
        bandwidth_bps=bandwidth_bps,
        receiver_window=receiver_window,
        window_scaling=window_scaling,
        window_scale=window_scale,
    )


def _parse_congestion(raw: Any) -> CongestionConfig:
    obj = _object(raw, E_BAD_CONGESTION, "congestion")
    mss = _int(obj.get("mss"), E_BAD_CONGESTION, "congestion.mss", minimum=1)
    initial_cwnd = _int(
        obj.get("initial_cwnd"),
        E_BAD_CONGESTION,
        "congestion.initial_cwnd",
        minimum=1,
    )
    initial_ssthresh = _int(
        obj.get("initial_ssthresh"),
        E_BAD_CONGESTION,
        "congestion.initial_ssthresh",
        minimum=1,
    )
    fast_retransmit = _bool(
        obj.get("fast_retransmit", True),
        E_BAD_CONGESTION,
        "congestion.fast_retransmit",
    )
    fast_recovery = _bool(
        obj.get("fast_recovery", True),
        E_BAD_CONGESTION,
        "congestion.fast_recovery",
    )
    ca_algorithm = obj.get("ca_algorithm", "newreno")
    if ca_algorithm not in {"reno", "newreno"}:
        raise EngineError(
            E_BAD_CONGESTION,
            "congestion.ca_algorithm must be 'reno' or 'newreno'",
            {"value": ca_algorithm},
        )
    rounding = obj.get("ca_increment_rounding", "floor")
    if rounding not in ROUNDING_MODES:
        raise EngineError(
            E_BAD_CONGESTION,
            "congestion.ca_increment_rounding must be floor, ceil, or nearest",
            {"value": rounding},
        )
    initial_rto_ms = _int(
        obj.get("initial_rto_ms"),
        E_BAD_CONGESTION,
        "congestion.initial_rto_ms",
        minimum=1,
    )
    max_rto_ms = _int(
        obj.get("max_rto_ms", initial_rto_ms),
        E_BAD_CONGESTION,
        "congestion.max_rto_ms",
        minimum=1,
    )
    if max_rto_ms < initial_rto_ms:
        raise EngineError(
            E_BAD_CONGESTION,
            "congestion.max_rto_ms must be >= initial_rto_ms",
            {"initial_rto_ms": initial_rto_ms, "max_rto_ms": max_rto_ms},
        )
    rto_backoff = _int(
        obj.get("rto_backoff", 2),
        E_BAD_CONGESTION,
        "congestion.rto_backoff",
        minimum=1,
    )
    max_retries = _int(
        obj.get("max_retries", 3),
        E_BAD_CONGESTION,
        "congestion.max_retries",
        minimum=0,
    )
    dup_ack_threshold = _int(
        obj.get("dup_ack_threshold", 3),
        E_BAD_CONGESTION,
        "congestion.dup_ack_threshold",
        minimum=1,
    )
    timeout_cwnd = _int(
        obj.get("timeout_cwnd", mss),
        E_BAD_CONGESTION,
        "congestion.timeout_cwnd",
        minimum=1,
    )
    fixed_point_scale = _int(
        obj.get("fixed_point_scale", 1024),
        E_BAD_CONGESTION,
        "congestion.fixed_point_scale",
        minimum=1,
        maximum=65536,
    )
    if fixed_point_scale & (fixed_point_scale - 1):
        raise EngineError(
            E_BAD_CONGESTION,
            "congestion.fixed_point_scale must be a power of two",
            {"value": fixed_point_scale},
        )
    clock_tolerance_ms = _int(
        obj.get("clock_tolerance_ms", 0),
        E_BAD_CONGESTION,
        "congestion.clock_tolerance_ms",
        minimum=0,
    )
    rto_reset_on_ack = _bool(
        obj.get("rto_reset_on_ack", True),
        E_BAD_CONGESTION,
        "congestion.rto_reset_on_ack",
    )
    retry_scope = obj.get("retry_scope", "consecutive")
    if retry_scope not in {"consecutive", "connection"}:
        raise EngineError(
            E_BAD_CONGESTION,
            "congestion.retry_scope must be 'consecutive' or 'connection'",
            {"value": retry_scope},
        )
    return CongestionConfig(
        mss=mss,
        initial_cwnd=initial_cwnd,
        initial_ssthresh=initial_ssthresh,
        fast_retransmit=fast_retransmit,
        fast_recovery=fast_recovery,
        ca_algorithm=ca_algorithm,
        ca_increment_rounding=rounding,
        initial_rto_ms=initial_rto_ms,
        max_rto_ms=max_rto_ms,
        rto_backoff=rto_backoff,
        max_retries=max_retries,
        dup_ack_threshold=dup_ack_threshold,
        timeout_cwnd=timeout_cwnd,
        fixed_point_scale=fixed_point_scale,
        clock_tolerance_ms=clock_tolerance_ms,
        rto_reset_on_ack=rto_reset_on_ack,
        retry_scope=retry_scope,
    )


def _parse_observations(raw: Any) -> ObservationConfig:
    obj = _object(raw, E_BAD_OBSERVATION, "observations")
    sample_every = _int(
        obj.get("sample_every_n_events", 1),
        E_BAD_OBSERVATION,
        "observations.sample_every_n_events",
        minimum=1,
    )
    times_raw = _list(
        obj.get("sample_times_ms", []),
        E_BAD_OBSERVATION,
        "observations.sample_times_ms",
    )
    times = tuple(
        _int(
            item,
            E_BAD_OBSERVATION,
            f"observations.sample_times_ms[{index}]",
            minimum=0,
        )
        for index, item in enumerate(times_raw)
    )
    types_raw = _list(
        obj.get("sample_event_types", []),
        E_BAD_OBSERVATION,
        "observations.sample_event_types",
    )
    event_types: list[str] = []
    for index, item in enumerate(types_raw):
        kind = _string(
            item,
            E_BAD_OBSERVATION,
            f"observations.sample_event_types[{index}]",
        )
        if kind not in EVENT_TYPES:
            raise EngineError(
                E_BAD_OBSERVATION,
                f"unknown sample event type: {kind}",
                {"type": kind},
            )
        event_types.append(kind)
    include_all = _bool(
        obj.get("include_all_events", True),
        E_BAD_OBSERVATION,
        "observations.include_all_events",
    )
    round_decimals = _int(
        obj.get("round_decimals", 6),
        E_BAD_OBSERVATION,
        "observations.round_decimals",
        minimum=0,
        maximum=12,
    )
    return ObservationConfig(
        sample_every_n_events=sample_every,
        sample_times_ms=times,
        sample_event_types=tuple(event_types),
        include_all_events=include_all,
        round_decimals=round_decimals,
    )


def _event_payload(kind: str, raw: dict[str, Any], index: int) -> dict[str, Any]:
    path = f"script.events[{index}]"
    if kind == "app_write":
        return {
            "bytes": _int(
                raw.get("bytes"), E_BAD_SCRIPT, f"{path}.bytes", minimum=1
            )
        }
    if kind == "app_read":
        return {
            "bytes": _int(
                raw.get("bytes"), E_BAD_SCRIPT, f"{path}.bytes", minimum=1
            )
        }
    if kind == "ack":
        payload: dict[str, Any] = {
            "ack_no": _int(
                raw.get("ack_no"),
                E_BAD_SCRIPT,
                f"{path}.ack_no",
                minimum=0,
            )
        }
        if "window" in raw:
            payload["window"] = _int(
                raw["window"], E_BAD_SCRIPT, f"{path}.window", minimum=0
            )
        if "description" in raw:
            payload["description"] = _string(
                raw["description"], E_BAD_SCRIPT, f"{path}.description"
            )
        return payload
    if kind == "dup_ack":
        payload = {
            "count": _int(
                raw.get("count", 1),
                E_BAD_SCRIPT,
                f"{path}.count",
                minimum=1,
            )
        }
        if "ack_no" in raw:
            payload["ack_no"] = _int(
                raw["ack_no"],
                E_BAD_SCRIPT,
                f"{path}.ack_no",
                minimum=0,
            )
        return payload
    if kind == "timeout":
        payload = {}
        if "seq" in raw:
            payload["seq"] = _int(
                raw["seq"], E_BAD_SCRIPT, f"{path}.seq", minimum=0
            )
        if "range" in raw:
            value = raw["range"]
            if isinstance(value, list) and len(value) == 2:
                start = _int(
                    value[0], E_BAD_SCRIPT, f"{path}.range[0]", minimum=0
                )
                end = _int(value[1], E_BAD_SCRIPT, f"{path}.range[1]", minimum=0)
            elif isinstance(value, dict):
                start = _int(
                    value.get("start"),
                    E_BAD_SCRIPT,
                    f"{path}.range.start",
                    minimum=0,
                )
                end = _int(
                    value.get("end"),
                    E_BAD_SCRIPT,
                    f"{path}.range.end",
                    minimum=0,
                )
            else:
                raise EngineError(
                    E_BAD_SCRIPT,
                    f"{path}.range must be [start, end] or an object",
                    {"path": f"{path}.range"},
                )
            if end <= start:
                raise EngineError(
                    E_BAD_SCRIPT,
                    f"{path}.range end must be greater than start",
                    {"range": [start, end]},
                )
            payload["range"] = [start, end]
        if "description" in raw:
            payload["description"] = _string(
                raw["description"], E_BAD_SCRIPT, f"{path}.description"
            )
        return payload
    if kind == "advertised_window":
        return {
            "window": _int(
                raw.get("window"),
                E_BAD_SCRIPT,
                f"{path}.window",
                minimum=0,
            )
        }
    if kind == "peer_data":
        payload = {
            "bytes": _int(
                raw.get("bytes"), E_BAD_SCRIPT, f"{path}.bytes", minimum=1
            )
        }
        if "seq" in raw:
            payload["seq"] = _int(
                raw["seq"], E_BAD_SCRIPT, f"{path}.seq", minimum=0
            )
        return payload
    return {}


def _parse_script(
    raw: Any,
) -> tuple[str, tuple[SceneEvent, ...]]:
    obj = _object(raw, E_BAD_SCRIPT, "script")
    initial_state = obj.get("initial_state", "LISTEN")
    if initial_state not in INITIAL_STATES:
        raise EngineError(
            E_BAD_SCRIPT,
            "script.initial_state is not a supported initial TCP state",
            {"value": initial_state, "supported": sorted(INITIAL_STATES)},
        )
    events_raw = _list(obj.get("events"), E_BAD_SCRIPT, "script.events")
    events: list[SceneEvent] = []
    event_ids: set[str] = set()
    previous_ts = -1
    total_written = 0
    for index, item in enumerate(events_raw):
        event_obj = _object(item, E_BAD_SCRIPT, f"script.events[{index}]")
        ts_ms = _int(
            event_obj.get("ts_ms"),
            E_BAD_SCRIPT,
            f"script.events[{index}].ts_ms",
            minimum=0,
        )
        if ts_ms < previous_ts:
            raise EngineError(
                E_BAD_SCRIPT,
                "script events must be ordered by non-decreasing ts_ms",
                {
                    "index": index,
                    "previous_ts_ms": previous_ts,
                    "ts_ms": ts_ms,
                },
            )
        previous_ts = ts_ms
        kind = _string(
            event_obj.get("type"),
            E_BAD_SCRIPT,
            f"script.events[{index}].type",
        )
        if kind not in EVENT_TYPES:
            raise EngineError(
                E_BAD_SCRIPT,
                f"unknown script event type: {kind}",
                {"index": index, "type": kind},
            )
        event_id = event_obj.get("event_id", f"evt-{index:04d}")
        event_id = _string(
            event_id, E_BAD_SCRIPT, f"script.events[{index}].event_id"
        )
        if event_id in event_ids:
            raise EngineError(
                E_BAD_SCRIPT,
                f"duplicate event_id: {event_id}",
                {"index": index, "event_id": event_id},
            )
        event_ids.add(event_id)
        payload = _event_payload(kind, event_obj, index)
        if kind == "timeout":
            references: list[tuple[int, int]] = []
            if "seq" in payload:
                references.append((payload["seq"], payload["seq"] + 1))
            if "range" in payload:
                references.append(tuple(payload["range"]))
            for start, end in references:
                if start >= total_written or end > total_written:
                    raise EngineError(
                        E_BAD_SCRIPT,
                        "timeout references bytes that have not been written before the event",
                        {
                            "index": index,
                            "written_before_event": total_written,
                            "range": [start, end],
                        },
                    )
        if kind == "app_write":
            total_written += payload["bytes"]
        events.append(
            SceneEvent(
                index=index,
                event_id=event_id,
                ts_ms=ts_ms,
                kind=kind,
                payload=payload,
            )
        )
    return initial_state, tuple(events)


def load_scene(
    *,
    link_path: str | None = None,
    script_path: str | None = None,
    congestion_path: str | None = None,
    observations_path: str | None = None,
    scene_path: str | None = None,
    name: str | None = None,
) -> Scene:
    """Load and fully validate a four-part or combined scene.

    The source digest is computed over the canonical JSON input objects, including
    event normalization, so replay can reject a changed truth source.
    """

    if scene_path:
        combined = _load_json(scene_path, "scene")
        root = _object(combined, E_BAD_JSON, "scene")
        raw_link = root.get("link")
        raw_script = root.get("script")
        raw_congestion = root.get("congestion")
        raw_observations = root.get("observations")
        default_name = root.get("name", Path(scene_path).stem)
    else:
        missing = [
            label
            for label, path in (
                ("link", link_path),
                ("script", script_path),
                ("congestion", congestion_path),
                ("observations", observations_path),
            )
            if not path
        ]
        if missing:
            raise EngineError(
                E_BAD_JSON,
                "provide --scene or all four scene part paths",
                {"missing": missing},
            )
        raw_link = _load_json(link_path, "link")
        raw_script = _load_json(script_path, "script")
        raw_congestion = _load_json(congestion_path, "congestion")
        raw_observations = _load_json(observations_path, "observations")
        default_name = name or Path(script_path).stem

    link = _parse_link(raw_link)
    congestion = _parse_congestion(raw_congestion)
    observations = _parse_observations(raw_observations)
    initial_state, events = _parse_script(raw_script)
    source_objects = {
        "format_version": 1,
        "link": raw_link,
        "script": raw_script,
        "congestion": raw_congestion,
        "observations": raw_observations,
    }
    source_digest = digest_json(source_objects)
    return Scene(
        name=name or default_name,
        link=link,
        congestion=congestion,
        observations=observations,
        initial_state=initial_state,
        events=events,
        source_digest=source_digest,
        source_objects=source_objects,
    )

