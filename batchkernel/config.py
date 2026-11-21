from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .errors import BusinessError


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def digest_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def load_json(path: str | Path, error_code: str) -> Any:
    file_path = Path(path)
    try:
        text = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise BusinessError(
            error_code,
            "cannot read JSON input",
            {"path": str(file_path), "reason": str(exc)},
        ) from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise BusinessError(
            error_code,
            "invalid JSON input",
            {
                "path": str(file_path),
                "line": exc.lineno,
                "column": exc.colno,
                "reason": exc.msg,
            },
        ) from exc


def _mapping(value: Any, code: str, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BusinessError(code, f"{context} must be a JSON object", {"context": context})
    return value


def _first(mapping: dict[str, Any], names: Iterable[str], default: Any = None) -> Any:
    for name in names:
        if name in mapping:
            return mapping[name]
    return default


def _as_int(value: Any, code: str, field: str, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BusinessError(code, f"{field} must be an integer", {"field": field})
    if minimum is not None and value < minimum:
        raise BusinessError(
            code,
            f"{field} must be >= {minimum}",
            {"field": field, "value": value, "minimum": minimum},
        )
    return value


def _as_positive_float(value: Any, code: str, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BusinessError(code, f"{field} must be numeric", {"field": field})
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise BusinessError(
            code,
            f"{field} must be a finite positive number",
            {"field": field, "value": value},
        )
    return result


def _as_nonnegative_float(value: Any, code: str, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BusinessError(code, f"{field} must be numeric", {"field": field})
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise BusinessError(
            code,
            f"{field} must be a finite non-negative number",
            {"field": field, "value": value},
        )
    return result


@dataclass(frozen=True)
class LayerSpec:
    name: str
    parameters: int

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "parameters": self.parameters}


@dataclass(frozen=True)
class ModelSpec:
    model_id: str
    layers: tuple[LayerSpec, ...]
    per_request_input_tokens: int
    per_request_output_tokens: int
    compute_units_per_million_parameters_per_token: float
    memory_mib_per_million_parameters: float
    memory_mib_per_token: float
    raw: dict[str, Any]

    @property
    def total_parameters(self) -> int:
        return sum(layer.parameters for layer in self.layers)

    def to_json(self) -> dict[str, Any]:
        return self.raw


@dataclass(frozen=True)
class ReplicaSpec:
    replica_id: str
    compute_units: float
    memory_limit_mib: int
    batch_window_ms: int
    max_batch_size: int
    max_sequence_length: int

    def to_json(self) -> dict[str, Any]:
        return {
            "replica_id": self.replica_id,
            "compute_units": self.compute_units,
            "memory_limit_mib": self.memory_limit_mib,
            "batch_window_ms": self.batch_window_ms,
            "max_batch_size": self.max_batch_size,
            "max_sequence_length": self.max_sequence_length,
        }


@dataclass(frozen=True)
class ReplicaSnapshot:
    version: int
    replicas: tuple[ReplicaSpec, ...]
    raw: dict[str, Any]

    @property
    def by_id(self) -> dict[str, ReplicaSpec]:
        return {replica.replica_id: replica for replica in self.replicas}

    @property
    def content(self) -> dict[str, Any]:
        return {"replicas": [replica.to_json() for replica in self.replicas]}

    @property
    def content_digest(self) -> str:
        return digest_json(self.content)

    def to_json(self) -> dict[str, Any]:
        return self.raw


@dataclass(frozen=True)
class RequestSpec:
    request_seq: int
    arrival_ms: int
    priority: int
    input_tokens: int
    max_output_tokens: int
    deadline_ms: int
    replica_id: str | None
    simulate_failures: int
    raw: dict[str, Any]

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.max_output_tokens

    def to_json(self) -> dict[str, Any]:
        return self.raw


@dataclass(frozen=True)
class PrecisionSpec:
    compute_scale: int
    memory_scale: int
    queue_scale: int
    relative_tolerance: float
    error_bound_budget: float
    clock_step_ms: int
    raw: dict[str, Any]

    def to_json(self) -> dict[str, Any]:
        return self.raw


@dataclass(frozen=True)
class ConfigBundle:
    model: ModelSpec | None
    replicas: ReplicaSnapshot | None
    requests: tuple[RequestSpec, ...] | None
    precision: PrecisionSpec | None


def parse_model(document: Any) -> ModelSpec:
    root = _mapping(document, "E_BAD_MODEL_INPUT", "model")
    model_id = _first(root, ("model_id", "id", "name"), "local-model")
    if not isinstance(model_id, str) or not model_id.strip():
        raise BusinessError(
            "E_BAD_MODEL_INPUT", "model_id must be a non-empty string", {"field": "model_id"}
        )
    layers_value = root.get("layers")
    if not isinstance(layers_value, list) or not layers_value:
        raise BusinessError(
            "E_BAD_MODEL_INPUT",
            "layers must be a non-empty array",
            {"field": "layers"},
        )
    layers: list[LayerSpec] = []
    names: set[str] = set()
    for index, item in enumerate(layers_value):
        layer = _mapping(item, "E_BAD_MODEL_INPUT", f"layers[{index}]")
        name = layer.get("name")
        if not isinstance(name, str) or not name:
            raise BusinessError(
                "E_BAD_MODEL_INPUT",
                "layer name must be a non-empty string",
                {"field": f"layers[{index}].name"},
            )
        if name in names:
            raise BusinessError(
                "E_BAD_MODEL_INPUT",
                "duplicate layer name",
                {"field": f"layers[{index}].name", "name": name},
            )
        names.add(name)
        parameters = _as_int(
            _first(
                layer,
                ("parameters", "params", "param_count", "parameter_count"),
            ),
            "E_BAD_MODEL_INPUT",
            f"layers[{index}].parameters",
            0,
        )
        layers.append(LayerSpec(name=name, parameters=parameters))
    if sum(layer.parameters for layer in layers) <= 0:
        raise BusinessError(
            "E_BAD_MODEL_INPUT",
            "total model parameter count must be positive",
            {"field": "layers"},
        )

    token_profile = _first(root, ("per_request_tokens", "request_profile"), {})
    token_profile = _mapping(token_profile, "E_BAD_MODEL_INPUT", "per_request_tokens")
    input_tokens = _as_int(
        _first(token_profile, ("input", "input_tokens"), 8),
        "E_BAD_MODEL_INPUT",
        "per_request_tokens.input",
        0,
    )
    output_tokens = _as_int(
        _first(token_profile, ("output", "output_tokens"), 8),
        "E_BAD_MODEL_INPUT",
        "per_request_tokens.output",
        0,
    )
    if input_tokens + output_tokens <= 0:
        raise BusinessError(
            "E_BAD_MODEL_INPUT",
            "per-request token profile must contain at least one token",
            {"field": "per_request_tokens"},
        )
    compute_factor = _as_positive_float(
        _first(
            root,
            (
                "compute_units_per_million_parameters_per_token",
                "compute_factor",
            ),
            1.0,
        ),
        "E_BAD_MODEL_INPUT",
        "compute_units_per_million_parameters_per_token",
    )
    memory_per_million = _as_nonnegative_float(
        _first(root, ("memory_mib_per_million_parameters", "memory_factor"), 2.0),
        "E_BAD_MODEL_INPUT",
        "memory_mib_per_million_parameters",
    )
    memory_per_token = _as_nonnegative_float(
        _first(root, ("memory_mib_per_token",), 0.125),
        "E_BAD_MODEL_INPUT",
        "memory_mib_per_token",
    )
    normalized = {
        "model_id": model_id,
        "layers": [layer.to_json() for layer in layers],
        "per_request_tokens": {"input": input_tokens, "output": output_tokens},
        "compute_units_per_million_parameters_per_token": compute_factor,
        "memory_mib_per_million_parameters": memory_per_million,
        "memory_mib_per_token": memory_per_token,
    }
    return ModelSpec(
        model_id=model_id,
        layers=tuple(layers),
        per_request_input_tokens=input_tokens,
        per_request_output_tokens=output_tokens,
        compute_units_per_million_parameters_per_token=compute_factor,
        memory_mib_per_million_parameters=memory_per_million,
        memory_mib_per_token=memory_per_token,
        raw=normalized,
    )


def parse_replicas(document: Any) -> ReplicaSnapshot:
    if isinstance(document, list):
        root: dict[str, Any] = {"version": 1, "replicas": document}
    else:
        root = _mapping(document, "E_BAD_REPLICA_INPUT", "replicas")
    version = _as_int(
        _first(root, ("version", "replica_version"), 1),
        "E_REPLICA_VERSION_INVALID",
        "version",
        1,
    )
    replicas_value = _first(root, ("replicas", "items"))
    if not isinstance(replicas_value, list) or not replicas_value:
        raise BusinessError(
            "E_BAD_REPLICA_INPUT",
            "replicas must be a non-empty array",
            {"field": "replicas"},
        )
    replicas: list[ReplicaSpec] = []
    seen: set[str] = set()
    for index, item in enumerate(replicas_value):
        replica = _mapping(item, "E_BAD_REPLICA_INPUT", f"replicas[{index}]")
        replica_id = _first(replica, ("replica_id", "id"))
        if not isinstance(replica_id, str) or not replica_id:
            raise BusinessError(
                "E_BAD_REPLICA_INPUT",
                "replica_id must be a non-empty string",
                {"field": f"replicas[{index}].replica_id"},
            )
        if replica_id in seen:
            raise BusinessError(
                "E_DUPLICATE_REPLICA",
                "replica_id must be unique",
                {"field": f"replicas[{index}].replica_id", "replica_id": replica_id},
            )
        seen.add(replica_id)
        compute_units = _as_positive_float(
            _first(replica, ("compute_units", "compute")),
            "E_REPLICA_COMPUTE_INVALID",
            f"replicas[{index}].compute_units",
        )
        memory_limit = _as_int(
            _first(replica, ("memory_limit_mib", "memory_mib", "memory_limit")),
            "E_REPLICA_CAPACITY_INVALID",
            f"replicas[{index}].memory_limit_mib",
            1,
        )
        batch_window = _as_int(
            _first(replica, ("batch_window_ms", "window_ms", "batching_window_ms")),
            "E_REPLICA_WINDOW_INVALID",
            f"replicas[{index}].batch_window_ms",
            1,
        )
        max_batch_size = _as_int(
            _first(replica, ("max_batch_size", "batch_size_limit")),
            "E_REPLICA_CAPACITY_INVALID",
            f"replicas[{index}].max_batch_size",
            1,
        )
        max_sequence_length = _as_int(
            _first(
                replica,
                ("max_sequence_length", "sequence_length_limit", "max_seq_len"),
            ),
            "E_REPLICA_CAPACITY_INVALID",
            f"replicas[{index}].max_sequence_length",
            1,
        )
        replicas.append(
            ReplicaSpec(
                replica_id=replica_id,
                compute_units=compute_units,
                memory_limit_mib=memory_limit,
                batch_window_ms=batch_window,
                max_batch_size=max_batch_size,
                max_sequence_length=max_sequence_length,
            )
        )
    replicas.sort(key=lambda replica: replica.replica_id)
    normalized = {
        "version": version,
        "replicas": [replica.to_json() for replica in replicas],
    }
    return ReplicaSnapshot(version=version, replicas=tuple(replicas), raw=normalized)


def parse_requests(document: Any) -> tuple[RequestSpec, ...]:
    if isinstance(document, list):
        requests_value = document
    else:
        root = _mapping(document, "E_BAD_REQUEST_INPUT", "requests")
        requests_value = _first(root, ("requests", "trace", "items"))
    if not isinstance(requests_value, list) or not requests_value:
        raise BusinessError(
            "E_BAD_REQUEST_INPUT",
            "requests must be a non-empty array",
            {"field": "requests"},
        )
    requests: list[RequestSpec] = []
    seen: set[int] = set()
    for index, item in enumerate(requests_value):
        request = _mapping(item, "E_BAD_REQUEST_INPUT", f"requests[{index}]")
        seq_value = _first(request, ("request_seq", "seq", "sequence", "request_id"))
        if isinstance(seq_value, str) and seq_value.lstrip("-").isdigit():
            seq_value = int(seq_value)
        request_seq = _as_int(
            seq_value,
            "E_REQUEST_SEQ_INVALID",
            f"requests[{index}].request_seq",
        )
        if request_seq in seen:
            raise BusinessError(
                "E_DUPLICATE_REQUEST",
                "request_seq must be unique",
                {
                    "field": f"requests[{index}].request_seq",
                    "request_seq": request_seq,
                },
            )
        seen.add(request_seq)
        arrival = _as_int(
            _first(request, ("arrival_ms", "arrival_clock_ms", "arrival")),
            "E_REQUEST_CLOCK_INVALID",
            f"requests[{index}].arrival_ms",
            0,
        )
        deadline = _as_int(
            _first(request, ("deadline_ms", "deadline_clock_ms", "deadline")),
            "E_REQUEST_CLOCK_INVALID",
            f"requests[{index}].deadline_ms",
            0,
        )
        if deadline < arrival:
            raise BusinessError(
                "E_DEADLINE_BEFORE_ARRIVAL",
                "deadline_ms must not be earlier than arrival_ms",
                {
                    "field": f"requests[{index}].deadline_ms",
                    "request_seq": request_seq,
                    "arrival_ms": arrival,
                    "deadline_ms": deadline,
                },
            )
        priority = _as_int(
            request.get("priority"),
            "E_BAD_REQUEST_INPUT",
            f"requests[{index}].priority",
        )
        input_tokens = _as_int(
            _first(request, ("input_tokens", "input_token_count")),
            "E_REQUEST_TOKEN_INVALID",
            f"requests[{index}].input_tokens",
            0,
        )
        output_tokens = _as_int(
            _first(request, ("max_output_tokens", "output_tokens", "max_output")),
            "E_REQUEST_TOKEN_INVALID",
            f"requests[{index}].max_output_tokens",
            0,
        )
        if input_tokens + output_tokens <= 0:
            raise BusinessError(
                "E_REQUEST_TOKEN_INVALID",
                "request input and output token count must be positive in aggregate",
                {"field": f"requests[{index}]", "request_seq": request_seq},
            )
        replica_id = request.get("replica_id")
        if replica_id is not None and (
            not isinstance(replica_id, str) or not replica_id
        ):
            raise BusinessError(
                "E_BAD_REQUEST_INPUT",
                "replica_id must be a non-empty string when present",
                {"field": f"requests[{index}].replica_id"},
            )
        simulate_failures = _as_int(
            _first(request, ("simulate_failures", "failure_plan"), 0),
            "E_BAD_REQUEST_INPUT",
            f"requests[{index}].simulate_failures",
            0,
        )
        normalized = {
            "request_seq": request_seq,
            "arrival_ms": arrival,
            "priority": priority,
            "input_tokens": input_tokens,
            "max_output_tokens": output_tokens,
            "deadline_ms": deadline,
            "replica_id": replica_id,
            "simulate_failures": simulate_failures,
        }
        requests.append(
            RequestSpec(
                request_seq=request_seq,
                arrival_ms=arrival,
                priority=priority,
                input_tokens=input_tokens,
                max_output_tokens=output_tokens,
                deadline_ms=deadline,
                replica_id=replica_id,
                simulate_failures=simulate_failures,
                raw=normalized,
            )
        )
    requests.sort(key=lambda request: request.request_seq)
    return tuple(requests)


def parse_precision(document: Any) -> PrecisionSpec:
    root = _mapping(document, "E_BAD_PRECISION_INPUT", "precision")
    nested = root.get("fixed_point", {})
    nested = _mapping(nested, "E_BAD_PRECISION_INPUT", "precision.fixed_point")

    def pick(names: tuple[str, ...], default: Any = None) -> Any:
        for name in names:
            if name in root:
                return root[name]
        for name in names:
            if name in nested:
                return nested[name]
        return default

    compute_scale = _as_int(
        pick(("compute_scale", "compute_fixed_scale", "compute_occupancy_scale")),
        "E_BAD_FIXED_SCALE",
        "compute_scale",
        1,
    )
    memory_scale = _as_int(
        pick(("memory_scale", "memory_fixed_scale", "memory_occupancy_scale")),
        "E_BAD_FIXED_SCALE",
        "memory_scale",
        1,
    )
    queue_scale = _as_int(
        pick(
            (
                "queue_scale",
                "queue_time_scale",
                "latency_scale",
                "queue_fixed_scale",
            )
        ),
        "E_BAD_FIXED_SCALE",
        "queue_scale",
        1,
    )
    tolerance = _as_positive_float(
        pick(("relative_tolerance", "tolerance")),
        "E_BAD_TOLERANCE",
        "relative_tolerance",
    )
    error_budget = _as_positive_float(
        pick(("error_bound_budget", "error_budget", "absolute_error_budget")),
        "E_BAD_ERROR_BUDGET",
        "error_bound_budget",
    )
    clock_step = _as_int(
        pick(("clock_step_ms", "logical_clock_step_ms", "logical_clock_step")),
        "E_BAD_CLOCK_STEP",
        "clock_step_ms",
        1,
    )
    normalized = {
        "compute_scale": compute_scale,
        "memory_scale": memory_scale,
        "queue_scale": queue_scale,
        "relative_tolerance": tolerance,
        "error_bound_budget": error_budget,
        "clock_step_ms": clock_step,
    }
    return PrecisionSpec(
        compute_scale=compute_scale,
        memory_scale=memory_scale,
        queue_scale=queue_scale,
        relative_tolerance=tolerance,
        error_bound_budget=error_budget,
        clock_step_ms=clock_step,
        raw=normalized,
    )


def validate_cross_constraints(
    replicas: ReplicaSnapshot | None,
    requests: tuple[RequestSpec, ...] | None,
) -> None:
    if replicas is None or requests is None:
        return
    replica_ids = set(replicas.by_id)
    max_total = max(request.total_tokens for request in requests)
    for replica in replicas.replicas:
        if replica.max_sequence_length < max_total:
            raise BusinessError(
                "E_SEQUENCE_LIMIT_TOO_SMALL",
                "replica max_sequence_length is smaller than a request token sum",
                {
                    "replica_id": replica.replica_id,
                    "max_sequence_length": replica.max_sequence_length,
                    "largest_request_token_sum": max_total,
                },
            )
    for request in requests:
        if request.replica_id is not None and request.replica_id not in replica_ids:
            raise BusinessError(
                "E_BAD_REQUEST_INPUT",
                "request references an unknown replica_id",
                {
                    "request_seq": request.request_seq,
                    "replica_id": request.replica_id,
                },
            )


def load_config_bundle(
    model_path: str | None,
    replicas_path: str | None,
    requests_path: str | None,
    precision_path: str | None,
) -> ConfigBundle:
    model = parse_model(load_json(model_path, "E_BAD_MODEL_INPUT")) if model_path else None
    replicas = (
        parse_replicas(load_json(replicas_path, "E_BAD_REPLICA_INPUT"))
        if replicas_path
        else None
    )
    requests = (
        parse_requests(load_json(requests_path, "E_BAD_REQUEST_INPUT"))
        if requests_path
        else None
    )
    precision = (
        parse_precision(load_json(precision_path, "E_BAD_PRECISION_INPUT"))
        if precision_path
        else None
    )
    validate_cross_constraints(replicas, requests)
    return ConfigBundle(model=model, replicas=replicas, requests=requests, precision=precision)

