from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import AlertLabError, invalid
from .rules import SEVERITIES
from .topology import Topology
from .util import (
    canonical_json,
    read_json,
    require_int,
    require_number,
    sha256_text,
)


def _fail(code: str, message: str, **details: Any) -> AlertLabError:
    return invalid(code, message, details=details or None)


@dataclass(frozen=True)
class EventSeed:
    event_uid: str
    stream_id: str
    seq: int
    instance_id: str
    rule_name: str
    severity: str
    event_ts_ms: int
    metric_value: int | float
    labels: dict[str, str]
    content_hash: str

    def to_record(self) -> dict[str, Any]:
        return {
            "event_uid": self.event_uid,
            "stream_id": self.stream_id,
            "seq": self.seq,
            "instance_id": self.instance_id,
            "rule_name": self.rule_name,
            "severity": self.severity,
            "event_ts_ms": self.event_ts_ms,
            "metric_value": self.metric_value,
            "labels": self.labels,
            "content_hash": self.content_hash,
        }


@dataclass(frozen=True)
class EventStream:
    stream_id: str
    window_ms: int
    allowed_lateness_ms: int
    injected_clock_ms: int
    events: tuple[EventSeed, ...]
    document: dict[str, Any]
    fingerprint: str


def event_stream_from_document(document: Any, topology: Topology) -> EventStream:
    if not isinstance(document, dict):
        raise _fail("E_EVENT_SCHEMA", "event stream document must be an object")
    stream_id = document.get("stream_id")
    window_ms = document.get("window_ms")
    allowed_lateness_ms = document.get("allowed_lateness_ms")
    injected_clock_ms = document.get("injected_clock_ms")
    raw_events = document.get("events")
    if not isinstance(stream_id, str) or not stream_id:
        raise _fail("E_EVENT_SCHEMA", "stream_id must be a non-empty string")
    if not require_int(window_ms, minimum=1):
        raise _fail("E_EVENT_SCHEMA", "window_ms must be a positive integer")
    if not require_int(allowed_lateness_ms, minimum=0):
        raise _fail(
            "E_EVENT_SCHEMA", "allowed_lateness_ms must be a non-negative integer"
        )
    if not require_int(injected_clock_ms, minimum=0):
        raise _fail("E_EVENT_SCHEMA", "injected_clock_ms must be a non-negative integer")
    if not isinstance(raw_events, list):
        raise _fail("E_EVENT_SCHEMA", "events must be a list")

    seeds: list[EventSeed] = []
    seen_seq: set[int] = set()
    for raw in raw_events:
        if not isinstance(raw, dict):
            raise _fail("E_EVENT_SCHEMA", "each event must be an object")
        seq = raw.get("seq")
        instance_id = raw.get("instance_id")
        rule_name = raw.get("rule_name")
        severity = raw.get("severity")
        timestamp = raw.get("logical_clock_ms")
        metric = raw.get("metric_value")
        labels = raw.get("labels", {})
        if not require_int(seq, minimum=0):
            raise _fail("E_EVENT_SEQ", "seq must be a non-negative integer")
        if seq in seen_seq:
            raise _fail("E_EVENT_SEQ", "event seq must be unique inside a stream", seq=seq)
        seen_seq.add(seq)
        if not isinstance(instance_id, str) or instance_id not in topology.instance_to_service:
            raise _fail(
                "E_EVENT_INSTANCE_UNKNOWN",
                "event instance does not exist in topology",
                seq=seq,
                instance_id=instance_id,
            )
        if not isinstance(rule_name, str) or not rule_name:
            raise _fail("E_EVENT_RULE", "rule_name must be a non-empty string", seq=seq)
        if severity not in SEVERITIES:
            raise _fail(
                "E_EVENT_SEVERITY",
                "event severity is not a legal enum value",
                seq=seq,
                allowed=list(SEVERITIES),
            )
        if not require_int(timestamp, minimum=0):
            raise _fail(
                "E_EVENT_TIMESTAMP",
                "logical_clock_ms must be a non-negative integer",
                seq=seq,
            )
        if not require_number(metric):
            raise _fail(
                "E_EVENT_METRIC", "metric_value must be a finite number", seq=seq
            )
        if not isinstance(labels, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in labels.items()
        ):
            raise _fail(
                "E_EVENT_LABELS",
                "labels must be an object of string values",
                seq=seq,
            )
        normalized = {
            "stream_id": stream_id,
            "seq": seq,
            "instance_id": instance_id,
            "rule_name": rule_name,
            "severity": severity,
            "logical_clock_ms": timestamp,
            "metric_value": metric,
            "labels": dict(sorted(labels.items())),
        }
        content_hash = sha256_text(canonical_json(normalized))
        event_uid = sha256_text(f"{stream_id}|{seq}")
        seeds.append(
            EventSeed(
                event_uid=event_uid,
                stream_id=stream_id,
                seq=seq,
                instance_id=instance_id,
                rule_name=rule_name,
                severity=severity,
                event_ts_ms=timestamp,
                metric_value=metric,
                labels=dict(sorted(labels.items())),
                content_hash=content_hash,
            )
        )
    seeds.sort(key=lambda event: event.seq)
    return EventStream(
        stream_id=stream_id,
        window_ms=window_ms,
        allowed_lateness_ms=allowed_lateness_ms,
        injected_clock_ms=injected_clock_ms,
        events=tuple(seeds),
        document=document,
        fingerprint=sha256_text(
            canonical_json(
                {
                    "stream_id": stream_id,
                    "window_ms": window_ms,
                    "allowed_lateness_ms": allowed_lateness_ms,
                    "injected_clock_ms": injected_clock_ms,
                    "events": [seed.to_record() for seed in seeds],
                }
            )
        ),
    )


def load_event_stream(path: str | Path, topology: Topology) -> EventStream:
    try:
        document = read_json(path)
    except FileNotFoundError:
        raise _fail("E_EVENT_FILE", "event stream file does not exist", path=str(path))
    except (OSError, ValueError) as exc:
        raise _fail("E_EVENT_JSON", "event stream is not valid JSON", error=str(exc))
    return event_stream_from_document(document, topology)
