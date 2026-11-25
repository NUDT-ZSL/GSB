"""Input parsing and deterministic domain model."""

from __future__ import annotations

import ipaddress
import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

from .errors import EngineError, ErrorCode


DEFAULT_MAX_CONCURRENCY = 2
DEFAULT_TIME_BUDGET_MS = 20


@dataclass(frozen=True)
class ShardSpec:
    index: int
    byte_size: int

    def to_dict(self) -> dict[str, Any]:
        return {"index": self.index, "byte_size": self.byte_size}


@dataclass(frozen=True)
class LayerSpec:
    name: str
    parameter_bytes: int
    shards: tuple[ShardSpec, ...]

    def shard_bytes(self, index: int) -> int:
        for shard in self.shards:
            if shard.index == index:
                return shard.byte_size
        raise KeyError(index)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "parameter_bytes": self.parameter_bytes,
            "shards": [shard.to_dict() for shard in self.shards],
        }


@dataclass(frozen=True)
class SpecView:
    version: int
    method: str
    layers: tuple[LayerSpec, ...]

    def layer(self, name: str) -> LayerSpec:
        for layer in self.layers:
            if layer.name == name:
                return layer
        raise KeyError(name)

    def has_shard(self, layer_name: str, shard_index: int) -> bool:
        try:
            self.layer(layer_name).shard_bytes(shard_index)
            return True
        except KeyError:
            return False

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "method": self.method,
            "layers": [layer.to_dict() for layer in self.layers],
        }


@dataclass(frozen=True)
class NodeInfo:
    node_id: str
    address: str
    compute_ratio: float
    memory_limit_bytes: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class GradientEvent:
    event_seq: int
    logic_clock_ms: int
    node_id: str
    layer: str
    shard_index: int
    spec_version: int
    expected_commit_ms: int
    duration_ms: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Injection:
    order_index: int
    logic_clock_ms: int
    kind: str
    injection_id: str
    node_id: str | None = None
    spec: SpecView | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if self.spec is not None:
            data["spec"] = self.spec.to_dict()
        return data


@dataclass
class LoadedInputs:
    plan: dict[str, Any]
    scheduling: dict[str, int]
    initial_spec: SpecView
    nodes: tuple[NodeInfo, ...]
    events: tuple[GradientEvent, ...]
    injections: tuple[Injection, ...]
    paths: dict[str, str]
    raw_text: dict[str, str]

    @property
    def node_ids(self) -> set[str]:
        return {node.node_id for node in self.nodes}

    def spec_versions(self) -> dict[int, SpecView]:
        result = {self.initial_spec.version: self.initial_spec}
        for injection in self.injections:
            if injection.kind == "spec_replace" and injection.spec is not None:
                result[injection.spec.version] = injection.spec
        return result


def pick(data: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    return default


def strict_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _error(code: str, message: str, **details: Any) -> EngineError:
    return EngineError(code, message, details or None)


def _require_nonempty_str(value: Any, what: str, code: str, **details: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _error(code, f"{what} must be a non-empty string", **details)
    return value.strip()


def validate_loopback_address(address: Any, *, node_id: str | None = None) -> str:
    if not isinstance(address, str) or not address.strip():
        raise _error(
            ErrorCode.NODES_INVALID,
            "node loopback address must be a non-empty string",
            node_id=node_id,
            address=address,
        )
    raw = address.strip()
    detail = {"node_id": node_id, "address": raw}

    if "://" in raw:
        parsed = urlparse(raw)
        scheme = (parsed.scheme or "").lower()
        host = parsed.hostname
        port = parsed.port
        if scheme in {"loopback", "lo", "inproc"}:
            if not host:
                raise _error(ErrorCode.NODES_INVALID, "loopback address host is missing", **detail)
            if port is None and scheme != "inproc":
                raise _error(ErrorCode.NODES_INVALID, "loopback address port is missing", **detail)
            if port is not None and not (1 <= port <= 65535):
                raise _error(ErrorCode.NODES_INVALID, "loopback address port is invalid", **detail)
            return raw
        if scheme not in {"tcp", "udp", "http", "https"}:
            raise _error(ErrorCode.NODES_INVALID, "unsupported loopback address scheme", **detail)
    else:
        candidate = raw
        if candidate.startswith("["):
            end = candidate.find("]")
            if end < 0 or end + 1 >= len(candidate) or candidate[end + 1] != ":":
                raise _error(ErrorCode.NODES_INVALID, "IPv6 loopback address must use [host]:port", **detail)
            host = candidate[1:end]
            try:
                port = int(candidate[end + 2 :])
            except ValueError as exc:
                raise _error(ErrorCode.NODES_INVALID, "loopback port must be an integer", **detail) from exc
        else:
            if candidate.count(":") != 1:
                raise _error(ErrorCode.NODES_INVALID, "loopback address must be host:port", **detail)
            host, port_text = candidate.rsplit(":", 1)
            try:
                port = int(port_text)
            except ValueError as exc:
                raise _error(ErrorCode.NODES_INVALID, "loopback port must be an integer", **detail) from exc
        if not (1 <= port <= 65535):
            raise _error(ErrorCode.NODES_INVALID, "loopback address port is invalid", **detail)

    if host is None:
        raise _error(ErrorCode.NODES_INVALID, "loopback address host is missing", **detail)
    host_text = host.strip("[]").lower()
    if host_text == "localhost":
        return raw
    try:
        ip = ipaddress.ip_address(host_text)
    except ValueError as exc:
        raise _error(
            ErrorCode.NODES_INVALID,
            "node address must resolve to a loopback IP or localhost",
            **detail,
        ) from exc
    if not ip.is_loopback:
        raise _error(ErrorCode.NODES_INVALID, "node address must be loopback", **detail)
    return raw


def _normalize_shards(layer_raw: dict[str, Any], layer_name: str) -> tuple[ShardSpec, ...]:
    code = ErrorCode.PLAN_INVALID
    shards_raw = pick(layer_raw, "shards", "shard_specs", "partitions", "shard_sizes")
    shard_count = pick(layer_raw, "shard_count", "num_shards", "n_shards", "shard_num")
    uniform_bytes = pick(
        layer_raw,
        "shard_byte_size",
        "bytes_per_shard",
        "shard_size_bytes",
        "default_shard_bytes",
    )

    normalized: list[ShardSpec] = []

    if isinstance(shards_raw, dict):
        for key, value in shards_raw.items():
            try:
                index = int(key)
            except ValueError as exc:
                raise _error(code, "shard map key must be an integer index", layer=layer_name, key=key) from exc
            if isinstance(value, dict):
                size = pick(value, "byte_size", "bytes", "size_bytes", "nbytes")
            else:
                size = value
            if not strict_int(size) or size <= 0:
                raise _error(code, "shard byte size must be a positive integer", layer=layer_name, shard=index)
            normalized.append(ShardSpec(index=index, byte_size=size))
    elif isinstance(shards_raw, list) and shards_raw:
        for position, item in enumerate(shards_raw):
            if strict_int(item):
                index = position
                size = item
            elif isinstance(item, dict):
                index = pick(item, "index", "shard_index", "id", default=position)
                size = pick(item, "byte_size", "bytes", "size_bytes", "nbytes")
                if not strict_int(index) or index < 0:
                    raise _error(code, "shard index must be a non-negative integer", layer=layer_name, position=position)
            else:
                raise _error(code, "shard entry must be an integer or object", layer=layer_name, position=position)
            if not strict_int(size) or size <= 0:
                raise _error(code, "shard byte size must be a positive integer", layer=layer_name, shard=index)
            normalized.append(ShardSpec(index=index, byte_size=size))
    if not normalized:
        effective_count = shard_count if strict_int(shard_count) and shard_count > 0 else shards_raw
        if strict_int(effective_count) and effective_count > 0:
            shard_count = effective_count
            sizes = pick(layer_raw, "shard_bytes", "byte_sizes", "shard_byte_sizes", "shard_byte_size")
            if isinstance(sizes, list) and len(sizes) == shard_count:
                for index, size in enumerate(sizes):
                    if not strict_int(size) or size <= 0:
                        raise _error(code, "shard byte size must be a positive integer", layer=layer_name, shard=index)
                    normalized.append(ShardSpec(index=index, byte_size=size))
            elif strict_int(uniform_bytes) and uniform_bytes > 0:
                normalized = [ShardSpec(index=i, byte_size=uniform_bytes) for i in range(shard_count)]
            else:
                raise _error(code, "shard_count requires positive shard sizes", layer=layer_name)

    if not normalized:
        raise _error(code, "sharding must declare non-empty shards", layer=layer_name)

    normalized.sort(key=lambda shard: shard.index)
    indices = [shard.index for shard in normalized]
    if indices != list(range(len(normalized))):
        raise _error(
            code,
            "shard indices must be continuous and start at zero",
            layer=layer_name,
            indices=indices,
        )
    if len(set(indices)) != len(indices):
        raise _error(code, "shard indices must be unique", layer=layer_name, indices=indices)
    return tuple(normalized)


def _declared_parameter_bytes(layer_raw: dict[str, Any]) -> int | None:
    total = pick(
        layer_raw,
        "parameter_bytes",
        "param_bytes",
        "parameters_bytes",
        "total_parameter_bytes",
        "total_bytes",
        "byte_count",
    )
    if strict_int(total):
        if total <= 0:
            raise _error(ErrorCode.PLAN_INVALID, "layer parameter bytes must be positive", layer=layer_raw.get("name"))
        return total

    parameters = pick(layer_raw, "parameters", "params")
    if isinstance(parameters, list) and parameters:
        total_value = 0
        for param in parameters:
            if strict_int(param):
                total_value += param
            elif isinstance(param, dict):
                size = pick(param, "byte_size", "bytes", "size_bytes", "nbytes")
                if not strict_int(size) or size <= 0:
                    return None
                total_value += size
            else:
                return None
        return total_value

    count = pick(layer_raw, "parameter_count", "param_count", "num_parameters")
    bytes_per = pick(layer_raw, "bytes_per_parameter", "parameter_byte_size", "dtype_bytes")
    if strict_int(count) and count > 0 and strict_int(bytes_per) and bytes_per > 0:
        return count * bytes_per
    return None


def parse_spec(raw_spec: Any, *, version: int | None = None, code: str = ErrorCode.PLAN_INVALID) -> SpecView:
    if not isinstance(raw_spec, dict):
        raise _error(code, "training plan/spec must be a JSON object")
    raw_version = pick(raw_spec, "version", "spec_version", "sharding_version")
    if version is None:
        version = raw_version if strict_int(raw_version) else 1
    if not strict_int(version) or version <= 0:
        raise _error(ErrorCode.SPEC_INVALID, "spec version must be a positive integer", version=version)

    method = pick(raw_spec, "sharding_method", "method", "partition_method")
    sharding = pick(raw_spec, "sharding", "partition")
    if not method and isinstance(sharding, dict):
        method = pick(sharding, "method", "name", "type")
    if not method and isinstance(sharding, str):
        method = sharding
    method = _require_nonempty_str(method, "sharding method", ErrorCode.PLAN_INVALID)

    layers_raw = pick(raw_spec, "layers", "model_layers")
    if not isinstance(layers_raw, list) or not layers_raw:
        raise _error(ErrorCode.PLAN_INVALID, "training plan must declare non-empty model layers")

    layers: list[LayerSpec] = []
    seen_names: set[str] = set()
    for position, layer_raw in enumerate(layers_raw):
        if not isinstance(layer_raw, dict):
            raise _error(ErrorCode.PLAN_INVALID, "layer entry must be an object", position=position)
        name = pick(layer_raw, "name", "layer_name", "layer_id", "id")
        name = _require_nonempty_str(name, "layer name", ErrorCode.PLAN_INVALID, position=position)
        if name in seen_names:
            raise _error(ErrorCode.PLAN_INVALID, "model layer names must be unique", layer=name)
        seen_names.add(name)

        shards = _normalize_shards(layer_raw, name)
        total = _declared_parameter_bytes(layer_raw)
        shard_total = sum(shard.byte_size for shard in shards)
        if total is not None and total != shard_total:
            raise _error(
                ErrorCode.PLAN_INVALID,
                "shard byte sizes must cover all layer parameter bytes",
                layer=name,
                declared_bytes=total,
                shard_bytes=shard_total,
            )
        layers.append(LayerSpec(name=name, parameter_bytes=total or shard_total, shards=shards))

    return SpecView(version=version, method=method, layers=tuple(layers))


def parse_scheduling(plan: dict[str, Any]) -> dict[str, int]:
    raw = pick(plan, "scheduling", "round", "schedule", "budget")
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise _error(ErrorCode.PLAN_INVALID, "scheduling configuration must be an object")
    concurrency = pick(
        raw,
        "max_concurrency",
        "concurrency",
        "parallel_limit",
        "max_parallel",
        default=DEFAULT_MAX_CONCURRENCY,
    )
    budget = pick(
        raw,
        "time_budget_ms",
        "budget_ms",
        "round_budget_ms",
        "sync_budget_ms",
        default=DEFAULT_TIME_BUDGET_MS,
    )
    if not strict_int(concurrency) or concurrency <= 0:
        raise _error(ErrorCode.PLAN_INVALID, "max_concurrency must be a positive integer")
    if not strict_int(budget) or budget <= 0:
        raise _error(ErrorCode.PLAN_INVALID, "time_budget_ms must be a positive integer")
    return {"max_concurrency": concurrency, "time_budget_ms": budget}


def parse_plan(raw: Any) -> tuple[dict[str, Any], SpecView, dict[str, int]]:
    if not isinstance(raw, dict):
        raise _error(ErrorCode.PLAN_INVALID, "training plan must be a JSON object")
    spec = parse_spec(raw, code=ErrorCode.PLAN_INVALID)
    scheduling = parse_scheduling(raw)
    return raw, spec, scheduling


def parse_nodes(raw: Any) -> tuple[NodeInfo, ...]:
    if isinstance(raw, dict):
        items = pick(raw, "nodes", "workers", "node_list", "worker_nodes", default=[])
    else:
        items = raw
    if not isinstance(items, list) or not items:
        raise _error(ErrorCode.NODES_INVALID, "node inventory must contain a non-empty nodes list")

    nodes: list[NodeInfo] = []
    seen: set[str] = set()
    for position, item in enumerate(items):
        if not isinstance(item, dict):
            raise _error(ErrorCode.NODES_INVALID, "node entry must be an object", position=position)
        node_id = pick(item, "node_id", "id", "worker_id", "name")
        node_id = _require_nonempty_str(
            node_id, "node identifier", ErrorCode.NODES_INVALID, position=position
        )
        if node_id in seen:
            raise _error(ErrorCode.NODES_INVALID, "node identifiers must be unique", node_id=node_id)
        seen.add(node_id)

        address = validate_loopback_address(
            pick(item, "address", "loopback_address", "addr", "endpoint", "communication_address"),
            node_id=node_id,
        )
        ratio = pick(item, "compute_ratio", "compute_power", "ratio", "weight", default=1.0)
        if isinstance(ratio, bool) or not isinstance(ratio, (int, float)) or not math.isfinite(float(ratio)) or ratio <= 0:
            raise _error(ErrorCode.NODES_INVALID, "compute_ratio must be a positive finite number", node_id=node_id)
        memory_limit = pick(
            item,
            "memory_limit_bytes",
            "memory_limit",
            "max_memory_bytes",
            "memory_cap_bytes",
            "memory_bytes_limit",
            "mem_limit_bytes",
        )
        if not strict_int(memory_limit) or memory_limit <= 0:
            raise _error(
                ErrorCode.NODES_INVALID,
                "memory limit must be a positive integer number of bytes",
                node_id=node_id,
            )
        nodes.append(NodeInfo(node_id, address, float(ratio), memory_limit))
    return tuple(sorted(nodes, key=lambda node: node.node_id))


def _read_json(path: Path, code: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise _error(code, f"required file not found: {path}", path=str(path)) from exc
    except json.JSONDecodeError as exc:
        raise _error(code, f"invalid JSON in {path.name}: {exc.msg}", path=str(path), line=exc.lineno) from exc


def _read_events(path: Path) -> list[dict[str, Any]]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise _error(ErrorCode.EVENTS_INVALID, f"event file not found: {path}", path=str(path)) from exc
    events: list[dict[str, Any]] = []
    if path.suffix.lower() == ".json":
        raw = json.loads(text) if text.strip() else []
        if isinstance(raw, dict):
            raw = pick(raw, "events", "gradient_events", default=[])
        if not isinstance(raw, list):
            raise _error(ErrorCode.EVENTS_INVALID, "events JSON must be a list or JSONL file")
        return raw
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise _error(
                ErrorCode.EVENTS_INVALID,
                f"invalid JSONL at line {line_number}: {exc.msg}",
                line=line_number,
            ) from exc
        if not isinstance(item, dict):
            raise _error(ErrorCode.EVENTS_INVALID, "each event must be a JSON object", line=line_number)
        item["_line"] = line_number
        events.append(item)
    return events


def normalize_injections(raw: Any, known_nodes: set[str], initial_version: int) -> tuple[Injection, ...]:
    if raw is None:
        items: list[dict[str, Any]] = []
    elif isinstance(raw, dict):
        items = pick(
            raw,
            "injections",
            "injection_list",
            "faults",
            "fault_injections",
            "inject_list",
            default=[],
        )
    else:
        items = raw
    if not isinstance(items, list):
        raise _error(ErrorCode.INJECTIONS_INVALID, "injections must be a JSON object with an injections list")

    result: list[Injection] = []
    last_version = initial_version
    seen_seq: set[int] = set()
    last_seq: int | None = None
    for order_index, item in enumerate(items):
        if not isinstance(item, dict):
            raise _error(ErrorCode.INJECTIONS_INVALID, "injection entry must be an object", index=order_index)
        clock = pick(item, "logic_clock_ms", "clock_ms", "logical_clock_ms", "at_ms", "time_ms")
        if not strict_int(clock) or clock < 0:
            raise _error(ErrorCode.INJECTIONS_INVALID, "injection logic clock must be a non-negative integer", index=order_index)
        raw_kind = pick(item, "type", "kind", "injection_type", "action")
        kind = _normalize_injection_kind(raw_kind, order_index)
        injection_id = str(pick(item, "id", "injection_id", "seq", default=f"inj-{order_index + 1}"))
        seq = pick(item, "seq", "injection_seq", "sequence")
        if seq is not None:
            if not strict_int(seq):
                raise _error(ErrorCode.INJECTIONS_INVALID, "injection seq must be an integer", index=order_index)
            if seq in seen_seq or (last_seq is not None and seq <= last_seq):
                raise _error(ErrorCode.INJECTIONS_INVALID, "injection seq must be strictly increasing", seq=seq)
            seen_seq.add(seq)
            last_seq = seq

        node_id = pick(item, "node_id", "node", "worker_id", "target")
        if kind != "spec_replace":
            node_id = _require_nonempty_str(node_id, "injection node_id", ErrorCode.INJECTIONS_INVALID, index=order_index)
            if node_id not in known_nodes:
                raise _error(ErrorCode.INJECTIONS_INVALID, "injection targets unknown node", node_id=node_id)

        spec = None
        payload = dict(item)
        if kind == "spec_replace":
            spec_raw = pick(item, "spec", "new_spec", "sharding_spec", "plan", "training_plan")
            if spec_raw is None and isinstance(item.get("layers"), list):
                spec_raw = item
            if spec_raw is None:
                raise _error(ErrorCode.SPEC_INVALID, "spec_replace injection requires a spec payload", index=order_index)
            explicit_version = pick(spec_raw if isinstance(spec_raw, dict) else {}, "version", "spec_version")
            version = explicit_version if strict_int(explicit_version) else last_version + 1
            if not strict_int(version) or version <= last_version:
                raise _error(
                    ErrorCode.SPEC_VERSION_CONFLICT,
                    "spec replacement version must be strictly monotonic",
                    previous_version=last_version,
                    version=version,
                )
            spec = parse_spec(spec_raw, version=version, code=ErrorCode.SPEC_INVALID)
            last_version = version

        result.append(
            Injection(
                order_index=order_index,
                logic_clock_ms=clock,
                kind=kind,
                injection_id=injection_id,
                node_id=node_id,
                spec=spec,
                payload=payload,
            )
        )
    return tuple(result)


def _normalize_injection_kind(raw_kind: Any, index: int) -> str:
    if not isinstance(raw_kind, str):
        raise _error(ErrorCode.INJECTIONS_INVALID, "injection type is required", index=index)
    kind = raw_kind.strip().lower().replace("-", "_")
    aliases = {
        "fault": "node_fault",
        "node_fault": "node_fault",
        "fail": "node_fault",
        "node_fail": "node_fault",
        "node_down": "node_fault",
        "exit": "node_fault",
        "node_exit": "node_fault",
        "kill": "node_fault",
        "crash": "node_fault",
        "recover": "node_recover",
        "recovery": "node_recover",
        "node_recover": "node_recover",
        "node_recovery": "node_recover",
        "node_up": "node_recover",
        "join": "node_recover",
        "node_join": "node_recover",
        "restart": "node_recover",
        "spec_replace": "spec_replace",
        "replace_spec": "spec_replace",
        "spec_change": "spec_replace",
        "reshard": "spec_replace",
        "sharding_change": "spec_replace",
        "parameter_shard_change": "spec_replace",
    }
    if kind not in aliases:
        raise _error(ErrorCode.INJECTIONS_INVALID, "unsupported injection type", type=raw_kind, index=index)
    return aliases[kind]


def normalize_events(raw_events: Iterable[dict[str, Any]], inputs: LoadedInputs | None = None,
                     specs: dict[int, SpecView] | None = None,
                     node_ids: set[str] | None = None) -> tuple[GradientEvent, ...]:
    if specs is None and inputs is not None:
        specs = inputs.spec_versions()
    if node_ids is None and inputs is not None:
        node_ids = inputs.node_ids
    assert specs is not None and node_ids is not None

    events: list[GradientEvent] = []
    last_seq: int | None = None
    for position, item in enumerate(raw_events):
        line = item.get("_line", position + 1) if isinstance(item, dict) else position + 1
        if not isinstance(item, dict):
            raise _error(ErrorCode.EVENTS_INVALID, "event must be a JSON object", line=line)
        seq = pick(item, "event_seq", "seq", "sequence", "event_id", "id")
        clock = pick(item, "logic_clock_ms", "clock_ms", "logical_clock_ms", "timestamp_ms", "ts_ms")
        node_id = pick(item, "node_id", "node", "worker_id")
        layer = pick(item, "layer", "layer_name", "model_layer")
        shard_index = pick(item, "shard_index", "shard", "index", "shard_seq", "shard_id")
        expected = pick(
            item,
            "expected_commit_ms",
            "expected_commit_at_ms",
            "expected_time_ms",
            "expected_commit_time_ms",
            "expected_commit_at_ms",
            "commit_deadline_ms",
            "expected_at_ms",
        )
        spec_version = pick(item, "spec_version", "version", "shard_version", default=1)
        duration = pick(item, "duration_ms", "transfer_ms", "cost_ms")

        if not strict_int(seq):
            raise _error(ErrorCode.EVENTS_INVALID, "event_seq must be an integer", line=line)
        if last_seq is not None and seq <= last_seq:
            raise _error(ErrorCode.EVENTS_INVALID, "event_seq values must be strictly increasing", line=line, seq=seq)
        last_seq = seq
        if not strict_int(clock) or clock < 0:
            raise _error(ErrorCode.EVENTS_INVALID, "event logic clock must be a non-negative integer", event_seq=seq)
        if not isinstance(node_id, str) or node_id not in node_ids:
            raise _error(ErrorCode.EVENTS_INVALID, "event node_id must reference a declared node", event_seq=seq, node_id=node_id)
        if not isinstance(layer, str) or not layer:
            raise _error(ErrorCode.EVENTS_INVALID, "event layer must be a non-empty string", event_seq=seq)
        if not strict_int(shard_index) or shard_index < 0:
            raise _error(ErrorCode.EVENTS_INVALID, "event shard_index must be a non-negative integer", event_seq=seq)
        if not strict_int(expected) or expected < 0:
            raise _error(ErrorCode.EVENTS_INVALID, "expected_commit_ms must be a non-negative integer", event_seq=seq)
        if not strict_int(spec_version) or spec_version <= 0 or spec_version not in specs:
            raise _error(ErrorCode.EVENTS_INVALID, "event spec_version must reference a declared specification", event_seq=seq, spec_version=spec_version)
        if duration is not None and (not strict_int(duration) or duration <= 0):
            raise _error(ErrorCode.EVENTS_INVALID, "duration_ms must be a positive integer when provided", event_seq=seq)

        spec = specs[spec_version]
        if not spec.has_shard(layer, shard_index):
            raise _error(
                ErrorCode.EVENTS_INVALID,
                "event shard_index must belong to the declared layer shard set",
                event_seq=seq,
                layer=layer,
                shard_index=shard_index,
                spec_version=spec_version,
            )
        events.append(
            GradientEvent(
                event_seq=seq,
                logic_clock_ms=clock,
                node_id=node_id,
                layer=layer,
                shard_index=shard_index,
                spec_version=spec_version,
                expected_commit_ms=expected,
                duration_ms=duration,
            )
        )
    return tuple(events)


def resolve_sample_paths(samples_dir: str | Path) -> dict[str, Path]:
    root = Path(samples_dir)
    candidates = {
        "plan": ["training_plan.json", "plan.json", "model_plan.json"],
        "nodes": ["nodes.json", "node_inventory.json", "workers.json"],
        "events": ["gradient_events.jsonl", "events.jsonl", "gradient_events.json"],
        "injections": ["injections.json", "injection_plan.json", "fault_injections.json"],
    }
    paths: dict[str, Path] = {}
    missing: list[str] = []
    for key, names in candidates.items():
        found = None
        for name in names:
            candidate = root / name
            if candidate.exists():
                found = candidate
                break
        if found is None:
            missing.append(candidates[key][0])
        else:
            paths[key] = found
    if missing:
        raise _error(ErrorCode.NOT_FOUND, "sample directory is missing required files", samples_dir=str(root), missing=missing)
    return paths


def load_inputs(
    samples_dir: str | Path | None = None,
    *,
    plan_path: str | Path | None = None,
    nodes_path: str | Path | None = None,
    events_path: str | Path | None = None,
    injections_path: str | Path | None = None,
) -> LoadedInputs:
    if samples_dir is not None:
        resolved = resolve_sample_paths(samples_dir)
    else:
        resolved = {}
    explicit = {
        "plan": Path(plan_path) if plan_path else resolved.get("plan"),
        "nodes": Path(nodes_path) if nodes_path else resolved.get("nodes"),
        "events": Path(events_path) if events_path else resolved.get("events"),
        "injections": Path(injections_path) if injections_path else resolved.get("injections"),
    }
    if any(value is None for value in explicit.values()):
        raise _error(ErrorCode.CLI_INVALID, "plan, nodes, events and injections inputs are required")

    plan_path_obj = explicit["plan"]
    nodes_path_obj = explicit["nodes"]
    events_path_obj = explicit["events"]
    injections_path_obj = explicit["injections"]
    assert all(isinstance(p, Path) for p in explicit.values())

    plan_raw = _read_json(plan_path_obj, ErrorCode.PLAN_INVALID)
    plan, initial_spec, scheduling = parse_plan(plan_raw)
    nodes = parse_nodes(_read_json(nodes_path_obj, ErrorCode.NODES_INVALID))
    node_ids = {node.node_id for node in nodes}

    injections_raw = _read_json(injections_path_obj, ErrorCode.INJECTIONS_INVALID)
    if isinstance(injections_raw, dict):
        injection_items = pick(injections_raw, "injections", "injection_list", "faults", default=[])
    else:
        injection_items = injections_raw
    injections = normalize_injections(injection_items, node_ids, initial_spec.version)
    specs = {initial_spec.version: initial_spec}
    for injection in injections:
        if injection.spec is not None:
            specs[injection.spec.version] = injection.spec

    raw_event_items = _read_events(events_path_obj)
    events = normalize_events(raw_event_items, specs=specs, node_ids=node_ids)

    raw_text = {
        "plan": plan_path_obj.read_text(encoding="utf-8"),
        "nodes": nodes_path_obj.read_text(encoding="utf-8"),
        "events": events_path_obj.read_text(encoding="utf-8"),
        "injections": injections_path_obj.read_text(encoding="utf-8"),
    }
    return LoadedInputs(
        plan=plan,
        scheduling=scheduling,
        initial_spec=initial_spec,
        nodes=nodes,
        events=events,
        injections=injections,
        paths={key: str(path) for key, path in explicit.items()},
        raw_text=raw_text,
    )
