"""Sample loading and strict policy/event validation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .errors import (
    E_BAD_ANOMALY,
    E_BAD_EVENT,
    E_BAD_JSON,
    E_BAD_POLICY,
    E_BAD_TOPOLOGY,
    PipelineError,
    RUNTIME_ANOMALY_CODES,
)


RESULT_SUCCESS = {"success", "ok", "pass", "passed", "true", True}
RESULT_FAILURE = {"failure", "fail", "failed", "error", "timeout", "false", False}
VALID_STAGES = {"parse", "rate_limit", "circuit", "degradation"}


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _bad(code: str, message: str, rule: str, **details: Any) -> PipelineError:
    merged = {"rule": rule}
    merged.update(details)
    return PipelineError(code, message, details=merged)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require_int(
    value: Any,
    *,
    code: str,
    rule: str,
    field: str,
    minimum: Optional[int] = None,
) -> int:
    if not _is_int(value):
        raise _bad(code, f"{field} must be an integer", rule, field=field, value=value)
    if minimum is not None and value < minimum:
        raise _bad(
            code,
            f"{field} must be >= {minimum}",
            rule,
            field=field,
            value=value,
        )
    return value


def _require_str(value: Any, *, code: str, rule: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _bad(code, f"{field} must be a non-empty string", rule, field=field, value=value)
    return value.strip()


def _decimal(value: Any, *, code: str, rule: str, field: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise _bad(code, f"{field} must be a number", rule, field=field, value=value)
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise _bad(code, f"{field} must be a number", rule, field=field, value=value)
    if not parsed.is_finite():
        raise _bad(code, f"{field} must be finite", rule, field=field, value=value)
    return parsed


def _scaled(value: Decimal, scale: int) -> int:
    return int((value * scale).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


@dataclass(frozen=True)
class BucketPolicy:
    capacity: int
    refill_per_sec: int
    burst_depth: int

    @property
    def capacity_milli(self) -> int:
        return self.capacity * 1000

    @property
    def burst_milli(self) -> int:
        return self.burst_depth * 1000

    def to_dict(self) -> Dict[str, int]:
        return {
            "capacity": self.capacity,
            "refill_per_sec": self.refill_per_sec,
            "burst_depth": self.burst_depth,
        }


@dataclass(frozen=True)
class ServiceNode:
    name: str
    instance_id: str
    upstream: Tuple[str, ...]
    priority: int
    fault_domain: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "instance_id": self.instance_id,
            "upstream": list(self.upstream),
            "priority": self.priority,
            "fault_domain": self.fault_domain,
        }


@dataclass(frozen=True)
class CircuitPolicy:
    window_length_ms: int
    step_ms: int
    failure_rate_threshold_micro: int
    half_open_probe_count: int
    cooldown_ms: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "window_length_ms": self.window_length_ms,
            "step_ms": self.step_ms,
            "failure_rate_threshold": str(
                (Decimal(self.failure_rate_threshold_micro) / Decimal(1_000_000))
                .normalize()
            ),
            "failure_rate_threshold_micro": self.failure_rate_threshold_micro,
            "half_open_probe_count": self.half_open_probe_count,
            "cooldown_ms": self.cooldown_ms,
        }


@dataclass(frozen=True)
class DegradationLevel:
    name: str
    ratio_bps: int

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "ratio_bps": self.ratio_bps}


@dataclass(frozen=True)
class DegradationPolicy:
    levels: Tuple[DegradationLevel, ...]
    initial_index: int
    recovery_ladder: Tuple[str, ...]
    recovery_successes_per_step: int

    @property
    def name_to_index(self) -> Dict[str, int]:
        return {level.name: index for index, level in enumerate(self.levels)}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "levels": [level.to_dict() for level in self.levels],
            "initial": self.levels[self.initial_index].name,
            "recovery_ladder": list(self.recovery_ladder),
            "recovery_successes_per_step": self.recovery_successes_per_step,
        }


@dataclass(frozen=True)
class GlobalErrorBudgetPolicy:
    max_failures: int
    window_ms: int

    def to_dict(self) -> Dict[str, int]:
        return {"max_failures": self.max_failures, "window_ms": self.window_ms}


@dataclass(frozen=True)
class Event:
    seq: int
    service: str
    clock_ms: int
    success: bool
    result: str
    latency_us: int
    inject: Tuple[Dict[str, Any], ...]
    raw: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        payload = dict(self.raw)
        payload["seq"] = self.seq
        payload["service"] = self.service
        payload["clock_ms"] = self.clock_ms
        payload["result"] = self.result
        payload["latency_us"] = self.latency_us
        return payload


@dataclass(frozen=True)
class Anomaly:
    anomaly_id: str
    event_seq: int
    service: str
    stage: str
    kind: str
    error_code: str
    details: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "anomaly_id": self.anomaly_id,
            "event_seq": self.event_seq,
            "service": self.service,
            "stage": self.stage,
            "kind": self.kind,
            "error_code": self.error_code,
            "details": self.details,
        }


@dataclass(frozen=True)
class Policy:
    schema_version: int
    policy_version: str
    services: Mapping[str, ServiceNode]
    service_order: Tuple[str, ...]
    service_buckets: Mapping[str, BucketPolicy]
    global_bucket: BucketPolicy
    clock_rollback_tolerance_ms: int
    circuit: CircuitPolicy
    degradation: DegradationPolicy
    global_error_budget: Optional[GlobalErrorBudgetPolicy]
    raw: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.raw)


@dataclass(frozen=True)
class SampleBundle:
    policy: Policy
    events: Tuple[Event, ...]
    anomalies: Tuple[Anomaly, ...]
    config_raw: Dict[str, Any]
    events_raw: List[Dict[str, Any]]
    anomalies_raw: List[Dict[str, Any]]
    config_sha256: str
    events_sha256: str
    anomalies_sha256: str

    def anomalies_for(self, event_seq: int) -> Tuple[Anomaly, ...]:
        return tuple(item for item in self.anomalies if item.event_seq == event_seq)


def _read_json(path: Path, code: str) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        raise PipelineError(code, f"file not found: {path}", details={"path": str(path)})
    except json.JSONDecodeError as exc:
        raise PipelineError(
            E_BAD_JSON,
            f"invalid JSON in {path}: {exc.msg}",
            details={"path": str(path), "line": exc.lineno, "column": exc.colno},
        )
    except OSError as exc:
        raise PipelineError(
            code,
            f"cannot read {path}: {exc}",
            details={"path": str(path)},
        )


def _extract_list(value: Any, key: str, code: str) -> List[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, dict) and isinstance(value.get(key), list):
        return list(value[key])
    raise PipelineError(
        code,
        f"expected a JSON list or an object containing '{key}'",
        details={"rule": "container_shape"},
    )


def _parse_bucket(value: Any, field: str) -> BucketPolicy:
    if not isinstance(value, dict):
        raise _bad(E_BAD_POLICY, f"{field} must be an object", "bucket_object", field=field)
    capacity = _require_int(
        value.get("capacity"),
        code=E_BAD_POLICY,
        rule="bucket_capacity_positive",
        field=f"{field}.capacity",
        minimum=1,
    )
    refill = _require_int(
        value.get("refill_per_sec", value.get("refill_rate")),
        code=E_BAD_POLICY,
        rule="bucket_refill_positive",
        field=f"{field}.refill_per_sec",
        minimum=1,
    )
    burst = _require_int(
        value.get("burst_depth", value.get("burst")),
        code=E_BAD_POLICY,
        rule="bucket_burst_positive",
        field=f"{field}.burst_depth",
        minimum=1,
    )
    if burst > capacity:
        raise _bad(
            E_BAD_POLICY,
            f"{field}.burst_depth must not exceed capacity",
            "bucket_burst_le_capacity",
            field=field,
            capacity=capacity,
            burst_depth=burst,
        )
    return BucketPolicy(capacity=capacity, refill_per_sec=refill, burst_depth=burst)


def _parse_topology(value: Any) -> Tuple[Mapping[str, ServiceNode], Tuple[str, ...]]:
    if not isinstance(value, dict) or not isinstance(value.get("services"), list):
        raise _bad(
            E_BAD_TOPOLOGY,
            "topology.services must be a list",
            "topology_services_list",
        )
    services: Dict[str, ServiceNode] = {}
    instance_ids = set()
    for index, raw in enumerate(value["services"]):
        if not isinstance(raw, dict):
            raise _bad(
                E_BAD_TOPOLOGY,
                f"topology.services[{index}] must be an object",
                "service_object",
                index=index,
            )
        name = _require_str(
            raw.get("name"),
            code=E_BAD_TOPOLOGY,
            rule="service_name",
            field=f"topology.services[{index}].name",
        )
        if name in services:
            raise _bad(
                E_BAD_TOPOLOGY,
                f"duplicate service name: {name}",
                "service_name_unique",
                service=name,
            )
        instance_id = _require_str(
            raw.get("instance_id"),
            code=E_BAD_TOPOLOGY,
            rule="instance_id",
            field=f"topology.services[{index}].instance_id",
        )
        if instance_id in instance_ids:
            raise _bad(
                E_BAD_TOPOLOGY,
                f"duplicate instance id: {instance_id}",
                "instance_id_unique",
                instance_id=instance_id,
            )
        instance_ids.add(instance_id)
        upstream_raw = raw.get("upstream", [])
        if not isinstance(upstream_raw, list):
            raise _bad(
                E_BAD_TOPOLOGY,
                f"upstream for {name} must be a list",
                "upstream_list",
                service=name,
            )
        upstream: List[str] = []
        for dep in upstream_raw:
            dep_name = _require_str(
                dep,
                code=E_BAD_TOPOLOGY,
                rule="upstream_name",
                field=f"{name}.upstream",
            )
            if dep_name == name:
                raise _bad(
                    E_BAD_TOPOLOGY,
                    f"service {name} cannot depend on itself",
                    "upstream_self_reference",
                    service=name,
                )
            upstream.append(dep_name)
        if len(set(upstream)) != len(upstream):
            raise _bad(
                E_BAD_TOPOLOGY,
                f"duplicate upstream dependency for {name}",
                "upstream_unique",
                service=name,
            )
        priority = _require_int(
            raw.get("priority"),
            code=E_BAD_TOPOLOGY,
            rule="priority_positive",
            field=f"topology.services[{index}].priority",
            minimum=1,
        )
        fault_domain = _require_str(
            raw.get("fault_domain"),
            code=E_BAD_TOPOLOGY,
            rule="fault_domain",
            field=f"topology.services[{index}].fault_domain",
        )
        services[name] = ServiceNode(
            name=name,
            instance_id=instance_id,
            upstream=tuple(upstream),
            priority=priority,
            fault_domain=fault_domain,
        )
    if not services:
        raise _bad(E_BAD_TOPOLOGY, "topology must contain services", "topology_nonempty")

    for service in services.values():
        for dep in service.upstream:
            if dep not in services:
                raise _bad(
                    E_BAD_TOPOLOGY,
                    f"service {service.name} depends on unknown service {dep}",
                    "upstream_closed",
                    service=service.name,
                    upstream=dep,
                )

    visiting: List[str] = []
    visited = set()

    def visit(name: str) -> None:
        if name in visited:
            return
        if name in visiting:
            cycle_start = visiting.index(name)
            cycle = visiting[cycle_start:] + [name]
            raise _bad(
                E_BAD_TOPOLOGY,
                f"topology cycle detected: {' -> '.join(cycle)}",
                "topology_acyclic",
                cycle=cycle,
            )
        visiting.append(name)
        for dep in services[name].upstream:
            visit(dep)
        visiting.pop()
        visited.add(name)

    for service_name in sorted(services):
        visit(service_name)

    return services, tuple(sorted(services))


def _parse_circuit(value: Any) -> CircuitPolicy:
    if not isinstance(value, dict):
        raise _bad(E_BAD_POLICY, "circuit_breaker must be an object", "circuit_object")
    window = _require_int(
        value.get("window_length_ms", value.get("window_length")),
        code=E_BAD_POLICY,
        rule="window_length_positive",
        field="circuit_breaker.window_length_ms",
        minimum=1,
    )
    step = _require_int(
        value.get("step_ms", value.get("step")),
        code=E_BAD_POLICY,
        rule="step_positive",
        field="circuit_breaker.step_ms",
        minimum=1,
    )
    if step > window:
        raise _bad(
            E_BAD_POLICY,
            "circuit_breaker.step_ms must not exceed window_length_ms",
            "step_le_window",
            step_ms=step,
            window_length_ms=window,
        )
    threshold = _decimal(
        value.get(
            "failure_rate_threshold",
            value.get("failure_threshold"),
        ),
        code=E_BAD_POLICY,
        rule="failure_rate_threshold",
        field="circuit_breaker.failure_rate_threshold",
    )
    if not (Decimal("0") < threshold < Decimal("1")):
        raise _bad(
            E_BAD_POLICY,
            "failure_rate_threshold must be in the open interval (0, 1)",
            "failure_rate_open_interval",
            value=str(threshold),
        )
    threshold_micro = _scaled(threshold, 1_000_000)
    if not (0 < threshold_micro < 1_000_000):
        raise _bad(
            E_BAD_POLICY,
            "failure_rate_threshold is outside the representable open interval",
            "failure_rate_fixed_point_open_interval",
            value=str(threshold),
            scaled_micro=threshold_micro,
        )
    probe_count = _require_int(
        value.get(
            "half_open_probe_count",
            value.get("half_open_probes"),
        ),
        code=E_BAD_POLICY,
        rule="half_open_probe_count_positive",
        field="circuit_breaker.half_open_probe_count",
        minimum=1,
    )
    cooldown = _require_int(
        value.get("cooldown_ms", value.get("cooldown")),
        code=E_BAD_POLICY,
        rule="cooldown_nonnegative",
        field="circuit_breaker.cooldown_ms",
        minimum=0,
    )
    return CircuitPolicy(
        window_length_ms=window,
        step_ms=step,
        failure_rate_threshold_micro=threshold_micro,
        half_open_probe_count=probe_count,
        cooldown_ms=cooldown,
    )


def _parse_degradation(value: Any) -> DegradationPolicy:
    if not isinstance(value, dict):
        raise _bad(E_BAD_POLICY, "degradation must be an object", "degradation_object")
    levels_raw = value.get("levels")
    if not isinstance(levels_raw, list) or not levels_raw:
        raise _bad(
            E_BAD_POLICY,
            "degradation.levels must be a non-empty list",
            "degradation_levels",
        )
    levels: List[DegradationLevel] = []
    names = set()
    previous = Decimal("-1")
    for index, raw in enumerate(levels_raw):
        if not isinstance(raw, dict):
            raise _bad(
                E_BAD_POLICY,
                f"degradation.levels[{index}] must be an object",
                "degradation_level_object",
                index=index,
            )
        name = _require_str(
            raw.get("name"),
            code=E_BAD_POLICY,
            rule="degradation_level_name",
            field=f"degradation.levels[{index}].name",
        )
        if name in names:
            raise _bad(
                E_BAD_POLICY,
                f"duplicate degradation level: {name}",
                "degradation_level_unique",
                level=name,
            )
        names.add(name)
        ratio = _decimal(
            raw.get("ratio"),
            code=E_BAD_POLICY,
            rule="degradation_ratio",
            field=f"degradation.levels[{index}].ratio",
        )
        if not (Decimal("0") < ratio <= Decimal("1")):
            raise _bad(
                E_BAD_POLICY,
                "degradation ratios must be in (0, 1]",
                "degradation_ratio_range",
                level=name,
                ratio=str(ratio),
            )
        if ratio <= previous:
            raise _bad(
                E_BAD_POLICY,
                "degradation ratios must be strictly increasing",
                "degradation_ratio_strictly_increasing",
                level=name,
                ratio=str(ratio),
                previous=str(previous),
            )
        previous = ratio
        levels.append(DegradationLevel(name=name, ratio_bps=_scaled(ratio, 10_000)))
    if previous != Decimal("1"):
        raise _bad(
            E_BAD_POLICY,
            "degradation ratio sequence must end at 1.0",
            "degradation_ratio_ends_at_one",
            last_ratio=str(previous),
        )
    for left, right in zip(levels, levels[1:]):
        if right.ratio_bps <= left.ratio_bps:
            raise _bad(
                E_BAD_POLICY,
                "degradation ratios are indistinguishable at 0.0001 resolution",
                "degradation_ratio_bps_unique",
                left=left.to_dict(),
                right=right.to_dict(),
            )

    initial_name = _require_str(
        value.get("initial", levels[-1].name),
        code=E_BAD_POLICY,
        rule="degradation_initial",
        field="degradation.initial",
    )
    name_to_index = {level.name: index for index, level in enumerate(levels)}
    if initial_name not in name_to_index:
        raise _bad(
            E_BAD_POLICY,
            f"unknown initial degradation level: {initial_name}",
            "degradation_initial_known",
            initial=initial_name,
        )

    ladder_raw = value.get("recovery_ladder")
    if ladder_raw is None:
        ladder_raw = [level.name for level in levels]
    if not isinstance(ladder_raw, list) or not ladder_raw:
        raise _bad(
            E_BAD_POLICY,
            "degradation.recovery_ladder must be a non-empty list",
            "recovery_ladder_list",
        )
    ladder: List[str] = []
    for item in ladder_raw:
        rung = _require_str(
            item,
            code=E_BAD_POLICY,
            rule="recovery_rung_name",
            field="degradation.recovery_ladder",
        )
        if rung not in name_to_index:
            raise _bad(
                E_BAD_POLICY,
                f"unknown recovery rung: {rung}",
                "recovery_rung_known",
                rung=rung,
            )
        if ladder and name_to_index[rung] <= name_to_index[ladder[-1]]:
            raise _bad(
                E_BAD_POLICY,
                "recovery ladder must have strictly increasing ratios",
                "recovery_ladder_increasing",
                previous=ladder[-1],
                current=rung,
            )
        ladder.append(rung)
    if ladder[-1] != levels[-1].name:
        raise _bad(
            E_BAD_POLICY,
            "recovery ladder must end at the 1.0 level",
            "recovery_ladder_ends_at_normal",
            last=ladder[-1],
        )
    recovery_successes = _require_int(
        value.get("recovery_successes_per_step", 1),
        code=E_BAD_POLICY,
        rule="recovery_successes_positive",
        field="degradation.recovery_successes_per_step",
        minimum=1,
    )
    return DegradationPolicy(
        levels=tuple(levels),
        initial_index=name_to_index[initial_name],
        recovery_ladder=tuple(ladder),
        recovery_successes_per_step=recovery_successes,
    )


def _parse_global_error_budget(value: Any) -> Optional[GlobalErrorBudgetPolicy]:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise _bad(
            E_BAD_POLICY,
            "global_error_budget must be an object",
            "global_error_budget_object",
        )
    max_failures = _require_int(
        value.get("max_failures"),
        code=E_BAD_POLICY,
        rule="global_error_budget_failures_positive",
        field="global_error_budget.max_failures",
        minimum=1,
    )
    window = _require_int(
        value.get("window_ms"),
        code=E_BAD_POLICY,
        rule="global_error_budget_window_positive",
        field="global_error_budget.window_ms",
        minimum=1,
    )
    return GlobalErrorBudgetPolicy(max_failures=max_failures, window_ms=window)


def parse_policy(raw: Any) -> Policy:
    if not isinstance(raw, dict):
        raise _bad(E_BAD_POLICY, "config must be a JSON object", "config_object")
    schema_version = _require_int(
        raw.get("schema_version", 1),
        code=E_BAD_POLICY,
        rule="schema_version",
        field="schema_version",
        minimum=1,
    )
    policy_version = _require_str(
        raw.get("policy_version", raw.get("version", "1")),
        code=E_BAD_POLICY,
        rule="policy_version",
        field="policy_version",
    )
    services, service_order = _parse_topology(raw.get("topology"))

    rate_limit = raw.get("rate_limit")
    if not isinstance(rate_limit, dict):
        raise _bad(E_BAD_POLICY, "rate_limit must be an object", "rate_limit_object")
    service_bucket_raw = rate_limit.get("services", rate_limit.get("service"))
    if not isinstance(service_bucket_raw, dict):
        raise _bad(
            E_BAD_POLICY,
            "rate_limit.services must be an object keyed by service",
            "service_buckets_object",
        )
    unknown_bucket_services = sorted(set(service_bucket_raw) - set(services))
    missing_bucket_services = sorted(set(services) - set(service_bucket_raw))
    if unknown_bucket_services or missing_bucket_services:
        raise _bad(
            E_BAD_POLICY,
            "service bucket policies must match topology services exactly",
            "service_buckets_match_topology",
            unknown=unknown_bucket_services,
            missing=missing_bucket_services,
        )
    service_buckets = {
        name: _parse_bucket(service_bucket_raw[name], f"rate_limit.services.{name}")
        for name in sorted(services)
    }
    global_bucket = _parse_bucket(rate_limit.get("global"), "rate_limit.global")
    clock_tolerance = _require_int(
        rate_limit.get("clock_rollback_tolerance_ms", 0),
        code=E_BAD_POLICY,
        rule="clock_rollback_tolerance_nonnegative",
        field="rate_limit.clock_rollback_tolerance_ms",
        minimum=0,
    )

    circuit = _parse_circuit(raw.get("circuit_breaker"))
    degradation = _parse_degradation(raw.get("degradation"))
    global_error_budget = _parse_global_error_budget(raw.get("global_error_budget"))

    return Policy(
        schema_version=schema_version,
        policy_version=policy_version,
        services=services,
        service_order=service_order,
        service_buckets=service_buckets,
        global_bucket=global_bucket,
        clock_rollback_tolerance_ms=clock_tolerance,
        circuit=circuit,
        degradation=degradation,
        global_error_budget=global_error_budget,
        raw=raw,
    )


def _parse_result(value: Any) -> Tuple[bool, str]:
    if isinstance(value, bool):
        return value, "success" if value else "failure"
    if isinstance(value, str) and value in RESULT_SUCCESS:
        return True, "success"
    if isinstance(value, str) and value in RESULT_FAILURE:
        return False, "failure"
    raise _bad(
        E_BAD_EVENT,
        "event result must be a success or failure marker",
        "event_result_marker",
        value=value,
    )


def _parse_event(raw: Any, index: int, services: Mapping[str, ServiceNode]) -> Event:
    if not isinstance(raw, dict):
        raise _bad(E_BAD_EVENT, f"events[{index}] must be an object", "event_object", index=index)
    seq_value = raw.get("seq", raw.get("event_seq", raw.get("request_seq")))
    seq = _require_int(
        seq_value,
        code=E_BAD_EVENT,
        rule="event_seq_positive",
        field=f"events[{index}].seq",
        minimum=1,
    )
    service = _require_str(
        raw.get("service"),
        code=E_BAD_EVENT,
        rule="event_service",
        field=f"events[{index}].service",
    )
    if service not in services:
        raise _bad(
            E_BAD_EVENT,
            f"event references unknown service: {service}",
            "event_service_known",
            event_seq=seq,
            service=service,
        )
    clock = _require_int(
        raw.get("clock_ms", raw.get("logical_clock_ms")),
        code=E_BAD_EVENT,
        rule="event_clock_nonnegative",
        field=f"events[{index}].clock_ms",
        minimum=0,
    )
    result_raw = raw.get("result", raw.get("outcome"))
    success, result = _parse_result(result_raw)
    latency = _require_int(
        raw.get("latency_us", raw.get("latency", 0)),
        code=E_BAD_EVENT,
        rule="event_latency_nonnegative",
        field=f"events[{index}].latency_us",
        minimum=0,
    )
    inject_raw = raw.get("inject", [])
    if inject_raw is None:
        inject_raw = []
    if isinstance(inject_raw, dict):
        inject_raw = [inject_raw]
    if not isinstance(inject_raw, list):
        raise _bad(
            E_BAD_EVENT,
            "event.inject must be a list",
            "event_inject_list",
            event_seq=seq,
        )
    inject: List[Dict[str, Any]] = []
    for item in inject_raw:
        if not isinstance(item, dict):
            raise _bad(
                E_BAD_EVENT,
                "event.inject entries must be objects",
                "event_inject_object",
                event_seq=seq,
            )
        inject.append(dict(item))
    return Event(
        seq=seq,
        service=service,
        clock_ms=clock,
        success=success,
        result=result,
        latency_us=latency,
        inject=tuple(inject),
        raw=dict(raw),
    )


def parse_events(raw: Any, policy: Policy) -> Tuple[Event, ...]:
    values = _extract_list(raw, "events", E_BAD_EVENT)
    events: List[Event] = []
    seen = set()
    for index, item in enumerate(values):
        event = _parse_event(item, index, policy.services)
        if event.seq in seen:
            raise _bad(
                E_BAD_EVENT,
                f"duplicate event sequence: {event.seq}",
                "event_seq_unique",
                event_seq=event.seq,
            )
        seen.add(event.seq)
        events.append(event)
    if not events:
        raise _bad(E_BAD_EVENT, "event stream must not be empty", "events_nonempty")
    events.sort(key=lambda item: item.seq)
    return tuple(events)


def _parse_anomaly(
    raw: Any,
    index: int,
    events_by_seq: Mapping[int, Event],
    default_seq: Optional[int] = None,
) -> Anomaly:
    if not isinstance(raw, dict):
        raise _bad(
            E_BAD_ANOMALY,
            f"anomalies[{index}] must be an object",
            "anomaly_object",
            index=index,
        )
    event_seq_value = raw.get("event_seq", raw.get("seq", default_seq))
    event_seq = _require_int(
        event_seq_value,
        code=E_BAD_ANOMALY,
        rule="anomaly_event_seq",
        field=f"anomalies[{index}].event_seq",
        minimum=1,
    )
    if event_seq not in events_by_seq:
        raise _bad(
            E_BAD_ANOMALY,
            f"anomaly references unknown event sequence: {event_seq}",
            "anomaly_event_known",
            event_seq=event_seq,
        )
    service_raw = raw.get("service", events_by_seq[event_seq].service)
    service = _require_str(
        service_raw,
        code=E_BAD_ANOMALY,
        rule="anomaly_service",
        field=f"anomalies[{index}].service",
    )
    if service != events_by_seq[event_seq].service:
        raise _bad(
            E_BAD_ANOMALY,
            "anomaly service must match its event service",
            "anomaly_service_matches_event",
            event_seq=event_seq,
            service=service,
            event_service=events_by_seq[event_seq].service,
        )
    stage = _require_str(
        raw.get("stage"),
        code=E_BAD_ANOMALY,
        rule="anomaly_stage",
        field=f"anomalies[{index}].stage",
    )
    if stage not in VALID_STAGES:
        raise _bad(
            E_BAD_ANOMALY,
            f"unknown anomaly stage: {stage}",
            "anomaly_stage_known",
            stage=stage,
        )
    kind = _require_str(
        raw.get("kind"),
        code=E_BAD_ANOMALY,
        rule="anomaly_kind",
        field=f"anomalies[{index}].kind",
    )
    code = RUNTIME_ANOMALY_CODES.get((stage, kind))
    if code is None:
        raise _bad(
            E_BAD_ANOMALY,
            f"unsupported anomaly kind {kind!r} for stage {stage!r}",
            "anomaly_kind_known",
            stage=stage,
            kind=kind,
        )
    details_raw = raw.get("details", {})
    if details_raw is None:
        details_raw = {}
    if not isinstance(details_raw, dict):
        raise _bad(
            E_BAD_ANOMALY,
            "anomaly details must be an object",
            "anomaly_details_object",
            event_seq=event_seq,
        )
    anomaly_id = str(
        raw.get(
            "anomaly_id",
            f"{event_seq}:{stage}:{kind}:{index}",
        )
    )
    return Anomaly(
        anomaly_id=anomaly_id,
        event_seq=event_seq,
        service=service,
        stage=stage,
        kind=kind,
        error_code=code,
        details=dict(details_raw),
    )


def parse_anomalies(
    raw: Any,
    events: Sequence[Event],
    event_injections: Optional[Sequence[Tuple[int, Dict[str, Any]]]] = None,
) -> Tuple[Anomaly, ...]:
    values = _extract_list(raw, "anomalies", E_BAD_ANOMALY)
    events_by_seq = {event.seq: event for event in events}
    anomalies: List[Anomaly] = []
    anomaly_ids = set()
    for index, item in enumerate(values):
        anomaly = _parse_anomaly(item, index, events_by_seq)
        if anomaly.anomaly_id in anomaly_ids:
            raise _bad(
                E_BAD_ANOMALY,
                f"duplicate anomaly id: {anomaly.anomaly_id}",
                "anomaly_id_unique",
                anomaly_id=anomaly.anomaly_id,
            )
        anomaly_ids.add(anomaly.anomaly_id)
        anomalies.append(anomaly)

    if event_injections:
        for event_seq, injection in event_injections:
            anomaly = _parse_anomaly(
                injection,
                len(anomalies),
                events_by_seq,
                default_seq=event_seq,
            )
            if anomaly.anomaly_id in anomaly_ids:
                raise _bad(
                    E_BAD_ANOMALY,
                    f"duplicate anomaly id: {anomaly.anomaly_id}",
                    "anomaly_id_unique",
                    anomaly_id=anomaly.anomaly_id,
                )
            anomaly_ids.add(anomaly.anomaly_id)
            anomalies.append(anomaly)
    anomalies.sort(key=lambda item: (item.event_seq, item.stage, item.anomaly_id))
    return tuple(anomalies)


def load_bundle_from_objects(
    config_raw: Any,
    events_raw: Any,
    anomalies_raw: Any = None,
) -> SampleBundle:
    policy = parse_policy(config_raw)
    events = parse_events(events_raw, policy)
    event_injections: List[Tuple[int, Dict[str, Any]]] = []
    for event in events:
        for injection in event.inject:
            event_injections.append((event.seq, injection))
    if anomalies_raw is None:
        anomalies_raw = []
    anomalies = parse_anomalies(anomalies_raw, events, event_injections)

    if isinstance(events_raw, dict):
        events_list = list(events_raw.get("events", []))
    else:
        events_list = list(events_raw)
    if isinstance(anomalies_raw, dict):
        anomalies_list = list(anomalies_raw.get("anomalies", []))
    else:
        anomalies_list = list(anomalies_raw)

    return SampleBundle(
        policy=policy,
        events=events,
        anomalies=anomalies,
        config_raw=dict(config_raw),
        events_raw=events_list,
        anomalies_raw=anomalies_list,
        config_sha256=sha256_json(config_raw),
        events_sha256=sha256_json(events_list),
        anomalies_sha256=sha256_json(anomalies_list),
    )


def load_bundle(
    config_path: str | Path,
    events_path: str | Path,
    anomalies_path: Optional[str | Path] = None,
) -> SampleBundle:
    config_raw = _read_json(Path(config_path), E_BAD_POLICY)
    events_raw = _read_json(Path(events_path), E_BAD_EVENT)
    anomalies_raw: Any = []
    if anomalies_path is not None:
        anomalies_raw = _read_json(Path(anomalies_path), E_BAD_ANOMALY)
    return load_bundle_from_objects(config_raw, events_raw, anomalies_raw)


def bounded_json(value: Any, limit: int = 200) -> str:
    text = canonical_json(value)
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."

