from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from urllib.parse import urlparse

from .errors import BusinessError, input_error


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require_int(value: Any, field: str, *, minimum: int | None = None) -> int:
    if not _is_int(value):
        raise input_error(f"{field} must be an integer", field=field, value=value)
    if minimum is not None and value < minimum:
        raise input_error(
            f"{field} must be >= {minimum}", field=field, value=value, minimum=minimum
        )
    return value


def _require_positive_int(value: Any, field: str) -> int:
    return _require_int(value, field, minimum=1)


def _require_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise input_error(f"{field} must be a non-empty string", field=field, value=value)
    return value.strip()


def _require_number(value: Any, field: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise input_error(f"{field} must be a finite number", field=field, value=value)
    number = float(value)
    if not math.isfinite(number) or number < 0 or (positive and number == 0):
        qualifier = "positive" if positive else "non-negative"
        raise input_error(
            f"{field} must be a finite {qualifier} number", field=field, value=value
        )
    return number


def _loopback_url(value: Any) -> str:
    text = _require_text(value, "launch_target_url")
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise input_error(
            "launch_target_url must be an HTTP(S) URL", value=text
        )
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise input_error(
            "launch_target_url must point to a loopback host",
            value=text,
            host=parsed.hostname,
        )
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise input_error(
            "launch_target_url must not contain credentials, query, or fragment",
            value=text,
        )
    return text.rstrip("/")


@dataclass(frozen=True)
class ServiceSpec:
    name: str
    model_version: str
    replica_memory_mib: int
    replica_compute_units: int
    tokens_per_second_per_compute_unit: float
    min_replicas: int
    max_replicas: int
    cold_start_ms: int
    batch_window_ms: int
    topology_domains: tuple[str, ...]
    priority: int

    @property
    def replica_capacity_tokens_per_second(self) -> Decimal:
        return Decimal(str(self.replica_compute_units)) * Decimal(
            str(self.tokens_per_second_per_compute_unit)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "model_version": self.model_version,
            "replica_memory_mib": self.replica_memory_mib,
            "replica_compute_units": self.replica_compute_units,
            "tokens_per_second_per_compute_unit": self.tokens_per_second_per_compute_unit,
            "min_replicas": self.min_replicas,
            "max_replicas": self.max_replicas,
            "cold_start_ms": self.cold_start_ms,
            "batch_window_ms": self.batch_window_ms,
            "topology_domains": list(self.topology_domains),
            "priority": self.priority,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ServiceSpec:
        topology = raw.get("topology_domains", [])
        if not isinstance(topology, list) or not topology:
            raise input_error(
                "topology_domains must be a non-empty list",
                service=raw.get("name"),
            )
        domains: list[str] = []
        for index, domain in enumerate(topology):
            text = _require_text(domain, f"topology_domains[{index}]")
            if text in domains:
                raise input_error(
                    "topology_domains contains a duplicate",
                    service=raw.get("name"),
                    domain=text,
                )
            domains.append(text)
        min_replicas = _require_int(raw.get("min_replicas"), "min_replicas", minimum=0)
        max_replicas = _require_int(raw.get("max_replicas"), "max_replicas", minimum=0)
        if min_replicas > max_replicas:
            raise input_error(
                "min_replicas must not exceed max_replicas",
                service=raw.get("name"),
                min_replicas=min_replicas,
                max_replicas=max_replicas,
            )
        return cls(
            name=_require_text(raw.get("name"), "service.name"),
            model_version=_require_text(raw.get("model_version"), "model_version"),
            replica_memory_mib=_require_positive_int(
                raw.get("replica_memory_mib"), "replica_memory_mib"
            ),
            replica_compute_units=_require_positive_int(
                raw.get("replica_compute_units"), "replica_compute_units"
            ),
            tokens_per_second_per_compute_unit=_require_number(
                raw.get("tokens_per_second_per_compute_unit"),
                "tokens_per_second_per_compute_unit",
                positive=True,
            ),
            min_replicas=min_replicas,
            max_replicas=max_replicas,
            cold_start_ms=_require_int(
                raw.get("cold_start_ms"), "cold_start_ms", minimum=0
            ),
            batch_window_ms=_require_positive_int(
                raw.get("batch_window_ms"), "batch_window_ms"
            ),
            topology_domains=tuple(sorted(domains)),
            priority=_require_int(raw.get("priority", 0), "priority"),
        )


@dataclass(frozen=True)
class NodeSpec:
    name: str
    gpu_count: int
    gpu_memory_mib: int
    max_instances: int
    topology_domain: str

    @property
    def total_memory_mib(self) -> int:
        return self.gpu_count * self.gpu_memory_mib

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "gpu_count": self.gpu_count,
            "gpu_memory_mib": self.gpu_memory_mib,
            "max_instances": self.max_instances,
            "topology_domain": self.topology_domain,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> NodeSpec:
        return cls(
            name=_require_text(raw.get("name"), "node.name"),
            gpu_count=_require_positive_int(raw.get("gpu_count"), "gpu_count"),
            gpu_memory_mib=_require_positive_int(
                raw.get("gpu_memory_mib"), "gpu_memory_mib"
            ),
            max_instances=_require_positive_int(
                raw.get("max_instances"), "max_instances"
            ),
            topology_domain=_require_text(
                raw.get("topology_domain"), "topology_domain"
            ),
        )


@dataclass(frozen=True)
class Observation:
    window_index: int
    logical_time_ms: int
    arrival_rate: float
    avg_sequence_length: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "window_index": self.window_index,
            "logical_time_ms": self.logical_time_ms,
            "arrival_rate": self.arrival_rate,
            "avg_sequence_length": self.avg_sequence_length,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any], index: int) -> Observation:
        return cls(
            window_index=index,
            logical_time_ms=_require_int(
                raw.get("logical_time_ms"), "logical_time_ms", minimum=0
            ),
            arrival_rate=_require_number(raw.get("arrival_rate"), "arrival_rate"),
            avg_sequence_length=_require_number(
                raw.get("avg_sequence_length"), "avg_sequence_length"
            ),
        )


@dataclass(frozen=True)
class WorkloadTrace:
    series: dict[str, tuple[Observation, ...]]

    def observation_at(self, clock_ms: int, service: str) -> Observation | None:
        windows = self.series.get(service) or self.series.get("*")
        if not windows:
            return None
        candidate = None
        for window in windows:
            if window.logical_time_ms <= clock_ms:
                candidate = window
            else:
                break
        return candidate

    def all_windows(self) -> tuple[Observation, ...]:
        return tuple(
            window
            for name in sorted(self.series)
            for window in self.series[name]
        )

    def first_clock_ms(self) -> int:
        starts = [windows[0].logical_time_ms for windows in self.series.values()]
        return min(starts)

    def to_dict(self) -> dict[str, Any]:
        if set(self.series) == {"*"}:
            return {"windows": [window.to_dict() for window in self.series["*"]]}
        return {
            "services": {
                service: [window.to_dict() for window in windows]
                for service, windows in sorted(self.series.items())
            }
        }


@dataclass(frozen=True)
class ScalingConfig:
    config_version: int
    round_budget_ms: int
    per_service_launch_limit: int
    backoff_base_ms: int
    backoff_max_ms: int
    failure_threshold: int
    jitter_mode: str
    jitter_percent: int
    clock_step_ms: int
    clock_regression_tolerance_ms: int
    drain_duration_ms: int
    health_probe_enabled: bool
    launch_target_url: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "config_version": self.config_version,
            "round_budget_ms": self.round_budget_ms,
            "per_service_launch_limit": self.per_service_launch_limit,
            "backoff_base_ms": self.backoff_base_ms,
            "backoff_max_ms": self.backoff_max_ms,
            "failure_threshold": self.failure_threshold,
            "jitter_rule": {
                "mode": self.jitter_mode,
                "percent": self.jitter_percent,
            },
            "clock_step_ms": self.clock_step_ms,
            "clock_regression_tolerance_ms": self.clock_regression_tolerance_ms,
            "drain_duration_ms": self.drain_duration_ms,
            "health_probe_enabled": self.health_probe_enabled,
            "launch_target_url": self.launch_target_url,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ScalingConfig:
        jitter = raw.get("jitter_rule", {})
        if not isinstance(jitter, dict):
            raise input_error("jitter_rule must be an object")
        mode = _require_text(jitter.get("mode", "deterministic"), "jitter_rule.mode")
        if mode not in {"none", "deterministic"}:
            raise input_error(
                "jitter_rule.mode must be 'none' or 'deterministic'", value=mode
            )
        percent = _require_int(
            jitter.get("percent", 0), "jitter_rule.percent", minimum=0
        )
        if percent > 100:
            raise input_error("jitter_rule.percent must be <= 100", value=percent)
        base = _require_positive_int(raw.get("backoff_base_ms"), "backoff_base_ms")
        maximum = _require_positive_int(raw.get("backoff_max_ms"), "backoff_max_ms")
        if maximum < base:
            raise input_error(
                "backoff_max_ms must be >= backoff_base_ms",
                backoff_base_ms=base,
                backoff_max_ms=maximum,
            )
        health = raw.get("health_probe_enabled", True)
        if not isinstance(health, bool):
            raise input_error("health_probe_enabled must be a boolean")
        return cls(
            config_version=_require_positive_int(
                raw.get("config_version"), "config_version"
            ),
            round_budget_ms=_require_positive_int(
                raw.get("round_budget_ms"), "round_budget_ms"
            ),
            per_service_launch_limit=_require_positive_int(
                raw.get("per_service_launch_limit"), "per_service_launch_limit"
            ),
            backoff_base_ms=base,
            backoff_max_ms=maximum,
            failure_threshold=_require_positive_int(
                raw.get("failure_threshold"), "failure_threshold"
            ),
            jitter_mode=mode,
            jitter_percent=percent,
            clock_step_ms=_require_positive_int(
                raw.get("clock_step_ms"), "clock_step_ms"
            ),
            clock_regression_tolerance_ms=_require_int(
                raw.get("clock_regression_tolerance_ms", 0),
                "clock_regression_tolerance_ms",
                minimum=0,
            ),
            drain_duration_ms=_require_int(
                raw.get("drain_duration_ms", 0),
                "drain_duration_ms",
                minimum=0,
            ),
            health_probe_enabled=health,
            launch_target_url=_loopback_url(raw.get("launch_target_url")),
        )


@dataclass(frozen=True)
class ScalerSpec:
    services: dict[str, ServiceSpec]
    nodes: dict[str, NodeSpec]
    trace: WorkloadTrace
    config: ScalingConfig

    def to_dict(self) -> dict[str, Any]:
        return {
            "services": [
                self.services[name].to_dict() for name in sorted(self.services)
            ],
            "nodes": [self.nodes[name].to_dict() for name in sorted(self.nodes)],
            "trace": self.trace.to_dict(),
            "config": self.config.to_dict(),
        }

    @property
    def digest(self) -> str:
        return hashlib.sha256(canonical_json(self.to_dict()).encode("utf-8")).hexdigest()

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ScalerSpec:
        services_raw = raw.get("services")
        nodes_raw = raw.get("nodes")
        if not isinstance(services_raw, list) or not services_raw:
            raise input_error("services must be a non-empty list")
        if not isinstance(nodes_raw, list) or not nodes_raw:
            raise input_error("nodes must be a non-empty list")

        services: dict[str, ServiceSpec] = {}
        for item in services_raw:
            if not isinstance(item, dict):
                raise input_error("each service must be an object")
            service = ServiceSpec.from_dict(item)
            if service.name in services:
                raise BusinessError(
                    "E_DUPLICATE_SERVICE",
                    f"service name '{service.name}' is duplicated",
                    {"service": service.name},
                )
            services[service.name] = service

        nodes: dict[str, NodeSpec] = {}
        for item in nodes_raw:
            if not isinstance(item, dict):
                raise input_error("each node must be an object")
            node = NodeSpec.from_dict(item)
            if node.name in nodes:
                raise BusinessError(
                    "E_DUPLICATE_NODE",
                    f"node name '{node.name}' is duplicated",
                    {"node": node.name},
                )
            nodes[node.name] = node

        trace_raw = raw.get("trace")
        if not isinstance(trace_raw, dict):
            raise input_error("trace must be an object")
        series_raw: dict[str, Any] = {}
        if "services" in trace_raw:
            if not isinstance(trace_raw["services"], dict) or not trace_raw["services"]:
                raise input_error("trace.services must be a non-empty object")
            series_raw = trace_raw["services"]
        elif "windows" in trace_raw:
            series_raw = {"*": trace_raw["windows"]}
        else:
            raise input_error("trace must contain either windows or services")
        series: dict[str, tuple[Observation, ...]] = {}
        for service_name in sorted(series_raw):
            service_series_name = _require_text(service_name, "trace.service")
            raw_windows = series_raw[service_name]
            if not isinstance(raw_windows, list) or not raw_windows:
                raise input_error(
                    "each trace series must be a non-empty list",
                    service=service_series_name,
                )
            windows: list[Observation] = []
            seen_times: set[int] = set()
            previous_time = -1
            for index, item in enumerate(raw_windows):
                if not isinstance(item, dict):
                    raise input_error(
                        f"trace service '{service_series_name}' window {index} must be an object"
                    )
                observation = Observation.from_dict(item, index)
                if observation.logical_time_ms in seen_times:
                    raise BusinessError(
                        "E_DUPLICATE_WINDOW_TIME",
                        "workload window logical_time_ms is duplicated within a service",
                        {
                            "service": service_series_name,
                            "logical_time_ms": observation.logical_time_ms,
                        },
                    )
                if observation.logical_time_ms <= previous_time:
                    raise input_error(
                        "trace windows must be strictly increasing by logical_time_ms",
                        service=service_series_name,
                        window_index=index,
                    )
                seen_times.add(observation.logical_time_ms)
                previous_time = observation.logical_time_ms
                windows.append(observation)
            series[service_series_name] = tuple(windows)
        if "*" not in series:
            missing = sorted(set(services) - set(series))
            if missing:
                raise input_error(
                    "trace is missing service series",
                    services=missing,
                )

        config_raw = raw.get("config")
        if not isinstance(config_raw, dict):
            raise input_error("config must be an object")
        config = ScalingConfig.from_dict(config_raw)
        return cls(
            services=services,
            nodes=nodes,
            trace=WorkloadTrace(series),
            config=config,
        )


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def pretty_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        indent=2,
        ensure_ascii=False,
        allow_nan=False,
    )
