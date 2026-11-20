from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import Any, Iterable

from .errors import KernelError, fail
from .fslock import stable_json


MIB = 1024.0 * 1024.0


def _obj(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        fail("E_BAD_INPUT", f"{name} must be a JSON object")
    return value


def _list(value: Any, name: str) -> list[Any]:
    if not isinstance(value, list):
        fail("E_BAD_INPUT", f"{name} must be a JSON array")
    return value


def _str(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        fail("E_BAD_INPUT", f"{name} must be a non-empty string")
    return value


def _int(
    value: Any,
    name: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        fail("E_BAD_INPUT", f"{name} must be an integer")
    if minimum is not None and value < minimum:
        fail("E_BAD_INPUT", f"{name} must be >= {minimum}", value=value)
    if maximum is not None and value > maximum:
        fail("E_BAD_INPUT", f"{name} must be <= {maximum}", value=value)
    return value


def _positive_int(value: Any, name: str) -> int:
    return _int(value, name, minimum=1)


def _number(value: Any, name: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        fail("E_BAD_INPUT", f"{name} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        fail("E_BAD_INPUT", f"{name} must be finite")
    if positive and result <= 0.0:
        fail("E_BAD_INPUT", f"{name} must be positive", value=result)
    return result


def _hash_json(value: Any) -> str:
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class LayerSpec:
    layer_id: str
    param_count: int


@dataclass(frozen=True)
class ModelSpec:
    model_id: str
    layers: tuple[LayerSpec, ...]
    total_params: int
    batch_base_compute_units: float
    layer_param_units_per_token: float
    input_token_compute_units: float
    output_token_compute_units: float
    fixed_queue_latency_ms: float
    bytes_per_param: int
    activation_base_mib: float
    request_base_mib: float
    kv_input_mib_per_token: float
    kv_output_mib_per_token: float
    raw: dict[str, Any]
    content_hash: str
    weights_mib: float


@dataclass(frozen=True)
class ReplicaSpec:
    replica_id: str
    available_compute_units: float
    memory_limit_mib: int
    batch_window_ms: int
    max_batch_size: int
    max_sequence_length: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "replica_id": self.replica_id,
            "available_compute_units": self.available_compute_units,
            "memory_limit_mib": self.memory_limit_mib,
            "batch_window_ms": self.batch_window_ms,
            "max_batch_size": self.max_batch_size,
            "max_sequence_length": self.max_sequence_length,
        }


@dataclass(frozen=True)
class ReplicaManifest:
    version: int
    replicas: tuple[ReplicaSpec, ...]
    raw: dict[str, Any]
    content_hash: str

    @property
    def by_id(self) -> dict[str, ReplicaSpec]:
        return {item.replica_id: item for item in self.replicas}


@dataclass(frozen=True)
class RequestSpec:
    request_seq: int
    request_id: str
    arrival_ms: int
    priority: int
    input_tokens: int
    max_output_tokens: int
    deadline_ms: int

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.max_output_tokens

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_seq": self.request_seq,
            "request_id": self.request_id,
            "arrival_ms": self.arrival_ms,
            "priority": self.priority,
            "input_tokens": self.input_tokens,
            "max_output_tokens": self.max_output_tokens,
            "deadline_ms": self.deadline_ms,
        }


@dataclass(frozen=True)
class RequestTrace:
    requests: tuple[RequestSpec, ...]
    raw: dict[str, Any]
    content_hash: str


@dataclass(frozen=True)
class PrecisionConfig:
    compute_scale: int
    memory_scale: int
    queue_ms_scale: int
    float_tolerance: float
    error_budget: float
    logical_clock_step_ms: int
    rounding_mode: str
    raw: dict[str, Any]
    content_hash: str


@dataclass(frozen=True)
class Dataset:
    model: ModelSpec
    replicas: ReplicaManifest
    requests: RequestTrace
    precision: PrecisionConfig
    candidates: dict[int, tuple[str, ...]]


def parse_model(value: Any) -> ModelSpec:
    obj = _obj(value, "model")
    model_id = _str(obj.get("model_id"), "model.model_id")
    layers_raw = _list(obj.get("layers"), "model.layers")
    if not layers_raw:
        fail("E_BAD_MODEL", "model.layers must not be empty")
    layers: list[LayerSpec] = []
    seen_layers: set[str] = set()
    total_params = 0
    for index, item in enumerate(layers_raw):
        layer = _obj(item, f"model.layers[{index}]")
        layer_id = _str(layer.get("layer_id"), f"model.layers[{index}].layer_id")
        if layer_id in seen_layers:
            fail("E_BAD_MODEL", "duplicate layer_id", layer_id=layer_id)
        seen_layers.add(layer_id)
        param_count = _positive_int(
            layer.get("param_count"), f"model.layers[{index}].param_count"
        )
        total_params += param_count
        layers.append(LayerSpec(layer_id=layer_id, param_count=param_count))

    compute = _obj(obj.get("compute"), "model.compute")
    memory = _obj(obj.get("memory"), "model.memory")
    queue = _obj(obj.get("queue", {}), "model.queue")
    bytes_per_param = _positive_int(
        memory.get("bytes_per_param", 2), "model.memory.bytes_per_param"
    )
    spec = ModelSpec(
        model_id=model_id,
        layers=tuple(layers),
        total_params=total_params,
        batch_base_compute_units=_number(
            compute.get("batch_base_units"), "model.compute.batch_base_units"
        ),
        layer_param_units_per_token=_number(
            compute.get("layer_param_units_per_token"),
            "model.compute.layer_param_units_per_token",
            positive=True,
        ),
        input_token_compute_units=_number(
            compute.get("input_token_units"),
            "model.compute.input_token_units",
            positive=True,
        ),
        output_token_compute_units=_number(
            compute.get("output_token_units"),
            "model.compute.output_token_units",
            positive=True,
        ),
        fixed_queue_latency_ms=_number(
            queue.get("fixed_latency_ms", 0.0),
            "model.queue.fixed_latency_ms",
        ),
        bytes_per_param=bytes_per_param,
        activation_base_mib=_number(
            memory.get("activation_base_mib"),
            "model.memory.activation_base_mib",
        ),
        request_base_mib=_number(
            memory.get("request_base_mib"), "model.memory.request_base_mib"
        ),
        kv_input_mib_per_token=_number(
            memory.get("kv_input_mib_per_token"),
            "model.memory.kv_input_mib_per_token",
        ),
        kv_output_mib_per_token=_number(
            memory.get("kv_output_mib_per_token"),
            "model.memory.kv_output_mib_per_token",
        ),
        raw=obj,
        content_hash=_hash_json(obj),
        weights_mib=(total_params * bytes_per_param) / MIB,
    )
    if spec.activation_base_mib < 0 or spec.request_base_mib < 0:
        fail("E_BAD_MODEL", "memory base values must be non-negative")
    if spec.kv_input_mib_per_token < 0 or spec.kv_output_mib_per_token < 0:
        fail("E_BAD_MODEL", "KV memory rates must be non-negative")
    return spec


def parse_replicas(value: Any) -> ReplicaManifest:
    obj = _obj(value, "replicas")
    version = _positive_int(obj.get("version"), "replicas.version")
    items = _list(obj.get("replicas"), "replicas.replicas")
    if not items:
        fail("E_BAD_REPLICA", "replica list must not be empty")
    replicas: list[ReplicaSpec] = []
    seen: set[str] = set()
    for index, item in enumerate(items):
        raw = _obj(item, f"replicas.replicas[{index}]")
        replica_id = _str(
            raw.get("replica_id"), f"replicas.replicas[{index}].replica_id"
        )
        if replica_id in seen:
            fail("E_DUP_REPLICA", "duplicate replica_id", replica_id=replica_id)
        seen.add(replica_id)
        replicas.append(
            ReplicaSpec(
                replica_id=replica_id,
                available_compute_units=_number(
                    raw.get("available_compute_units"),
                    f"replicas.replicas[{index}].available_compute_units",
                    positive=True,
                ),
                memory_limit_mib=_positive_int(
                    raw.get("memory_limit_mib"),
                    f"replicas.replicas[{index}].memory_limit_mib",
                ),
                batch_window_ms=_positive_int(
                    raw.get("batch_window_ms"),
                    f"replicas.replicas[{index}].batch_window_ms",
                ),
                max_batch_size=_positive_int(
                    raw.get("max_batch_size"),
                    f"replicas.replicas[{index}].max_batch_size",
                ),
                max_sequence_length=_positive_int(
                    raw.get("max_sequence_length"),
                    f"replicas.replicas[{index}].max_sequence_length",
                ),
            )
        )
    replicas.sort(key=lambda item: item.replica_id)
    return ReplicaManifest(
        version=version,
        replicas=tuple(replicas),
        raw=obj,
        content_hash=_hash_json(obj),
    )


def parse_requests(value: Any) -> RequestTrace:
    obj = _obj(value, "requests")
    items = _list(obj.get("requests"), "requests.requests")
    if not items:
        fail("E_BAD_REQ", "request trace must not be empty")
    requests: list[RequestSpec] = []
    seqs: set[int] = set()
    ids: set[str] = set()
    for index, item in enumerate(items):
        raw = _obj(item, f"requests.requests[{index}]")
        request_seq = _int(
            raw.get("request_seq"),
            f"requests.requests[{index}].request_seq",
            minimum=0,
        )
        request_id = _str(
            raw.get("request_id"), f"requests.requests[{index}].request_id"
        )
        if request_seq in seqs:
            fail(
                "E_DUP_REQ",
                "duplicate request_seq",
                request_seq=request_seq,
            )
        if request_id in ids:
            fail("E_DUP_REQ", "duplicate request_id", request_id=request_id)
        seqs.add(request_seq)
        ids.add(request_id)
        arrival = _int(
            raw.get("arrival_ms"),
            f"requests.requests[{index}].arrival_ms",
            minimum=0,
        )
        deadline = _int(
            raw.get("deadline_ms"),
            f"requests.requests[{index}].deadline_ms",
            minimum=0,
        )
        if deadline < arrival:
            fail(
                "E_BAD_REQ",
                "deadline_ms must not be earlier than arrival_ms",
                request_id=request_id,
                arrival_ms=arrival,
                deadline_ms=deadline,
            )
        requests.append(
            RequestSpec(
                request_seq=request_seq,
                request_id=request_id,
                arrival_ms=arrival,
                priority=_int(
                    raw.get("priority"),
                    f"requests.requests[{index}].priority",
                    minimum=0,
                    maximum=255,
                ),
                input_tokens=_positive_int(
                    raw.get("input_tokens"),
                    f"requests.requests[{index}].input_tokens",
                ),
                max_output_tokens=_int(
                    raw.get("max_output_tokens"),
                    f"requests.requests[{index}].max_output_tokens",
                    minimum=0,
                ),
                deadline_ms=deadline,
            )
        )
    requests.sort(key=lambda item: (item.arrival_ms, item.request_seq))
    return RequestTrace(
        requests=tuple(requests),
        raw=obj,
        content_hash=_hash_json(obj),
    )


def parse_precision(value: Any) -> PrecisionConfig:
    obj = _obj(value, "precision")
    scales = _obj(obj.get("fixed_scales"), "precision.fixed_scales")
    rounding = obj.get("rounding", "half_even")
    if rounding not in ("half_even", "half_up", "floor", "ceiling"):
        fail(
            "E_BAD_PRECISION",
            "precision.rounding is unsupported",
            rounding=rounding,
        )
    return PrecisionConfig(
        compute_scale=_positive_int(
            scales.get("compute"), "precision.fixed_scales.compute"
        ),
        memory_scale=_positive_int(
            scales.get("memory"), "precision.fixed_scales.memory"
        ),
        queue_ms_scale=_positive_int(
            scales.get("queue_ms"), "precision.fixed_scales.queue_ms"
        ),
        float_tolerance=_number(
            obj.get("float_tolerance"), "precision.float_tolerance"
        ),
        error_budget=_number(obj.get("error_budget"), "precision.error_budget"),
        logical_clock_step_ms=_positive_int(
            obj.get("logical_clock_step_ms"),
            "precision.logical_clock_step_ms",
        ),
        rounding_mode=rounding,
        raw=obj,
        content_hash=_hash_json(obj),
    )


def estimate_individual(
    request: RequestSpec, replica: ReplicaSpec, model: ModelSpec
) -> tuple[float, float]:
    layer_cost = (
        model.total_params
        * model.layer_param_units_per_token
        * request.total_tokens
    )
    compute = (
        model.batch_base_compute_units
        + layer_cost
        + request.input_tokens * model.input_token_compute_units
        + request.max_output_tokens * model.output_token_compute_units
    )
    memory = (
        model.weights_mib
        + model.activation_base_mib
        + model.request_base_mib
        + request.input_tokens * model.kv_input_mib_per_token
        + request.max_output_tokens * model.kv_output_mib_per_token
    )
    return compute, memory


def build_candidates(
    model: ModelSpec,
    replicas: ReplicaManifest,
    requests: Iterable[RequestSpec],
) -> dict[int, tuple[str, ...]]:
    result: dict[int, tuple[str, ...]] = {}
    for request in requests:
        viable: list[str] = []
        reasons: list[dict[str, Any]] = []
        for replica in replicas.replicas:
            compute, memory = estimate_individual(request, replica, model)
            if request.total_tokens > replica.max_sequence_length:
                reasons.append(
                    {
                        "replica_id": replica.replica_id,
                        "constraint": "SEQUENCE_LENGTH",
                    }
                )
            elif compute > replica.available_compute_units:
                reasons.append(
                    {
                        "replica_id": replica.replica_id,
                        "constraint": "COMPUTE_BUDGET",
                    }
                )
            elif memory > float(replica.memory_limit_mib):
                reasons.append(
                    {
                        "replica_id": replica.replica_id,
                        "constraint": "MEMORY_BUDGET",
                    }
                )
            else:
                viable.append(replica.replica_id)
        if not viable:
            fail(
                "E_REQ_NO_REPLICA",
                "request has no viable replica",
                request_id=request.request_id,
                request_seq=request.request_seq,
                total_tokens=request.total_tokens,
                rejected=reasons,
            )
        result[request.request_seq] = tuple(sorted(viable))
    return result


def validate_dataset(
    model_value: Any,
    replica_value: Any,
    request_value: Any,
    precision_value: Any,
) -> Dataset:
    try:
        model = parse_model(model_value)
    except KernelError as exc:
        if exc.code == "E_BAD_INPUT":
            exc.code = "E_BAD_MODEL"
        raise
    try:
        replicas = parse_replicas(replica_value)
    except KernelError as exc:
        if exc.code == "E_BAD_INPUT":
            exc.code = "E_BAD_REPLICA"
        raise
    try:
        requests = parse_requests(request_value)
    except KernelError as exc:
        if exc.code == "E_BAD_INPUT":
            exc.code = "E_BAD_REQ"
        raise
    try:
        precision = parse_precision(precision_value)
    except KernelError as exc:
        if exc.code == "E_BAD_INPUT":
            exc.code = "E_BAD_PRECISION"
        raise
    candidates = build_candidates(model, replicas, requests.requests)
    return Dataset(
        model=model,
        replicas=replicas,
        requests=requests,
        precision=precision,
        candidates=candidates,
    )

