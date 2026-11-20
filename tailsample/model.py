from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .errors import BusinessError, fail
from .util import (
    canonical_json,
    json_type_name,
    require_dict,
    require_int,
    require_list,
    require_number,
    require_str,
    sha256_json,
)


@dataclass(frozen=True, slots=True)
class ServiceTopology:
    version: str
    services: tuple[str, ...]
    digest: str
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Span:
    trace_id: str
    span_id: str
    parent_span_id: str | None
    service: str
    operation: str
    start_ms: int
    end_ms: int
    status_code: int
    is_error: bool
    is_root_end: bool
    attributes: dict[str, int | float]
    payload: dict[str, Any]

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms

    def as_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "service": self.service,
            "operation": self.operation,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "duration_ms": self.duration_ms,
            "status_code": self.status_code,
            "is_error": self.is_error,
            "attributes": dict(sorted(self.attributes.items())),
        }

    def canonical_dict(self) -> dict[str, Any]:
        return self.as_dict()

    def digest(self) -> str:
        return sha256_json(self.canonical_dict())


@dataclass(frozen=True, slots=True)
class SpanEvent:
    event_id: str
    arrival_ms: int
    span: Span
    payload: dict[str, Any]

    def digest(self) -> str:
        return sha256_json(self.payload)


def parse_topology(data: Any) -> ServiceTopology:
    root = require_dict(data, "topology", "E_BAD_TOPOLOGY")
    version = root.get("version", "topology-v1")
    require_str(version, "topology.version", "E_BAD_TOPOLOGY")
    services_raw = require_list(root.get("services"), "topology.services", "E_BAD_TOPOLOGY")
    if not services_raw:
        fail("E_BAD_TOPOLOGY", "topology must declare at least one service")
    names: list[str] = []
    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(services_raw):
        service = require_dict(raw, f"topology.services[{index}]", "E_BAD_TOPOLOGY")
        name = require_str(
            service.get("name"), f"topology.services[{index}].name", "E_BAD_TOPOLOGY"
        )
        if name in seen:
            fail(
                "E_BAD_TOPOLOGY",
                "service names must be unique",
                service=name,
                index=index,
            )
        seen.add(name)
        names.append(name)
        normalized.append(dict(service))
    payload = dict(root)
    payload["version"] = version
    payload["services"] = sorted(normalized, key=lambda item: item["name"])
    return ServiceTopology(
        version=version,
        services=tuple(sorted(names)),
        digest=sha256_json(payload),
        payload=payload,
    )


def parse_span(
    data: Any,
    known_services: set[str] | frozenset[str] | None = None,
) -> Span:
    raw = require_dict(data, "span", "E_BAD_SPAN")
    trace_id = require_str(raw.get("trace_id"), "span.trace_id", "E_BAD_SPAN")
    span_id = require_str(raw.get("span_id"), "span.span_id", "E_BAD_SPAN")
    parent_value = raw.get("parent_span_id")
    if parent_value is not None:
        parent_span_id = require_str(parent_value, "span.parent_span_id", "E_BAD_SPAN")
    else:
        parent_span_id = None
    service = require_str(raw.get("service"), "span.service", "E_BAD_SPAN")
    if known_services is not None and service not in known_services:
        fail(
            "E_UNKNOWN_SERVICE",
            "span references a service that is not declared in topology",
            trace_id=trace_id,
            span_id=span_id,
            service=service,
        )
    operation = require_str(raw.get("operation"), "span.operation", "E_BAD_SPAN")
    start_ms = require_int(raw.get("start_ms"), "span.start_ms", "E_BAD_SPAN")
    end_ms = require_int(raw.get("end_ms"), "span.end_ms", "E_BAD_SPAN")
    if start_ms < 0 or end_ms < 0:
        fail(
            "E_BAD_SPAN",
            "span timestamps must be non-negative",
            trace_id=trace_id,
            span_id=span_id,
        )
    if end_ms < start_ms:
        fail(
            "E_TIME_INVERSION",
            "span end must not precede span start",
            trace_id=trace_id,
            span_id=span_id,
            start_ms=start_ms,
            end_ms=end_ms,
        )
    status_code = require_int(raw.get("status_code"), "span.status_code", "E_BAD_SPAN")
    is_error = raw.get("is_error")
    if not isinstance(is_error, bool):
        fail(
            "E_BAD_SPAN",
            "span.is_error must be a boolean",
            trace_id=trace_id,
            span_id=span_id,
            value=is_error,
        )
    attributes_raw = require_dict(
        raw.get("attributes", {}), "span.attributes", "E_BAD_SPAN"
    )
    attributes: dict[str, int | float] = {}
    for key, value in attributes_raw.items():
        require_str(key, "span.attributes key", "E_BAD_SPAN")
        attributes[key] = require_number(
            value, f"span.attributes[{key}]", "E_BAD_SPAN"
        )
    root_marker = raw.get("root_end", parent_span_id is None)
    if not isinstance(root_marker, bool):
        fail(
            "E_BAD_SPAN",
            "span.root_end must be a boolean",
            trace_id=trace_id,
            span_id=span_id,
        )
    payload = {
        "trace_id": trace_id,
        "span_id": span_id,
        "parent_span_id": parent_span_id,
        "service": service,
        "operation": operation,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "status_code": status_code,
        "is_error": is_error,
        "attributes": dict(sorted(attributes.items())),
        "root_end": root_marker,
    }
    return Span(
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=parent_span_id,
        service=service,
        operation=operation,
        start_ms=start_ms,
        end_ms=end_ms,
        status_code=status_code,
        is_error=is_error,
        is_root_end=root_marker,
        attributes=attributes,
        payload=payload,
    )


def parse_event(data: Any, known_services: set[str] | frozenset[str]) -> SpanEvent:
    if not isinstance(data, dict):
        fail("E_BAD_SPAN", "event must be an object")
    if "span" in data:
        arrival_ms = require_int(data.get("arrival_ms"), "event.arrival_ms", "E_BAD_SPAN")
        span_raw = data.get("span")
    else:
        arrival_ms = require_int(data.get("arrival_ms"), "event.arrival_ms", "E_BAD_SPAN")
        span_raw = data
    if arrival_ms < 0:
        fail("E_BAD_SPAN", "event.arrival_ms must be non-negative", arrival_ms=arrival_ms)
    span = parse_span(span_raw, known_services)
    event_id_raw = data.get("event_id")
    if event_id_raw is None:
        event_id = f"evt:{span.trace_id}:{span.span_id}:{arrival_ms}"
    else:
        event_id = require_str(event_id_raw, "event.event_id", "E_BAD_SPAN")
    payload = {
        "event_id": event_id,
        "arrival_ms": arrival_ms,
        "span": span.payload,
    }
    return SpanEvent(
        event_id=event_id,
        arrival_ms=arrival_ms,
        span=span,
        payload=payload,
    )


def parse_event_bundle(
    data: Any,
    known_services: set[str] | frozenset[str],
) -> list[SpanEvent]:
    if isinstance(data, list):
        raw_events = data
    else:
        root = require_dict(data, "event bundle", "E_BAD_SPAN")
        raw_events = require_list(root.get("events"), "event bundle.events", "E_BAD_SPAN")
    events: list[SpanEvent] = []
    event_ids: set[str] = set()
    identities: dict[tuple[str, str], str] = {}
    for index, raw in enumerate(raw_events):
        try:
            event = parse_event(raw, known_services)
        except BusinessError as exc:
            exc.details.setdefault("event_index", index)
            raise
        if event.event_id in event_ids:
            fail(
                "E_BAD_SPAN",
                "event_id must be unique in a bundle",
                event_id=event.event_id,
                event_index=index,
            )
        event_ids.add(event.event_id)
        identity = (event.span.trace_id, event.span.span_id)
        digest = event.span.digest()
        if identity in identities:
            if identities[identity] != digest:
                fail(
                    "E_BAD_SPAN",
                    "the same span id has conflicting content",
                    trace_id=identity[0],
                    span_id=identity[1],
                    event_index=index,
                )
            fail(
                "E_BAD_SPAN",
                "duplicate span in event bundle",
                trace_id=identity[0],
                span_id=identity[1],
                event_index=index,
            )
        identities[identity] = digest
        events.append(event)
    validate_trace_graphs([event.span for event in events], require_complete_when_root=True)
    return sorted(
        events,
        key=lambda event: (
            event.arrival_ms,
            event.event_id,
            event.span.trace_id,
            event.span.span_id,
        ),
    )


def group_spans(spans: Iterable[Span]) -> dict[str, list[Span]]:
    grouped: dict[str, list[Span]] = {}
    for span in spans:
        grouped.setdefault(span.trace_id, []).append(span)
    for values in grouped.values():
        values.sort(key=lambda span: (span.start_ms, span.end_ms, span.span_id))
    return grouped


def validate_attribute_types(spans: Iterable[Span]) -> None:
    registry: dict[str, str] = {}
    for span in spans:
        for key, value in span.attributes.items():
            kind = json_type_name(value)
            previous = registry.get(key)
            if previous is not None and previous != kind:
                fail(
                    "E_ATTRIBUTE_TYPE_CONFLICT",
                    "an attribute key has conflicting value types within a trace",
                    trace_id=span.trace_id,
                    key=key,
                    previous_type=previous,
                    current_type=kind,
                    span_id=span.span_id,
                )
            registry[key] = kind


def validate_trace_graphs(
    spans: Iterable[Span],
    *,
    require_complete_when_root: bool,
) -> None:
    grouped = group_spans(spans)
    for trace_id, trace_spans in grouped.items():
        by_id: dict[str, Span] = {}
        for span in trace_spans:
            if span.span_id in by_id:
                fail(
                    "E_BAD_SPAN",
                    "span_id must be unique within a trace",
                    trace_id=trace_id,
                    span_id=span.span_id,
                )
            by_id[span.span_id] = span
        roots = [span for span in trace_spans if span.is_root_end]
        if len(roots) > 1:
            fail(
                "E_BAD_TOPOLOGY",
                "a trace must not have multiple root-end spans",
                trace_id=trace_id,
                root_span_ids=sorted(span.span_id for span in roots),
            )
        should_require_complete = bool(roots) if require_complete_when_root else False
        for span in trace_spans:
            if span.parent_span_id is None:
                continue
            if span.parent_span_id == span.span_id:
                fail(
                    "E_BAD_TOPOLOGY",
                    "a span cannot be its own parent",
                    trace_id=trace_id,
                    span_id=span.span_id,
                )
            if should_require_complete and span.parent_span_id not in by_id:
                fail(
                    "E_MISSING_PARENT",
                    "parent span is missing from the trace",
                    trace_id=trace_id,
                    span_id=span.span_id,
                    parent_span_id=span.parent_span_id,
                )
        state: dict[str, int] = {}

        def visit(span_id: str, chain: list[str]) -> None:
            marker = state.get(span_id, 0)
            if marker == 1:
                cycle = chain[chain.index(span_id) :] + [span_id]
                fail(
                    "E_BAD_TOPOLOGY",
                    "parent links form a cycle",
                    trace_id=trace_id,
                    cycle=cycle,
                )
            if marker == 2:
                return
            state[span_id] = 1
            span = by_id[span_id]
            if span.parent_span_id and span.parent_span_id in by_id:
                visit(span.parent_span_id, chain + [span_id])
            state[span_id] = 2

        for span_id in sorted(by_id):
            visit(span_id, [])
        validate_attribute_types(trace_spans)


def canonical_chain(spans: Iterable[Span]) -> list[dict[str, Any]]:
    ordered = sorted(
        spans,
        key=lambda span: (
            span.start_ms,
            span.end_ms,
            span.span_id,
            span.trace_id,
        ),
    )
    return [span.canonical_dict() for span in ordered]


def chain_digest(spans: Iterable[Span]) -> str:
    return sha256_json(canonical_chain(spans))


def chain_summary(spans: Iterable[Span]) -> dict[str, Any]:
    ordered = sorted(
        spans,
        key=lambda span: (span.start_ms, span.end_ms, span.span_id, span.trace_id),
    )
    if not ordered:
        return {
            "span_count": 0,
            "start_ms": None,
            "end_ms": None,
            "duration_ms": None,
            "digest": sha256_json([]),
        }
    start_ms = min(span.start_ms for span in ordered)
    end_ms = max(span.end_ms for span in ordered)
    return {
        "span_count": len(ordered),
        "start_ms": start_ms,
        "end_ms": end_ms,
        "duration_ms": end_ms - start_ms,
        "digest": sha256_json(canonical_chain(ordered)),
    }


def span_from_row(row: Any) -> Span:
    attributes = row["attributes"]
    return Span(
        trace_id=row["trace_id"],
        span_id=row["span_id"],
        parent_span_id=row["parent_span_id"],
        service=row["service"],
        operation=row["operation"],
        start_ms=row["start_ms"],
        end_ms=row["end_ms"],
        status_code=row["status_code"],
        is_error=bool(row["is_error"]),
        is_root_end=bool(row["is_root_end"]),
        attributes={key: value for key, value in attributes.items()},
        payload={},
    )


def canonical_event_payload(event: SpanEvent) -> str:
    return canonical_json(event.payload)

