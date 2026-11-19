from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .canonical import canonical_json, digest, normalize_digest
from .errors import (
    E_EVENT_CLOCK_INVALID,
    E_EVENT_INVALID,
    E_EVENT_SEQUENCE_DUPLICATE,
    E_EVENT_SEVERITY_INVALID,
    E_EVENT_UNKNOWN_INSTANCE,
    E_PATCH_CONFLICT,
    E_PATCH_DIGEST_MISMATCH,
    E_PATCH_INVALID,
    E_PATCH_UNKNOWN_DEPENDENCY,
    E_PATCH_UNKNOWN_INSTANCE,
    E_PATCH_UNKNOWN_SERVICE,
    E_PATCH_UNKNOWN_SILENCE,
    E_PATCH_VERSION_SEQUENCE,
    E_SAMPLE_PARSE,
    WorkbenchError,
)
from .topology import (
    SilenceWindow,
    Topology,
    parse_silence_window,
    parse_topology_payload,
    replace_service_upstreams,
)


SEVERITY_RANK = {"info": 1, "warning": 2, "critical": 3}
SEVERITIES = frozenset(SEVERITY_RANK)


@dataclass(frozen=True)
class EventSequence:
    raw: int | str
    text: str
    kind: int
    number: int | None
    text_sort: str

    @property
    def sort_key(self) -> tuple[int, int, int, str]:
        return (self.kind, self.number if self.number is not None else 0, 0, self.text_sort)


@dataclass(frozen=True)
class Event:
    event_seq: EventSequence
    instance_id: str
    rule_name: str
    severity: str
    occurred_clock_ms: int
    arrived_clock_ms: int
    metric_value: float
    silenced_maintenance: bool
    bucket_id: str
    payload_json: str

    @property
    def sort_key(self) -> tuple[int, int, int, int, str]:
        return (self.arrived_clock_ms, *self.event_seq.sort_key)

    def to_dict(self) -> dict[str, Any]:
        return {
            "arrived_clock_ms": self.arrived_clock_ms,
            "bucket_id": self.bucket_id,
            "event_seq": self.event_seq.raw,
            "instance_id": self.instance_id,
            "metric_value": self.metric_value,
            "occurred_clock_ms": self.occurred_clock_ms,
            "rule_name": self.rule_name,
            "severity": self.severity,
            "silenced_maintenance": self.silenced_maintenance,
        }


@dataclass(frozen=True)
class PatchPlan:
    parent_version: int
    target_version: int
    effective_clock_ms: int
    content_digest: str
    dependency_add: tuple[tuple[str, str], ...]
    dependency_remove: tuple[tuple[str, str], ...]
    weight_changes: tuple[tuple[str, float], ...]
    silence_add: tuple[SilenceWindow, ...]
    silence_remove: tuple[str, ...]
    payload_json: str
    new_topology: Topology


@dataclass(frozen=True)
class PatchInspection:
    parent_version: int
    target_version: int
    effective_clock_ms: int
    content_digest: str
    normalized_changes: dict[str, list[Any]]
    payload: dict[str, Any]


def load_json_file(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise WorkbenchError(
            E_SAMPLE_PARSE,
            "unable to read JSON sample",
            {"path": str(path), "reason": str(exc)},
        ) from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise WorkbenchError(
            E_SAMPLE_PARSE,
            "invalid JSON sample",
            {
                "column": exc.colno,
                "line": exc.lineno,
                "path": str(path),
                "reason": exc.msg,
            },
        ) from exc


def parse_topology_file(path: Path) -> Topology:
    return parse_topology_payload(load_json_file(path))


def _sequence(value: Any, details: dict[str, Any]) -> EventSequence:
    if isinstance(value, bool):
        raise WorkbenchError(E_EVENT_INVALID, "event_seq must be an integer or string", details)
    if isinstance(value, int):
        if value < 0:
            raise WorkbenchError(E_EVENT_INVALID, "event_seq must be non-negative", details)
        return EventSequence(raw=value, text=str(value), kind=0, number=value, text_sort="")
    if isinstance(value, str) and value.strip():
        return EventSequence(raw=value, text=value, kind=1, number=None, text_sort=value)
    raise WorkbenchError(E_EVENT_INVALID, "event_seq must be an integer or non-empty string", details)


def _nonnegative_int(value: Any, field: str, details: dict[str, Any]) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise WorkbenchError(
            E_EVENT_CLOCK_INVALID,
            f"{field} must be a non-negative integer",
            {**details, "field": field},
        )
    return value


def _event_buckets(payload: Any) -> list[tuple[str, list[Any]]]:
    if isinstance(payload, list):
        return [("default", payload)]
    if not isinstance(payload, dict):
        raise WorkbenchError(E_EVENT_INVALID, "event sample must be an object or list")
    if isinstance(payload.get("buckets"), list):
        buckets: list[tuple[str, list[Any]]] = []
        batch_ids: set[str] = set()
        for index, bucket in enumerate(payload["buckets"]):
            if not isinstance(bucket, dict):
                raise WorkbenchError(E_EVENT_INVALID, "bucket must be an object", {"index": index})
            bucket_id = str(bucket.get("bucket_id", f"bucket-{index}"))
            if bucket_id in batch_ids:
                raise WorkbenchError(
                    E_EVENT_INVALID,
                    "bucket_id values must be unique",
                    {"bucket_id": bucket_id},
                )
            batch_ids.add(bucket_id)
            events = bucket.get("events", [])
            if not isinstance(events, list):
                raise WorkbenchError(
                    E_EVENT_INVALID,
                    "bucket events must be a list",
                    {"bucket_id": bucket_id},
                )
            buckets.append((bucket_id, events))
        return buckets
    if isinstance(payload.get("events"), list):
        return [(str(payload.get("bucket_id", "default")), payload["events"])]
    raise WorkbenchError(E_EVENT_INVALID, "event sample must contain buckets or events")


def parse_events_payload(payload: Any, topology: Topology) -> list[Event]:
    events: list[Event] = []
    seen: dict[str, EventSequence] = {}
    for bucket_id, items in _event_buckets(payload):
        for index, item in enumerate(items):
            details = {"bucket_id": bucket_id, "index": index}
            if not isinstance(item, dict):
                raise WorkbenchError(E_EVENT_INVALID, "event must be an object", details)
            seq = _sequence(item.get("event_seq"), details)
            if seq.text in seen:
                previous = seen[seq.text]
                raise WorkbenchError(
                    E_EVENT_SEQUENCE_DUPLICATE,
                    "event_seq values must be unique within the sample",
                    {
                        "bucket_id": bucket_id,
                        "event_seq": seq.raw,
                        "previous_event_seq": previous.raw,
                    },
                )
            seen[seq.text] = seq
            details["event_seq"] = seq.raw
            instance_id = item.get("instance_id")
            if not isinstance(instance_id, str) or not instance_id.strip():
                raise WorkbenchError(E_EVENT_INVALID, "instance_id must be a non-empty string", details)
            instance_id = instance_id.strip()
            if instance_id not in topology.instances:
                raise WorkbenchError(
                    E_EVENT_UNKNOWN_INSTANCE,
                    "event references an instance absent from the active topology",
                    {**details, "instance_id": instance_id, "topology_version": topology.version},
                )
            rule_name = item.get("rule_name")
            if not isinstance(rule_name, str) or not rule_name.strip():
                raise WorkbenchError(E_EVENT_INVALID, "rule_name must be a non-empty string", details)
            rule_name = rule_name.strip()
            severity = item.get("severity")
            if not isinstance(severity, str) or severity.lower() not in SEVERITIES:
                raise WorkbenchError(
                    E_EVENT_SEVERITY_INVALID,
                    "severity must be one of info, warning, critical",
                    {**details, "severity": severity},
                )
            severity = severity.lower()
            occurred = _nonnegative_int(item.get("occurred_clock_ms"), "occurred_clock_ms", details)
            arrived = _nonnegative_int(item.get("arrived_clock_ms"), "arrived_clock_ms", details)
            raw_metric = item.get("metric_value")
            if (
                isinstance(raw_metric, bool)
                or not isinstance(raw_metric, (int, float))
                or not math.isfinite(float(raw_metric))
            ):
                raise WorkbenchError(
                    E_EVENT_INVALID,
                    "metric_value must be a finite number",
                    {**details, "metric_value": raw_metric},
                )
            silenced = item.get("silenced_maintenance")
            if not isinstance(silenced, bool):
                raise WorkbenchError(
                    E_EVENT_INVALID,
                    "silenced_maintenance must be boolean",
                    details,
                )
            event = Event(
                event_seq=seq,
                instance_id=instance_id,
                rule_name=rule_name,
                severity=severity,
                occurred_clock_ms=occurred,
                arrived_clock_ms=arrived,
                metric_value=float(raw_metric),
                silenced_maintenance=silenced,
                bucket_id=bucket_id,
                payload_json=canonical_json(
                    {
                        "arrived_clock_ms": arrived,
                        "event_seq": seq.raw,
                        "instance_id": instance_id,
                        "metric_value": float(raw_metric),
                        "occurred_clock_ms": occurred,
                        "rule_name": rule_name,
                        "severity": severity,
                        "silenced_maintenance": silenced,
                    }
                ),
            )
            events.append(event)
    events.sort(key=lambda item: item.sort_key)
    return events


def parse_events_file(path: Path, topology: Topology) -> list[Event]:
    return parse_events_payload(load_json_file(path), topology)


def _normalized_changes(payload: dict[str, Any]) -> dict[str, list[Any]]:
    raw_changes = payload.get("changes", payload)
    if not isinstance(raw_changes, dict):
        raise WorkbenchError(E_PATCH_INVALID, "changes must be an object")

    def raw_list(*keys: str) -> list[Any]:
        for key in keys:
            value = raw_changes.get(key)
            if value is not None:
                if not isinstance(value, list):
                    raise WorkbenchError(E_PATCH_INVALID, f"{key} must be a list")
                return value
        return []

    dependency_add: list[dict[str, str]] = []
    for item in raw_list("dependency_add", "dependencies_add", "add_dependencies"):
        if isinstance(item, str) and "->" in item:
            service, upstream = item.split("->", 1)
            item = {"service_name": service.strip(), "upstream_service": upstream.strip()}
        if not isinstance(item, dict):
            raise WorkbenchError(E_PATCH_INVALID, "dependency_add item must be an object")
        service = item.get("service_name", item.get("service"))
        upstream = item.get("upstream_service", item.get("upstream"))
        if not isinstance(service, str) or not isinstance(upstream, str):
            raise WorkbenchError(
                E_PATCH_INVALID,
                "dependency_add requires service_name and upstream_service",
            )
        dependency_add.append({"service_name": service.strip(), "upstream_service": upstream.strip()})

    dependency_remove: list[dict[str, str]] = []
    for item in raw_list("dependency_remove", "dependencies_remove", "remove_dependencies"):
        if isinstance(item, str) and "->" in item:
            service, upstream = item.split("->", 1)
            item = {"service_name": service.strip(), "upstream_service": upstream.strip()}
        if not isinstance(item, dict):
            raise WorkbenchError(E_PATCH_INVALID, "dependency_remove item must be an object")
        service = item.get("service_name", item.get("service"))
        upstream = item.get("upstream_service", item.get("upstream"))
        if not isinstance(service, str) or not isinstance(upstream, str):
            raise WorkbenchError(
                E_PATCH_INVALID,
                "dependency_remove requires service_name and upstream_service",
            )
        dependency_remove.append({"service_name": service.strip(), "upstream_service": upstream.strip()})

    weight_changes: list[dict[str, Any]] = []
    raw_weights = raw_changes.get("instance_weights", raw_changes.get("weight_changes", []))
    if isinstance(raw_weights, dict):
        raw_weights = [{"instance_id": key, "weight": value} for key, value in raw_weights.items()]
    if not isinstance(raw_weights, list):
        raise WorkbenchError(E_PATCH_INVALID, "instance_weights must be a list or object")
    for item in raw_weights:
        if not isinstance(item, dict):
            raise WorkbenchError(E_PATCH_INVALID, "instance_weights item must be an object")
        instance_id = item.get("instance_id")
        weight = item.get("weight")
        if not isinstance(instance_id, str) or not instance_id.strip():
            raise WorkbenchError(E_PATCH_INVALID, "instance_weights requires instance_id")
        if (
            isinstance(weight, bool)
            or not isinstance(weight, (int, float))
            or not math.isfinite(float(weight))
            or float(weight) <= 0
        ):
            raise WorkbenchError(
                E_PATCH_INVALID,
                "instance weight must be a finite number greater than zero",
            )
        weight_changes.append({"instance_id": instance_id.strip(), "weight": float(weight)})

    silence_add: list[dict[str, Any]] = []
    for item in raw_list("silence_add", "silence_windows_add", "add_silences"):
        if not isinstance(item, dict):
            raise WorkbenchError(E_PATCH_INVALID, "silence_add item must be an object")
        silence_add.append(dict(item))

    silence_remove: list[str] = []
    for item in raw_list("silence_remove", "silence_windows_remove", "remove_silences"):
        if isinstance(item, dict):
            item = item.get("silence_id")
        if not isinstance(item, str) or not item.strip():
            raise WorkbenchError(E_PATCH_INVALID, "silence_remove requires silence_id values")
        silence_remove.append(item.strip())

    return {
        "dependency_add": sorted(
            dependency_add,
            key=lambda item: (item["service_name"], item["upstream_service"]),
        ),
        "dependency_remove": sorted(
            dependency_remove,
            key=lambda item: (item["service_name"], item["upstream_service"]),
        ),
        "instance_weights": sorted(weight_changes, key=lambda item: item["instance_id"]),
        "silence_add": sorted(
            silence_add,
            key=lambda item: str(item.get("silence_id", item.get("id", ""))),
        ),
        "silence_remove": sorted(set(silence_remove)),
    }


def patch_digest_content(payload: dict[str, Any], normalized_changes: dict[str, list[Any]] | None = None) -> dict[str, Any]:
    return {
        "changes": normalized_changes if normalized_changes is not None else _normalized_changes(payload),
        "effective_clock_ms": payload.get("effective_clock_ms", 0),
        "parent_topology_version": payload.get(
            "parent_topology_version", payload.get("base_topology_version")
        ),
        "patched_topology_version": payload.get(
            "patched_topology_version", payload.get("target_topology_version")
        ),
    }


def compute_patch_digest(payload: dict[str, Any]) -> str:
    return digest(patch_digest_content(payload))


def _int_field(value: Any, field: str, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise WorkbenchError(code, f"{field} must be an integer", {"field": field})
    return value


def inspect_patch_payload(payload: Any) -> PatchInspection:
    if not isinstance(payload, dict):
        raise WorkbenchError(E_PATCH_INVALID, "patch sample must be a JSON object")
    parent = _int_field(
        payload.get("parent_topology_version", payload.get("base_topology_version")),
        "parent_topology_version",
        E_PATCH_INVALID,
    )
    target = _int_field(
        payload.get("patched_topology_version", payload.get("target_topology_version")),
        "patched_topology_version",
        E_PATCH_INVALID,
    )
    if target != parent + 1:
        raise WorkbenchError(
            E_PATCH_VERSION_SEQUENCE,
            "target topology version must be exactly parent + 1",
            {"parent_topology_version": parent, "target_topology_version": target},
        )
    normalized = _normalized_changes(payload)
    provided_digest = payload.get("content_digest", payload.get("digest"))
    if not isinstance(provided_digest, str) or not provided_digest.strip():
        raise WorkbenchError(
            E_PATCH_DIGEST_MISMATCH,
            "content_digest is required",
            {"parent_topology_version": parent, "target_topology_version": target},
        )
    expected = compute_patch_digest(payload)
    raw_body = {key: value for key, value in payload.items() if key not in {"content_digest", "digest"}}
    raw_digest = digest(raw_body)
    supplied = normalize_digest(provided_digest)
    if supplied not in {normalize_digest(expected), normalize_digest(raw_digest)}:
        raise WorkbenchError(
            E_PATCH_DIGEST_MISMATCH,
            "patch content_digest does not match normalized fields",
            {
                "expected": expected,
                "provided": provided_digest,
                "target_topology_version": target,
            },
        )
    effective_clock = _int_field(
        payload.get("effective_clock_ms", 0),
        "effective_clock_ms",
        E_PATCH_INVALID,
    )
    if effective_clock < 0:
        raise WorkbenchError(
            E_PATCH_INVALID,
            "effective_clock_ms must be non-negative",
            {"effective_clock_ms": effective_clock},
        )
    return PatchInspection(
        parent_version=parent,
        target_version=target,
        effective_clock_ms=effective_clock,
        content_digest=expected,
        normalized_changes=normalized,
        payload=payload,
    )


def parse_patch_payload(payload: Any, current: Topology) -> PatchPlan:
    inspection = inspect_patch_payload(payload)
    parent = inspection.parent_version
    target = inspection.target_version
    normalized = inspection.normalized_changes
    expected = inspection.content_digest

    dependency_add: list[tuple[str, str]] = []
    for item in normalized["dependency_add"]:
        service = item["service_name"]
        upstream = item["upstream_service"]
        if service not in current.services:
            raise WorkbenchError(
                E_PATCH_UNKNOWN_SERVICE,
                "dependency_add references an undeclared service",
                {"service_name": service, "target_topology_version": target},
            )
        if upstream not in current.services:
            raise WorkbenchError(
                E_PATCH_UNKNOWN_SERVICE,
                "dependency_add references an undeclared upstream service",
                {"service_name": service, "upstream_service": upstream},
            )
        if (service, upstream) in current.dependency_pairs():
            raise WorkbenchError(
                E_PATCH_CONFLICT,
                "dependency_add repeats an existing edge",
                {"service_name": service, "upstream_service": upstream},
            )
        dependency_add.append((service, upstream))

    dependency_remove: list[tuple[str, str]] = []
    for item in normalized["dependency_remove"]:
        service = item["service_name"]
        upstream = item["upstream_service"]
        if service not in current.services or upstream not in current.services:
            raise WorkbenchError(
                E_PATCH_UNKNOWN_SERVICE,
                "dependency_remove references an undeclared service",
                {"service_name": service, "upstream_service": upstream},
            )
        if (service, upstream) not in current.dependency_pairs():
            raise WorkbenchError(
                E_PATCH_UNKNOWN_DEPENDENCY,
                "dependency_remove edge does not exist",
                {"service_name": service, "upstream_service": upstream},
            )
        dependency_remove.append((service, upstream))

    weights: list[tuple[str, float]] = []
    for item in normalized["instance_weights"]:
        instance_id = item["instance_id"]
        if instance_id not in current.instances:
            raise WorkbenchError(
                E_PATCH_UNKNOWN_INSTANCE,
                "weight change references an undeclared instance",
                {"instance_id": instance_id, "target_topology_version": target},
            )
        weights.append((instance_id, float(item["weight"])))

    silences_add: list[SilenceWindow] = []
    known_silence_ids = set(current.silences)
    for index, item in enumerate(normalized["silence_add"]):
        instance_id = item.get("instance_id")
        if instance_id is not None and instance_id not in current.instances:
            raise WorkbenchError(
                E_PATCH_UNKNOWN_INSTANCE,
                "silence_add references an undeclared instance",
                {"instance_id": instance_id, "silence_id": item.get("silence_id", item.get("id"))},
            )
        service_name = item.get("service_name")
        if service_name is not None and service_name not in current.services:
            raise WorkbenchError(
                E_PATCH_UNKNOWN_SERVICE,
                "silence_add references an undeclared service",
                {"service_name": service_name, "silence_id": item.get("silence_id", item.get("id"))},
            )
        silence = parse_silence_window(item, current.instances, current.services, index)
        if silence.silence_id in known_silence_ids:
            raise WorkbenchError(
                E_PATCH_CONFLICT,
                "silence_add repeats an existing silence ID",
                {"silence_id": silence.silence_id},
            )
        known_silence_ids.add(silence.silence_id)
        silences_add.append(silence)

    silences_remove: list[str] = []
    for silence_id in normalized["silence_remove"]:
        if silence_id not in current.silences:
            raise WorkbenchError(
                E_PATCH_UNKNOWN_SILENCE,
                "silence_remove references an unknown silence ID",
                {"silence_id": silence_id},
            )
        if silence_id in {item.silence_id for item in silences_add}:
            raise WorkbenchError(
                E_PATCH_CONFLICT,
                "the same silence ID cannot be added and removed",
                {"silence_id": silence_id},
            )
        silences_remove.append(silence_id)

    effective_clock = inspection.effective_clock_ms
    if effective_clock == 0 and "effective_clock_ms" not in payload:
        effective_clock = current.effective_clock_ms

    new_topology = replace_service_upstreams(
        current,
        add=dependency_add,
        remove=dependency_remove,
        weights=dict(weights),
        silence_add=silences_add,
        silence_remove=silences_remove,
        version=target,
        effective_clock_ms=effective_clock,
        parent_version=parent,
        source_kind="patch",
        source_digest=digest(patch_digest_content(payload, normalized)),
    )
    return PatchPlan(
        parent_version=parent,
        target_version=target,
        effective_clock_ms=effective_clock,
        content_digest=expected,
        dependency_add=tuple(dependency_add),
        dependency_remove=tuple(dependency_remove),
        weight_changes=tuple(weights),
        silence_add=tuple(silences_add),
        silence_remove=tuple(silences_remove),
        payload_json=canonical_json(payload),
        new_topology=new_topology,
    )


def parse_patch_file(path: Path, current: Topology) -> PatchPlan:
    return parse_patch_payload(load_json_file(path), current)
