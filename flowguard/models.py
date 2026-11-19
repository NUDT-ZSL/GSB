"""Input parsing, normalization, and strict validation."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from .errors import ErrorCode, FlowGuardError, validation_error
from .util import canonical_json, cost_to_units, is_int, is_number, sha256_json


VALID_RESULTS = {"success": "success", "ok": "success", "error": "error", "failed": "error", "failure": "error"}


@dataclass(frozen=True)
class Service:
    name: str
    instances: tuple[str, ...]
    priority: int


@dataclass(frozen=True)
class Topology:
    services: tuple[Service, ...]
    dependencies: tuple[tuple[str, str], ...]

    def service_map(self) -> dict[str, Service]:
        return {service.name: service for service in self.services}

    def normalized(self) -> dict[str, Any]:
        return {
            "version": 1,
            "services": [
                {
                    "name": service.name,
                    "instances": list(service.instances),
                    "priority": service.priority,
                }
                for service in sorted(self.services, key=lambda item: item.name)
            ],
            "dependencies": [
                {"from": source, "to": target}
                for source, target in sorted(self.dependencies)
            ],
        }


@dataclass(frozen=True)
class Event:
    seq: int
    service: str
    clock_ms: int
    result: str
    latency_ms: float
    cost_weight: float

    @property
    def cost_units(self) -> int:
        return cost_to_units(self.cost_weight)

    def normalized(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "service": self.service,
            "clock_ms": self.clock_ms,
            "result": self.result,
            "latency_ms": self.latency_ms,
            "cost_weight": self.cost_weight,
        }

    def digest(self) -> str:
        return sha256_json(self.normalized())


@dataclass(frozen=True)
class Policy:
    qps_limit: int
    token_bucket_capacity: int
    window_length_ms: int
    window_step_ms: int
    breaker_error_rate_threshold: float
    breaker_min_requests: int
    breaker_consecutive_failures: int
    breaker_open_ms: int
    half_open_max_probes: int
    half_open_successes_required: int
    degrade_level: int
    degrade_allow_ratio: float
    degrade_error_rate_threshold: float
    degrade_stable_windows: int
    retention_limit: int
    clock_tolerance_ms: int
    summary_bins: int
    summary_quantile: float
    mode_restore_ratio: float

    @property
    def active_segment_count(self) -> int:
        return max(1, math.ceil(self.window_length_ms / self.window_step_ms))

    @property
    def restore_threshold(self) -> int:
        return max(1, int(math.floor(self.retention_limit * self.mode_restore_ratio)))

    def normalized(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PolicySet:
    by_service: dict[str, Policy]

    def get(self, service: str) -> Policy:
        try:
            return self.by_service[service]
        except KeyError as exc:
            raise validation_error(
                ErrorCode.POLICY_INVALID,
                "policy is missing for service",
                service=service,
            ) from exc

    def normalized(self) -> dict[str, Any]:
        return {
            "version": 1,
            "services": {
                name: policy.normalized()
                for name, policy in sorted(self.by_service.items())
            },
        }


@dataclass(frozen=True)
class Sample:
    topology: Topology
    events: tuple[Event, ...]
    policies: PolicySet
    topology_digest: str
    events_digest: str
    policy_digest: str

    def digests(self) -> dict[str, str]:
        return {
            "topology": self.topology_digest,
            "events": self.events_digest,
            "policy": self.policy_digest,
        }


def load_json_file(path: str | Path) -> Any:
    source = Path(path)
    try:
        with source.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError as exc:
        raise FlowGuardError(
            ErrorCode.NOT_FOUND,
            "JSON file not found",
            {"path": str(source)},
            exit_code=2,
        ) from exc
    except OSError as exc:
        raise FlowGuardError(
            ErrorCode.EVENT_SCHEMA_INVALID,
            "cannot read JSON file",
            {"path": str(source), "reason": str(exc)},
            exit_code=2,
        ) from exc
    except json.JSONDecodeError as exc:
        raise validation_error(
            ErrorCode.EVENT_SCHEMA_INVALID,
            "invalid JSON",
            path=str(source),
            line=exc.lineno,
            column=exc.colno,
        ) from exc


def parse_topology(raw: Any) -> Topology:
    if not isinstance(raw, dict):
        raise validation_error(ErrorCode.TOPOLOGY_INVALID, "topology must be an object")
    service_rows = raw.get("services")
    if not isinstance(service_rows, list) or not service_rows:
        raise validation_error(
            ErrorCode.TOPOLOGY_INVALID,
            "topology.services must be a non-empty array",
            field="services",
        )
    dependencies_raw = raw.get("dependencies", raw.get("edges", []))
    if not isinstance(dependencies_raw, list):
        raise validation_error(
            ErrorCode.TOPOLOGY_INVALID,
            "topology.dependencies must be an array",
            field="dependencies",
        )

    services: list[Service] = []
    names: set[str] = set()
    for index, row in enumerate(service_rows):
        if not isinstance(row, dict):
            raise validation_error(
                ErrorCode.TOPOLOGY_INVALID,
                "service row must be an object",
                index=index,
            )
        name = row.get("name")
        if not isinstance(name, str) or not name.strip():
            raise validation_error(
                ErrorCode.TOPOLOGY_INVALID,
                "service name must be a non-empty string",
                index=index,
            )
        name = name.strip()
        if name in names:
            raise validation_error(
                ErrorCode.TOPOLOGY_DUPLICATE_SERVICE,
                "service name must be unique",
                service=name,
                index=index,
            )
        names.add(name)
        instances = row.get("instances")
        if (
            not isinstance(instances, list)
            or not instances
            or any(not isinstance(item, str) or not item.strip() for item in instances)
        ):
            raise validation_error(
                ErrorCode.TOPOLOGY_INVALID,
                "service.instances must be a non-empty string array",
                service=name,
            )
        instance_names = [item.strip() for item in instances]
        if len(set(instance_names)) != len(instance_names):
            raise validation_error(
                ErrorCode.TOPOLOGY_INVALID,
                "service instances must be unique",
                service=name,
            )
        priority = row.get("priority", 0)
        if not is_int(priority) or priority < 0:
            raise validation_error(
                ErrorCode.TOPOLOGY_INVALID,
                "service priority must be a non-negative integer",
                service=name,
            )
        services.append(Service(name, tuple(instance_names), priority))

    dependencies: list[tuple[str, str]] = []
    seen_edges: set[tuple[str, str]] = set()
    for index, row in enumerate(dependencies_raw):
        if not isinstance(row, dict):
            raise validation_error(
                ErrorCode.TOPOLOGY_INVALID,
                "dependency row must be an object",
                index=index,
            )
        source = row.get("from", row.get("source"))
        target = row.get("to", row.get("target"))
        if not isinstance(source, str) or not isinstance(target, str):
            raise validation_error(
                ErrorCode.TOPOLOGY_INVALID,
                "dependency endpoints must be strings",
                index=index,
            )
        if source not in names or target not in names:
            raise validation_error(
                ErrorCode.TOPOLOGY_UNKNOWN_DEPENDENCY,
                "dependency endpoint is not closed by the service list",
                index=index,
                source=source,
                target=target,
            )
        edge = (source, target)
        if edge in seen_edges:
            raise validation_error(
                ErrorCode.TOPOLOGY_INVALID,
                "duplicate dependency edge",
                source=source,
                target=target,
            )
        seen_edges.add(edge)
        dependencies.append(edge)
    return Topology(tuple(services), tuple(dependencies))


def _merged_policy_rows(raw: Any, services: Iterable[str]) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, dict):
        raise validation_error(ErrorCode.POLICY_INVALID, "policy must be an object")
    default_raw = raw.get("default", raw)
    if not isinstance(default_raw, dict):
        raise validation_error(
            ErrorCode.POLICY_INVALID,
            "policy.default must be an object",
        )
    overrides_raw = raw.get("services", {})
    if overrides_raw is None:
        overrides_raw = {}
    if not isinstance(overrides_raw, dict):
        raise validation_error(
            ErrorCode.POLICY_INVALID,
            "policy.services must be an object",
        )
    unknown = set(overrides_raw) - set(services)
    if unknown:
        raise validation_error(
            ErrorCode.POLICY_INVALID,
            "policy contains unknown services",
            services=sorted(unknown),
        )
    result: dict[str, dict[str, Any]] = {}
    for service in services:
        merged = dict(default_raw)
        override = overrides_raw.get(service, {})
        if not isinstance(override, dict):
            raise validation_error(
                ErrorCode.POLICY_INVALID,
                "service policy must be an object",
                service=service,
            )
        merged.update(override)
        for key in ("default", "services", "version"):
            merged.pop(key, None)
        result[service] = merged
    return result


def _policy_from_row(row: dict[str, Any], service: str) -> Policy:
    aliases = {
        "qps_limit": ("qps", "requests_per_second", "rate_limit_per_second"),
        "token_bucket_capacity": ("bucket_capacity",),
        "window_length_ms": ("window_ms", "window_length"),
        "window_step_ms": ("step_ms", "window_step"),
        "breaker_error_rate_threshold": (
            "error_rate_threshold",
            "breaker_threshold",
        ),
        "breaker_min_requests": ("min_requests",),
        "breaker_consecutive_failures": ("consecutive_failures",),
        "breaker_open_ms": ("open_duration_ms",),
        "half_open_max_probes": ("half_open_probes",),
        "half_open_successes_required": ("half_open_successes",),
        "degrade_allow_ratio": ("degrade_ratio", "allow_ratio"),
        "degrade_error_rate_threshold": ("degrade_threshold",),
        "retention_limit": ("window_retention_limit", "max_raw_events"),
        "clock_tolerance_ms": ("tolerance_ms", "clock_rollback_tolerance_ms"),
        "summary_quantile": ("quantile",),
    }
    for canonical, alternatives in aliases.items():
        if canonical not in row:
            for alternative in alternatives:
                if alternative in row:
                    row[canonical] = row[alternative]
                    break

    def integer(name: str, default: int | None = None) -> int:
        if name not in row and default is not None:
            return default
        value = row.get(name)
        if not is_int(value):
            raise validation_error(
                ErrorCode.POLICY_INVALID,
                f"{name} must be an integer",
                service=service,
                field=name,
            )
        return value

    def number(name: str, default: float | None = None) -> float:
        if name not in row and default is not None:
            return default
        value = row.get(name)
        if not is_number(value):
            raise validation_error(
                ErrorCode.POLICY_INVALID,
                f"{name} must be a finite number",
                service=service,
                field=name,
            )
        return float(value)

    qps = integer("qps_limit")
    if qps <= 0:
        raise validation_error(
            ErrorCode.POLICY_QPS_INVALID,
            "qps_limit must be a positive integer",
            service=service,
            value=qps,
        )
    capacity = integer("token_bucket_capacity")
    if capacity <= 0:
        raise validation_error(
            ErrorCode.POLICY_BUCKET_INVALID,
            "token_bucket_capacity must be a positive integer",
            service=service,
            value=capacity,
        )
    window_length = integer("window_length_ms")
    window_step = integer("window_step_ms")
    if window_length <= 0 or window_step <= 0:
        raise validation_error(
            ErrorCode.POLICY_WINDOW_INVALID,
            "window length and step must be positive",
            service=service,
            window_length_ms=window_length,
            window_step_ms=window_step,
        )
    if window_step > window_length:
        raise validation_error(
            ErrorCode.POLICY_WINDOW_INVALID,
            "window_step_ms must not exceed window_length_ms",
            service=service,
            window_length_ms=window_length,
            window_step_ms=window_step,
        )

    breaker_rate = number("breaker_error_rate_threshold")
    if not 0.0 < breaker_rate <= 1.0:
        raise validation_error(
            ErrorCode.POLICY_RATIO_INVALID,
            "breaker_error_rate_threshold must be in (0, 1]",
            service=service,
            value=breaker_rate,
        )
    degrade_rate = number("degrade_error_rate_threshold", breaker_rate)
    if not 0.0 < degrade_rate <= 1.0:
        raise validation_error(
            ErrorCode.POLICY_RATIO_INVALID,
            "degrade_error_rate_threshold must be in (0, 1]",
            service=service,
            value=degrade_rate,
        )
    degrade_ratio = number("degrade_allow_ratio")
    if not 0.0 <= degrade_ratio <= 1.0:
        raise validation_error(
            ErrorCode.POLICY_RATIO_INVALID,
            "degrade_allow_ratio must be in [0, 1]",
            service=service,
            value=degrade_ratio,
        )
    restore_ratio = number("mode_restore_ratio", 0.8)
    if not 0.0 <= restore_ratio <= 1.0:
        raise validation_error(
            ErrorCode.POLICY_RATIO_INVALID,
            "mode_restore_ratio must be in [0, 1]",
            service=service,
            value=restore_ratio,
        )
    quantile = number("summary_quantile", 0.95)
    if not 0.0 < quantile <= 1.0:
        raise validation_error(
            ErrorCode.POLICY_RATIO_INVALID,
            "summary_quantile must be in (0, 1]",
            service=service,
            value=quantile,
        )

    min_requests = integer("breaker_min_requests")
    consecutive = integer("breaker_consecutive_failures")
    open_ms = integer("breaker_open_ms")
    max_probes = integer("half_open_max_probes")
    required_successes = integer("half_open_successes_required")
    level = integer("degrade_level")
    stable_windows = integer("degrade_stable_windows", 1)
    retention = integer("retention_limit")
    tolerance = integer("clock_tolerance_ms")
    bins = integer("summary_bins", 32)
    if min_requests <= 0 or consecutive <= 0 or max_probes <= 0:
        raise validation_error(
            ErrorCode.POLICY_INVALID,
            "breaker thresholds and probe counts must be positive",
            service=service,
        )
    if required_successes <= 0 or required_successes > max_probes:
        raise validation_error(
            ErrorCode.POLICY_INVALID,
            "half_open_successes_required must be in [1, half_open_max_probes]",
            service=service,
        )
    if open_ms < 0:
        raise validation_error(
            ErrorCode.POLICY_INVALID,
            "breaker_open_ms must be non-negative",
            service=service,
        )
    if level < 0:
        raise validation_error(
            ErrorCode.POLICY_INVALID,
            "degrade_level must be non-negative",
            service=service,
        )
    if stable_windows < 1:
        raise validation_error(
            ErrorCode.POLICY_INVALID,
            "degrade_stable_windows must be at least 1",
            service=service,
        )
    if retention <= 0:
        raise validation_error(
            ErrorCode.POLICY_INVALID,
            "retention_limit must be positive",
            service=service,
        )
    if tolerance < 0:
        raise validation_error(
            ErrorCode.POLICY_INVALID,
            "clock_tolerance_ms must be non-negative",
            service=service,
        )
    if bins < 4:
        raise validation_error(
            ErrorCode.POLICY_INVALID,
            "summary_bins must be at least 4",
            service=service,
        )
    return Policy(
        qps_limit=qps,
        token_bucket_capacity=capacity,
        window_length_ms=window_length,
        window_step_ms=window_step,
        breaker_error_rate_threshold=breaker_rate,
        breaker_min_requests=min_requests,
        breaker_consecutive_failures=consecutive,
        breaker_open_ms=open_ms,
        half_open_max_probes=max_probes,
        half_open_successes_required=required_successes,
        degrade_level=level,
        degrade_allow_ratio=degrade_ratio,
        degrade_error_rate_threshold=degrade_rate,
        degrade_stable_windows=stable_windows,
        retention_limit=retention,
        clock_tolerance_ms=tolerance,
        summary_bins=bins,
        summary_quantile=quantile,
        mode_restore_ratio=restore_ratio,
    )


def parse_policy(raw: Any, topology: Topology) -> PolicySet:
    services = [service.name for service in topology.services]
    rows = _merged_policy_rows(raw, services)
    return PolicySet(
        {service: _policy_from_row(rows[service], service) for service in services}
    )


def parse_event_row(
    row: Any,
    index: int,
    known_services: set[str] | None = None,
) -> Event:
    if not isinstance(row, dict):
        raise validation_error(
            ErrorCode.EVENT_SCHEMA_INVALID,
            "event must be an object",
            index=index,
        )
    seq = row.get("seq", row.get("event_seq"))
    if not is_int(seq) or seq <= 0:
        raise validation_error(
            ErrorCode.EVENT_SCHEMA_INVALID,
            "event.seq must be a positive integer",
            index=index,
        )
    service = row.get("service")
    if not isinstance(service, str) or not service.strip():
        raise validation_error(
            ErrorCode.EVENT_SCHEMA_INVALID,
            "event.service must be a non-empty string",
            index=index,
            seq=seq,
        )
    service = service.strip()
    if known_services is not None and service not in known_services:
        raise validation_error(
            ErrorCode.EVENT_FIELD_INVALID,
            "event service is not present in topology",
            index=index,
            seq=seq,
            service=service,
        )
    clock_ms = row.get("clock_ms", row.get("timestamp_ms", row.get("ts_ms")))
    if not is_int(clock_ms) or clock_ms < 0:
        raise validation_error(
            ErrorCode.EVENT_CLOCK_INVALID,
            "event.clock_ms must be a non-negative integer",
            index=index,
            seq=seq,
            value=clock_ms,
        )
    result_raw = row.get("result", row.get("result_mark", row.get("outcome")))
    if not isinstance(result_raw, str):
        raise validation_error(
            ErrorCode.EVENT_RESULT_INVALID,
            "event.result must be a recognized string marker",
            index=index,
            seq=seq,
            value=result_raw,
        )
    result = VALID_RESULTS.get(result_raw.strip().lower())
    if result is None:
        raise validation_error(
            ErrorCode.EVENT_RESULT_INVALID,
            "event.result must be success/ok/error/failed/failure",
            index=index,
            seq=seq,
            value=result_raw,
        )
    latency = row.get("latency_ms")
    if not is_number(latency):
        raise validation_error(
            ErrorCode.EVENT_LATENCY_INVALID,
            "event.latency_ms must be a finite number",
            index=index,
            seq=seq,
            value=latency,
        )
    if float(latency) < 0:
        raise validation_error(
            ErrorCode.EVENT_LATENCY_INVALID,
            "event.latency_ms must be non-negative",
            index=index,
            seq=seq,
            value=latency,
        )
    cost = row.get("cost_weight", row.get("cost", 1.0))
    if not is_number(cost) or float(cost) <= 0:
        raise validation_error(
            ErrorCode.EVENT_FIELD_INVALID,
            "event.cost_weight must be a positive finite number",
            index=index,
            seq=seq,
            value=cost,
        )
    return Event(
        seq=seq,
        service=service,
        clock_ms=clock_ms,
        result=result,
        latency_ms=float(latency),
        cost_weight=float(cost),
    )


def parse_events(raw: Any, topology: Topology) -> tuple[Event, ...]:
    if isinstance(raw, list):
        rows = raw
    elif isinstance(raw, dict):
        rows = raw.get("events")
    else:
        rows = None
    if not isinstance(rows, list) or not rows:
        raise validation_error(
            ErrorCode.EVENT_SCHEMA_INVALID,
            "event file must contain a non-empty events array",
        )
    known_services = set(topology.service_map())
    events: list[Event] = []
    previous = 0
    for index, row in enumerate(rows):
        event = parse_event_row(row, index, known_services)
        if event.seq <= previous:
            raise validation_error(
                ErrorCode.EVENT_SEQ_NOT_INCREASING,
                "event.seq must be strictly increasing",
                index=index,
                previous_seq=previous,
                seq=event.seq,
            )
        previous = event.seq
        events.append(event)
    return tuple(events)


def parse_sample(topology_raw: Any, events_raw: Any, policy_raw: Any) -> Sample:
    topology = parse_topology(topology_raw)
    policies = parse_policy(policy_raw, topology)
    events = parse_events(events_raw, topology)
    topology_normalized = topology.normalized()
    policy_normalized = policies.normalized()
    events_normalized = [event.normalized() for event in events]
    return Sample(
        topology=topology,
        events=events,
        policies=policies,
        topology_digest=sha256_json(topology_normalized),
        events_digest=sha256_json(events_normalized),
        policy_digest=sha256_json(policy_normalized),
    )


def canonical_event_json(event: Event) -> str:
    return canonical_json(event.normalized())
