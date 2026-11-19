from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .errors import BusinessError, ErrorCode


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


def load_json(path: str | Path) -> Any:
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            return json.load(handle, parse_constant=_reject_constant)
    except json.JSONDecodeError as exc:
        raise BusinessError(
            ErrorCode.INPUT_JSON,
            "invalid JSON",
            details={"path": str(path), "line": exc.lineno, "column": exc.colno},
        ) from exc
    except OSError as exc:
        raise BusinessError(
            ErrorCode.INPUT_JSON,
            "cannot read JSON input",
            details={"path": str(path), "reason": str(exc)},
        ) from exc
    except ValueError as exc:
        raise BusinessError(
            ErrorCode.INPUT_JSON,
            "invalid JSON number",
            details={"path": str(path), "reason": str(exc)},
        ) from exc


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def digest_value(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _as_mapping(value: Any, code: ErrorCode, message: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BusinessError(code, message)
    return value


def _as_list(value: Any, code: ErrorCode, message: str) -> list[Any]:
    if not isinstance(value, list):
        raise BusinessError(code, message)
    return value


def _required(mapping: dict[str, Any], key: str, code: ErrorCode, context: str) -> Any:
    if key not in mapping:
        raise BusinessError(code, f"{context}: missing field {key}")
    return mapping[key]


def _nonempty_string(value: Any, code: ErrorCode, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BusinessError(code, f"{field_name} must be a non-empty string")
    return value.strip()


def _integer(value: Any, code: ErrorCode, field_name: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BusinessError(code, f"{field_name} must be an integer")
    if value < minimum:
        raise BusinessError(code, f"{field_name} must be >= {minimum}")
    return value


def _finite_number(value: Any, code: ErrorCode, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BusinessError(code, f"{field_name} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise BusinessError(code, f"{field_name} must be finite")
    return number


@dataclass(frozen=True)
class ServiceSpec:
    name: str
    model_version: str
    memory_mib_per_replica: int
    compute_units_per_replica: int
    min_replicas: int
    max_replicas: int
    cold_start_ticks: int
    batch_window_ms: int
    throughput_tokens_per_tick: float
    throughput_source: str
    max_concurrent_launches: int
    allowed_topology_domains: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["allowed_topology_domains"] = list(self.allowed_topology_domains)
        return result


@dataclass(frozen=True)
class NodeSpec:
    name: str
    gpu_count: int
    memory_mib_per_gpu: int
    max_instances_per_node: int
    topology_domain: str


@dataclass(frozen=True)
class NodePool:
    nodes: tuple[NodeSpec, ...]
    compute_units_capacity: int | None
    max_replicas_per_domain_per_service: int | None

    def node_map(self) -> dict[str, NodeSpec]:
        return {node.name: node for node in self.nodes}


@dataclass(frozen=True)
class WorkloadWindow:
    tick: int
    arrival_rate: float
    avg_sequence_length: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class WorkloadTrace:
    trace_id: str
    windows: tuple[WorkloadWindow, ...]
    window_by_tick: dict[int, WorkloadWindow] = field(compare=False)


@dataclass(frozen=True)
class InputBundle:
    services: tuple[ServiceSpec, ...]
    node_pool: NodePool
    trace: WorkloadTrace
    sample_digest: str
    raw_services: dict[str, Any]
    raw_nodes: dict[str, Any]
    raw_trace: dict[str, Any]

    def service_map(self) -> dict[str, ServiceSpec]:
        return {service.name: service for service in self.services}

    def summary(self) -> dict[str, Any]:
        return {
            "sample_digest": self.sample_digest,
            "services": [service.to_dict() for service in self.services],
            "nodes": [asdict(node) for node in self.node_pool.nodes],
            "compute_units_capacity": self.node_pool.compute_units_capacity,
            "max_replicas_per_domain_per_service": (
                self.node_pool.max_replicas_per_domain_per_service
            ),
            "trace_id": self.trace.trace_id,
            "windows": [window.to_dict() for window in self.trace.windows],
        }


def _parse_services(raw: Any) -> tuple[ServiceSpec, ...]:
    if isinstance(raw, list):
        items = raw
    else:
        mapping = _as_mapping(raw, ErrorCode.INPUT_SCHEMA, "services input must be an object")
        items = _as_list(
            _required(mapping, "services", ErrorCode.INPUT_SCHEMA, "services"),
            ErrorCode.INPUT_SCHEMA,
            "services must be a list",
        )
    if not items:
        raise BusinessError(ErrorCode.INPUT_SCHEMA, "services must not be empty")

    services: list[ServiceSpec] = []
    seen: set[str] = set()
    for index, item in enumerate(items):
        item = _as_mapping(item, ErrorCode.INPUT_SCHEMA, f"services[{index}] must be an object")
        name = _nonempty_string(
            _required(item, "name", ErrorCode.INVALID_SERVICE_NAME, "service"),
            ErrorCode.INVALID_SERVICE_NAME,
            "service name",
        )
        if name in seen:
            raise BusinessError(
                ErrorCode.DUPLICATE_SERVICE,
                "service names must be unique",
                details={"service": name},
            )
        seen.add(name)

        model_version = _nonempty_string(
            _required(item, "model_version", ErrorCode.INVALID_MODEL_VERSION, name),
            ErrorCode.INVALID_MODEL_VERSION,
            f"{name}.model_version",
        )
        memory = _integer(
            _required(item, "memory_mib_per_replica", ErrorCode.INVALID_MEMORY, name),
            ErrorCode.INVALID_MEMORY,
            f"{name}.memory_mib_per_replica",
            1,
        )
        compute = _integer(
            _required(item, "compute_units_per_replica", ErrorCode.INVALID_COMPUTE, name),
            ErrorCode.INVALID_COMPUTE,
            f"{name}.compute_units_per_replica",
            1,
        )
        min_replicas = _integer(
            _required(item, "min_replicas", ErrorCode.INVALID_REPLICA_BOUNDS, name),
            ErrorCode.INVALID_REPLICA_BOUNDS,
            f"{name}.min_replicas",
            0,
        )
        max_replicas = _integer(
            _required(item, "max_replicas", ErrorCode.INVALID_REPLICA_BOUNDS, name),
            ErrorCode.INVALID_REPLICA_BOUNDS,
            f"{name}.max_replicas",
            0,
        )
        if min_replicas > max_replicas:
            raise BusinessError(
                ErrorCode.INVALID_REPLICA_BOUNDS,
                "min_replicas must not exceed max_replicas",
                details={"service": name, "min_replicas": min_replicas, "max_replicas": max_replicas},
            )
        cold_start = _integer(
            _required(item, "cold_start_ticks", ErrorCode.INVALID_COLD_START, name),
            ErrorCode.INVALID_COLD_START,
            f"{name}.cold_start_ticks",
            0,
        )
        batch_window = _integer(
            _required(item, "batch_window_ms", ErrorCode.INVALID_BATCH_WINDOW, name),
            ErrorCode.INVALID_BATCH_WINDOW,
            f"{name}.batch_window_ms",
            1,
        )

        throughput_source = "explicit"
        throughput_raw = item.get("throughput_tokens_per_tick")
        if throughput_raw is None:
            throughput_raw = item.get("tokens_per_tick_per_replica")
        if throughput_raw is None:
            throughput_raw = compute
            throughput_source = "compute_units_per_replica"
        throughput = _finite_number(
            throughput_raw,
            ErrorCode.INVALID_THROUGHPUT,
            f"{name}.throughput_tokens_per_tick",
        )
        if throughput <= 0:
            raise BusinessError(
                ErrorCode.INVALID_THROUGHPUT,
                f"{name}.throughput_tokens_per_tick must be > 0",
            )

        max_concurrency = _integer(
            item.get("max_concurrent_launches", 1),
            ErrorCode.INVALID_CONCURRENCY_LIMIT,
            f"{name}.max_concurrent_launches",
            1,
        )

        domains_value = item.get("allowed_topology_domains", [])
        domains = _as_list(
            domains_value,
            ErrorCode.INVALID_TOPOLOGY,
            f"{name}.allowed_topology_domains must be a list",
        )
        normalized_domains: list[str] = []
        domain_seen: set[str] = set()
        for domain_value in domains:
            domain = _nonempty_string(
                domain_value,
                ErrorCode.INVALID_TOPOLOGY,
                f"{name}.allowed_topology_domains[]",
            )
            if domain in domain_seen:
                raise BusinessError(
                    ErrorCode.INVALID_TOPOLOGY,
                    "allowed topology domains must be unique",
                    details={"service": name, "domain": domain},
                )
            domain_seen.add(domain)
            normalized_domains.append(domain)

        services.append(
            ServiceSpec(
                name=name,
                model_version=model_version,
                memory_mib_per_replica=memory,
                compute_units_per_replica=compute,
                min_replicas=min_replicas,
                max_replicas=max_replicas,
                cold_start_ticks=cold_start,
                batch_window_ms=batch_window,
                throughput_tokens_per_tick=throughput,
                throughput_source=throughput_source,
                max_concurrent_launches=max_concurrency,
                allowed_topology_domains=tuple(normalized_domains),
            )
        )
    return tuple(services)


def _parse_nodes(raw: Any) -> NodePool:
    mapping = _as_mapping(raw, ErrorCode.INPUT_SCHEMA, "nodes input must be an object")
    items = _as_list(
        _required(mapping, "nodes", ErrorCode.INPUT_SCHEMA, "nodes"),
        ErrorCode.INPUT_SCHEMA,
        "nodes must be a list",
    )
    if not items:
        raise BusinessError(ErrorCode.INPUT_SCHEMA, "nodes must not be empty")

    nodes: list[NodeSpec] = []
    seen: set[str] = set()
    for index, item in enumerate(items):
        item = _as_mapping(item, ErrorCode.INPUT_SCHEMA, f"nodes[{index}] must be an object")
        name = _nonempty_string(
            _required(item, "name", ErrorCode.INVALID_NODE_NAME, "node"),
            ErrorCode.INVALID_NODE_NAME,
            "node name",
        )
        if name in seen:
            raise BusinessError(
                ErrorCode.DUPLICATE_NODE,
                "node names must be unique",
                details={"node": name},
            )
        seen.add(name)

        gpu_count = _integer(
            _required(item, "gpu_count", ErrorCode.INVALID_GPU_COUNT, name),
            ErrorCode.INVALID_GPU_COUNT,
            f"{name}.gpu_count",
            1,
        )
        gpu_memory = _integer(
            _required(item, "memory_mib_per_gpu", ErrorCode.INVALID_GPU_MEMORY, name),
            ErrorCode.INVALID_GPU_MEMORY,
            f"{name}.memory_mib_per_gpu",
            1,
        )
        max_instances = _integer(
            _required(item, "max_instances_per_node", ErrorCode.INVALID_NODE_CAPACITY, name),
            ErrorCode.INVALID_NODE_CAPACITY,
            f"{name}.max_instances_per_node",
            1,
        )
        domain = _nonempty_string(
            _required(item, "topology_domain", ErrorCode.INVALID_TOPOLOGY, name),
            ErrorCode.INVALID_TOPOLOGY,
            f"{name}.topology_domain",
        )
        nodes.append(
            NodeSpec(
                name=name,
                gpu_count=gpu_count,
                memory_mib_per_gpu=gpu_memory,
                max_instances_per_node=max_instances,
                topology_domain=domain,
            )
        )

    compute_capacity: int | None = None
    if mapping.get("compute_units_capacity") is not None:
        compute_capacity = _integer(
            mapping["compute_units_capacity"],
            ErrorCode.INVALID_COMPUTE_CAPACITY,
            "compute_units_capacity",
            1,
        )

    domain_limit: int | None = None
    if mapping.get("max_replicas_per_domain_per_service") is not None:
        domain_limit = _integer(
            mapping["max_replicas_per_domain_per_service"],
            ErrorCode.INVALID_TOPOLOGY,
            "max_replicas_per_domain_per_service",
            1,
        )

    return NodePool(
        nodes=tuple(nodes),
        compute_units_capacity=compute_capacity,
        max_replicas_per_domain_per_service=domain_limit,
    )


def _parse_trace(raw: Any) -> WorkloadTrace:
    mapping = _as_mapping(raw, ErrorCode.INPUT_SCHEMA, "trace input must be an object")
    trace_id = _nonempty_string(
        mapping.get("trace_id", mapping.get("name", "default-trace")),
        ErrorCode.INVALID_TRACE,
        "trace_id",
    )
    items = _as_list(
        _required(mapping, "windows", ErrorCode.INVALID_TRACE, "trace"),
        ErrorCode.INVALID_TRACE,
        "trace.windows must be a list",
    )
    if not items:
        raise BusinessError(ErrorCode.INVALID_TRACE, "trace.windows must not be empty")

    windows: list[WorkloadWindow] = []
    by_tick: dict[int, WorkloadWindow] = {}
    previous_tick = -1
    for index, item in enumerate(items):
        item = _as_mapping(item, ErrorCode.INVALID_TRACE, f"windows[{index}] must be an object")
        tick_value = item.get("tick", item.get("logical_tick", item.get("window")))
        if tick_value is None:
            raise BusinessError(
                ErrorCode.INVALID_TRACE_TICK,
                "trace window is missing tick",
                details={"index": index},
            )
        tick = _integer(tick_value, ErrorCode.INVALID_TRACE_TICK, "trace window tick", 0)
        if tick in by_tick:
            raise BusinessError(
                ErrorCode.INVALID_TRACE_TICK,
                "trace window ticks must be unique",
                details={"tick": tick},
            )
        if tick <= previous_tick:
            raise BusinessError(
                ErrorCode.INVALID_TRACE_TICK,
                "trace window ticks must be strictly increasing",
                details={"tick": tick, "previous_tick": previous_tick},
            )
        previous_tick = tick

        arrival = _finite_number(
            _required(item, "arrival_rate", ErrorCode.INVALID_ARRIVAL_RATE, f"window {tick}"),
            ErrorCode.INVALID_ARRIVAL_RATE,
            f"window {tick}.arrival_rate",
        )
        if arrival < 0:
            raise BusinessError(
                ErrorCode.INVALID_ARRIVAL_RATE,
                "arrival_rate must be >= 0",
                details={"tick": tick, "arrival_rate": arrival},
            )
        sequence_length = _finite_number(
            _required(
                item,
                "avg_sequence_length",
                ErrorCode.INVALID_SEQUENCE_LENGTH,
                f"window {tick}",
            ),
            ErrorCode.INVALID_SEQUENCE_LENGTH,
            f"window {tick}.avg_sequence_length",
        )
        if sequence_length < 0:
            raise BusinessError(
                ErrorCode.INVALID_SEQUENCE_LENGTH,
                "avg_sequence_length must be >= 0",
                details={"tick": tick, "avg_sequence_length": sequence_length},
            )

        window = WorkloadWindow(
            tick=tick,
            arrival_rate=arrival,
            avg_sequence_length=sequence_length,
        )
        windows.append(window)
        by_tick[tick] = window

    return WorkloadTrace(trace_id=trace_id, windows=tuple(windows), window_by_tick=by_tick)


def build_input_bundle(raw_services: Any, raw_nodes: Any, raw_trace: Any) -> InputBundle:
    services = _parse_services(raw_services)
    node_pool = _parse_nodes(raw_nodes)
    trace = _parse_trace(raw_trace)
    digest_payload = {
        "services": raw_services,
        "nodes": raw_nodes,
        "trace": raw_trace,
    }
    sample_digest = digest_value(digest_payload)
    return InputBundle(
        services=services,
        node_pool=node_pool,
        trace=trace,
        sample_digest=sample_digest,
        raw_services=raw_services,
        raw_nodes=raw_nodes,
        raw_trace=raw_trace,
    )


def load_input_bundle(
    services_path: str | Path,
    nodes_path: str | Path,
    trace_path: str | Path,
) -> InputBundle:
    raw_services = load_json(services_path)
    raw_nodes = load_json(nodes_path)
    raw_trace = load_json(trace_path)
    return build_input_bundle(raw_services, raw_nodes, raw_trace)


def validate_uniqueness(items: Iterable[str], code: ErrorCode, kind: str) -> None:
    seen: set[str] = set()
    for item in items:
        if item in seen:
            raise BusinessError(code, f"{kind} names must be unique", details={kind: item})
        seen.add(item)
