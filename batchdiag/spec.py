from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping

from .errors import BusinessError
from .jsonutil import canonical_json, digest_json, read_json


SERVICE_CLASSES = ("gold", "silver", "bronze")
SERVICE_CLASS_RANK = {name: index for index, name in enumerate(SERVICE_CLASSES)}
METRICS = ("compute", "memory", "queue_wait")
SUPPORTED_ROUNDING = {"nearest_even"}


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _as_mapping(value: Any, code: str, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BusinessError(code, f"{label} must be a JSON object")
    return value


def _pick(mapping: Mapping[str, Any], names: Iterable[str]) -> Any:
    for name in names:
        if name in mapping:
            return mapping[name]
    return None


def _required(mapping: Mapping[str, Any], names: Iterable[str], code: str, label: str) -> Any:
    sentinel = object()
    for name in names:
        if name in mapping:
            return mapping[name]
    raise BusinessError(code, f"missing required field: {label}", {"accepted_names": list(names)})


def _positive_int(value: Any, code: str, label: str) -> int:
    if not _is_int(value) or value <= 0:
        raise BusinessError(code, f"{label} must be a positive integer", {"value": value})
    return value


def _nonnegative_int(value: Any, code: str, label: str) -> int:
    if not _is_int(value) or value < 0:
        raise BusinessError(code, f"{label} must be a non-negative integer", {"value": value})
    return value


def _finite_number(value: Any, code: str, label: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BusinessError(code, f"{label} must be a finite number", {"value": value})
    result = float(value)
    if not math.isfinite(result) or (positive and result <= 0):
        qualifier = "positive and finite" if positive else "finite"
        raise BusinessError(code, f"{label} must be {qualifier}", {"value": value})
    return result


def _decimal(value: Any, code: str, label: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise BusinessError(code, f"{label} must be a decimal number", {"value": value})
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise BusinessError(
            code, f"{label} must be a decimal number", {"value": value}
        ) from exc
    if not result.is_finite():
        raise BusinessError(code, f"{label} must be finite", {"value": value})
    if positive and result <= 0:
        raise BusinessError(code, f"{label} must be positive", {"value": value})
    return result


@dataclass(frozen=True)
class LayerSpec:
    name: str
    parameters: int

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "parameters": self.parameters}


@dataclass(frozen=True)
class ModelSpec:
    model_id: str
    layers: tuple[LayerSpec, ...]
    token_contract_input: int | None
    token_contract_output: int | None

    @property
    def total_parameters(self) -> int:
        return sum(layer.parameters for layer in self.layers)

    def as_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "model_id": self.model_id,
            "layers": [layer.as_dict() for layer in self.layers],
            "total_parameters": self.total_parameters,
        }
        if self.token_contract_input is not None:
            value["token_contract"] = {
                "input_tokens": self.token_contract_input,
                "output_tokens": self.token_contract_output,
            }
        return value


@dataclass(frozen=True)
class ReplicaSpec:
    replica_id: str
    compute_units: float
    memory_limit_mib: int
    max_batch_size: int
    max_sequence_length: int
    dispatch_quota: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "replica_id": self.replica_id,
            "compute_units": self.compute_units,
            "memory_limit_mib": self.memory_limit_mib,
            "max_batch_size": self.max_batch_size,
            "max_sequence_length": self.max_sequence_length,
            "dispatch_quota": self.dispatch_quota,
        }


@dataclass(frozen=True)
class RequestSpec:
    request_id: int
    arrival_ms: int
    service_class: str
    input_tokens: int
    output_tokens: int
    deadline_ms: int

    @property
    def sequence_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def as_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "arrival_ms": self.arrival_ms,
            "service_class": self.service_class,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "deadline_ms": self.deadline_ms,
        }


@dataclass(frozen=True)
class PrecisionConfig:
    clock_step_ms: int
    clock_rollback_tolerance_ms: int
    start_clock_ms: int
    scales: dict[str, int]
    rounding: str
    float_tolerance: Decimal
    error_budget: dict[str, Decimal]
    queue_threshold: int
    sample_ratio: Decimal
    formulas: dict[str, dict[str, Decimal]]

    def as_dict(self) -> dict[str, Any]:
        return {
            "clock_step_ms": self.clock_step_ms,
            "clock_rollback_tolerance_ms": self.clock_rollback_tolerance_ms,
            "start_clock_ms": self.start_clock_ms,
            "fixed_point": {
                metric: {
                    "scale": self.scales[metric],
                    "rounding": self.rounding,
                }
                for metric in METRICS
            },
            "float_tolerance": str(self.float_tolerance),
            "error_budget": {
                metric: str(self.error_budget[metric]) for metric in METRICS
            },
            "amplification": {"queue_threshold": self.queue_threshold},
            "diagnostic_sample_ratio": str(self.sample_ratio),
            "formulas": {
                metric: {key: str(value) for key, value in sorted(self.formulas[metric].items())}
                for metric in METRICS
            },
        }


@dataclass(frozen=True)
class WorkspaceSpec:
    model: ModelSpec
    replicas: tuple[ReplicaSpec, ...]
    requests: tuple[RequestSpec, ...]
    precision: PrecisionConfig

    @property
    def sample_hash(self) -> str:
        return digest_json(self.as_dict())

    @property
    def request_by_id(self) -> dict[int, RequestSpec]:
        return {request.request_id: request for request in self.requests}

    @property
    def replica_by_id(self) -> dict[str, ReplicaSpec]:
        return {replica.replica_id: replica for replica in self.replicas}

    def as_dict(self) -> dict[str, Any]:
        return {
            "model": self.model.as_dict(),
            "replicas": [replica.as_dict() for replica in self.replicas],
            "requests": [request.as_dict() for request in self.requests],
            "precision": self.precision.as_dict(),
        }

    def canonical(self) -> str:
        return canonical_json(self.as_dict())


def _validate_model(value: Any) -> ModelSpec:
    root = _as_mapping(value, "E_MODEL_SCHEMA", "model")
    if isinstance(root.get("model"), dict):
        root = root["model"]
    model_id = _required(root, ("model_id", "id", "name"), "E_MODEL_SCHEMA", "model_id")
    if not isinstance(model_id, str) or not model_id.strip():
        raise BusinessError("E_MODEL_ID", "model_id must be a non-empty string")
    layers_raw = _required(root, ("layers",), "E_MODEL_SCHEMA", "layers")
    if not isinstance(layers_raw, list) or not layers_raw:
        raise BusinessError("E_MODEL_SCHEMA", "layers must be a non-empty array")
    layers: list[LayerSpec] = []
    seen_names: set[str] = set()
    for index, layer_raw in enumerate(layers_raw):
        layer = _as_mapping(layer_raw, "E_MODEL_SCHEMA", f"layers[{index}]")
        name = _required(layer, ("name", "layer_name"), "E_MODEL_SCHEMA", "layer.name")
        parameters = _required(
            layer,
            ("parameters", "parameter_count", "params"),
            "E_MODEL_SCHEMA",
            "layer.parameters",
        )
        if not isinstance(name, str) or not name.strip():
            raise BusinessError(
                "E_LAYER_NAME", "layer name must be a non-empty string", {"index": index}
            )
        if name in seen_names:
            raise BusinessError(
                "E_LAYER_DUPLICATE", "layer names must be unique", {"name": name}
            )
        if not _is_int(parameters) or parameters < 0:
            raise BusinessError(
                "E_LAYER_PARAMETER",
                "layer parameters must be a non-negative integer",
                {"layer": name, "value": parameters},
            )
        seen_names.add(name)
        layers.append(LayerSpec(name=name, parameters=parameters))
    if sum(layer.parameters for layer in layers) <= 0:
        raise BusinessError("E_LAYER_PARAMETER", "total model parameters must be positive")

    contract = _pick(root, ("token_contract", "tokens"))
    input_tokens: int | None = None
    output_tokens: int | None = None
    if contract is not None:
        contract_map = _as_mapping(contract, "E_MODEL_SCHEMA", "token_contract")
        input_tokens = _positive_int(
            _required(
                contract_map,
                ("input_tokens", "input", "input_token_count"),
                "E_MODEL_SCHEMA",
                "token_contract.input_tokens",
            ),
            "E_MODEL_TOKENS",
            "token_contract.input_tokens",
        )
        output_tokens = _positive_int(
            _required(
                contract_map,
                ("output_tokens", "output", "output_token_count"),
                "E_MODEL_SCHEMA",
                "token_contract.output_tokens",
            ),
            "E_MODEL_TOKENS",
            "token_contract.output_tokens",
        )
    return ModelSpec(
        model_id=model_id,
        layers=tuple(layers),
        token_contract_input=input_tokens,
        token_contract_output=output_tokens,
    )


def _validate_replicas(value: Any) -> tuple[ReplicaSpec, ...]:
    if isinstance(value, dict):
        value = _required(value, ("replicas",), "E_REPLICAS_SCHEMA", "replicas")
    if not isinstance(value, list) or not value:
        raise BusinessError("E_REPLICAS_SCHEMA", "replicas must be a non-empty array")
    replicas: list[ReplicaSpec] = []
    seen: set[str] = set()
    for index, raw in enumerate(value):
        item = _as_mapping(raw, "E_REPLICAS_SCHEMA", f"replicas[{index}]")
        replica_id = _required(
            item, ("replica_id", "id"), "E_REPLICAS_SCHEMA", "replica_id"
        )
        if not isinstance(replica_id, str) or not replica_id.strip():
            raise BusinessError(
                "E_REPLICA_ID", "replica_id must be a non-empty string", {"index": index}
            )
        if replica_id in seen:
            raise BusinessError(
                "E_DUPLICATE_REPLICA", "replica IDs must be unique", {"replica_id": replica_id}
            )
        compute = _finite_number(
            _required(item, ("compute_units", "compute"), "E_REPLICAS_SCHEMA", "compute_units"),
            "E_REPLICA_COMPUTE",
            "compute_units",
            positive=True,
        )
        memory = _positive_int(
            _required(
                item,
                ("memory_limit_mib", "memory_mib", "vram_mib"),
                "E_REPLICAS_SCHEMA",
                "memory_limit_mib",
            ),
            "E_REPLICA_MEMORY",
            "memory_limit_mib",
        )
        max_batch = _positive_int(
            _required(
                item,
                ("max_batch_size", "max_batch"),
                "E_REPLICAS_SCHEMA",
                "max_batch_size",
            ),
            "E_REPLICA_MAX_BATCH",
            "max_batch_size",
        )
        max_sequence = _positive_int(
            _required(
                item,
                ("max_sequence_length", "max_sequence", "max_seq_len"),
                "E_REPLICAS_SCHEMA",
                "max_sequence_length",
            ),
            "E_REPLICA_MAX_SEQUENCE",
            "max_sequence_length",
        )
        quota = _positive_int(
            _required(
                item,
                ("dispatch_quota", "quota", "per_batch_quota"),
                "E_REPLICAS_SCHEMA",
                "dispatch_quota",
            ),
            "E_REPLICA_QUOTA",
            "dispatch_quota",
        )
        seen.add(replica_id)
        replicas.append(
            ReplicaSpec(
                replica_id=replica_id,
                compute_units=compute,
                memory_limit_mib=memory,
                max_batch_size=max_batch,
                max_sequence_length=max_sequence,
                dispatch_quota=quota,
            )
        )
    return tuple(replicas)


def _validate_requests(value: Any) -> tuple[RequestSpec, ...]:
    if isinstance(value, dict):
        value = _required(value, ("requests",), "E_REQUESTS_SCHEMA", "requests")
    if not isinstance(value, list) or not value:
        raise BusinessError("E_REQUESTS_SCHEMA", "requests must be a non-empty array")
    requests: list[RequestSpec] = []
    seen: set[int] = set()
    for index, raw in enumerate(value):
        item = _as_mapping(raw, "E_REQUESTS_SCHEMA", f"requests[{index}]")
        request_id = _required(
            item,
            ("request_id", "request_seq", "seq", "id"),
            "E_REQUESTS_SCHEMA",
            "request_id",
        )
        if not _is_int(request_id) or request_id < 0:
            raise BusinessError(
                "E_REQUEST_ID",
                "request_id must be a non-negative integer",
                {"index": index, "value": request_id},
            )
        if request_id in seen:
            raise BusinessError(
                "E_DUPLICATE_REQUEST",
                "request IDs must be unique",
                {"request_id": request_id},
            )
        arrival = _nonnegative_int(
            _required(
                item,
                ("arrival_ms", "arrive_ms", "arrival_logical_ms"),
                "E_REQUESTS_SCHEMA",
                "arrival_ms",
            ),
            "E_REQUEST_CLOCK",
            "arrival_ms",
        )
        deadline = _nonnegative_int(
            _required(
                item,
                ("deadline_ms", "deadline_logical_ms", "expected_deadline"),
                "E_REQUESTS_SCHEMA",
                "deadline_ms",
            ),
            "E_REQUEST_CLOCK",
            "deadline_ms",
        )
        if deadline < arrival:
            raise BusinessError(
                "E_REQUEST_DEADLINE",
                "deadline must not be earlier than arrival",
                {"request_id": request_id, "arrival_ms": arrival, "deadline_ms": deadline},
            )
        service_class = _required(
            item,
            ("service_class", "service_tier", "class", "sla"),
            "E_REQUESTS_SCHEMA",
            "service_class",
        )
        if not isinstance(service_class, str) or service_class not in SERVICE_CLASSES:
            raise BusinessError(
                "E_SERVICE_CLASS",
                "service_class is not in the closed enum",
                {"request_id": request_id, "value": service_class, "allowed": list(SERVICE_CLASSES)},
            )
        input_tokens = _positive_int(
            _required(
                item,
                ("input_tokens", "input_token_count"),
                "E_REQUESTS_SCHEMA",
                "input_tokens",
            ),
            "E_REQUEST_TOKENS",
            "input_tokens",
        )
        output_tokens = _positive_int(
            _required(
                item,
                ("output_tokens", "expected_output_tokens", "output_token_count"),
                "E_REQUESTS_SCHEMA",
                "output_tokens",
            ),
            "E_REQUEST_TOKENS",
            "output_tokens",
        )
        seen.add(request_id)
        requests.append(
            RequestSpec(
                request_id=request_id,
                arrival_ms=arrival,
                service_class=service_class,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                deadline_ms=deadline,
            )
        )
    requests.sort(key=lambda request: (request.arrival_ms, request.request_id))
    return tuple(requests)


def _scale_value(value: Any, metric: str) -> int:
    raw = value
    if isinstance(raw, dict):
        raw = _pick(raw, ("scale", "fixed_scale", "denominator"))
    normalized: int | None = None
    if _is_int(raw):
        normalized = raw
    elif isinstance(raw, float) and math.isfinite(raw) and raw.is_integer():
        normalized = int(raw)
    elif isinstance(raw, str):
        try:
            parsed = Decimal(raw)
            if parsed.is_finite() and parsed == parsed.to_integral_value():
                normalized = int(parsed)
        except (InvalidOperation, ValueError):
            normalized = None
    if (
        normalized is None
        or normalized <= 0
        or normalized > 10**12
        or str(normalized).strip("0") not in {"1"}
    ):
        raise BusinessError(
            "E_SCALE",
            "fixed-point scale must be a positive power of ten no greater than 1e12",
            {"metric": metric, "value": value},
        )
    return raw


def _validate_config(value: Any) -> PrecisionConfig:
    root = _as_mapping(value, "E_CONFIG_SCHEMA", "precision config")
    if isinstance(root.get("precision"), dict):
        root = root["precision"]
    step = _positive_int(
        _required(root, ("clock_step_ms", "step_ms"), "E_CONFIG_SCHEMA", "clock_step_ms"),
        "E_CLOCK_STEP",
        "clock_step_ms",
    )
    rollback_tolerance = _nonnegative_int(
        root.get("clock_rollback_tolerance_ms", 0),
        "E_CLOCK_STEP",
        "clock_rollback_tolerance_ms",
    )
    start_clock = _nonnegative_int(
        root.get("start_clock_ms", 0), "E_CLOCK_STEP", "start_clock_ms"
    )

    scales_raw = _required(
        root,
        ("fixed_point_scales", "scales", "fixed_point"),
        "E_CONFIG_SCHEMA",
        "fixed_point_scales",
    )
    scales_map = _as_mapping(scales_raw, "E_CONFIG_SCHEMA", "fixed_point_scales")
    scales: dict[str, int] = {}
    for metric in METRICS:
        candidate = _pick(scales_map, (metric, "wait" if metric == "queue_wait" else metric))
        if candidate is None:
            raise BusinessError(
                "E_SCALE", "missing fixed-point scale", {"metric": metric}
            )
        scales[metric] = _scale_value(candidate, metric)

    rounding = str(root.get("rounding_direction", root.get("rounding", "nearest_even")))
    if rounding not in SUPPORTED_ROUNDING:
        raise BusinessError(
            "E_ROUNDING",
            "unsupported rounding direction",
            {"value": rounding, "allowed": sorted(SUPPORTED_ROUNDING)},
        )

    tolerance = _decimal(
        _required(
            root,
            ("float_tolerance", "tolerance"),
            "E_CONFIG_SCHEMA",
            "float_tolerance",
        ),
        "E_TOLERANCE",
        "float_tolerance",
    )
    if tolerance < 0:
        raise BusinessError(
            "E_TOLERANCE", "float_tolerance must be non-negative"
        )
    budget_raw = _required(
        root,
        ("error_budget", "error_bounds"),
        "E_CONFIG_SCHEMA",
        "error_budget",
    )
    budget_map = _as_mapping(budget_raw, "E_CONFIG_SCHEMA", "error_budget")
    budgets: dict[str, Decimal] = {}
    for metric in METRICS:
        candidate = _pick(budget_map, (metric, "wait" if metric == "queue_wait" else metric))
        if candidate is None:
            raise BusinessError("E_ERROR_BUDGET", "missing error budget", {"metric": metric})
        budgets[metric] = _decimal(
            candidate, "E_ERROR_BUDGET", f"error_budget.{metric}", positive=True
        )

    amplification = root.get("amplification", {})
    if amplification is not None and not isinstance(amplification, dict):
        raise BusinessError("E_AMPLIFICATION_THRESHOLD", "amplification must be an object")
    threshold_raw = _pick(
        amplification or {},
        ("queue_threshold", "watermark_threshold", "threshold"),
    )
    if threshold_raw is None:
        threshold_raw = root.get("queue_threshold", 0)
    threshold = _nonnegative_int(
        threshold_raw, "E_AMPLIFICATION_THRESHOLD", "amplification.queue_threshold"
    )

    sample_ratio = _decimal(
        root.get("diagnostic_sample_ratio", root.get("sample_ratio", 1)),
        "E_SAMPLE_RATIO",
        "diagnostic_sample_ratio",
    )
    if sample_ratio < 0 or sample_ratio > 1:
        raise BusinessError(
            "E_SAMPLE_RATIO",
            "diagnostic_sample_ratio must be in [0, 1]",
            {"value": str(sample_ratio)},
        )

    formulas_raw = root.get("formulas")
    if formulas_raw is None:
        formulas_raw = {
            metric: root.get(metric)
            for metric in METRICS
            if isinstance(root.get(metric), dict)
        }
    formulas_map = _as_mapping(formulas_raw, "E_CONFIG_SCHEMA", "formulas")
    formulas: dict[str, dict[str, Decimal]] = {}
    aliases = {
        "base": ("base", "fixed_overhead", "overhead"),
        "input_token": ("input_token", "input_token_cost", "per_input_token"),
        "output_token": ("output_token", "output_token_cost", "per_output_token"),
        "parameter": ("parameter", "parameter_cost", "per_parameter"),
        "batch_size": ("batch_size", "batch_size_cost", "per_request"),
        "queue_depth": ("queue_depth", "queue_cost", "per_queued_request", "watermark"),
        "carry": ("carry", "carry_gain", "previous_batch"),
        "queue_carry": (
            "queue_carry",
            "queue_carry_gain",
            "watermark_multiplier",
            "queue_amplification",
        ),
    }
    required_terms = {
        "compute": ("base", "input_token", "output_token", "parameter", "queue_depth"),
        "memory": ("base", "input_token", "output_token", "parameter", "queue_depth"),
        "queue_wait": ("base", "queue_depth"),
    }
    for metric in METRICS:
        raw_formula = _required(formulas_map, (metric,), "E_CONFIG_SCHEMA", f"formulas.{metric}")
        formula_map = _as_mapping(raw_formula, "E_CONFIG_SCHEMA", f"formulas.{metric}")
        normalized: dict[str, Decimal] = {}
        for canonical_name, candidates in aliases.items():
            raw_term = _pick(formula_map, candidates)
            if raw_term is None:
                if canonical_name in required_terms[metric]:
                    raise BusinessError(
                        "E_FORMULA_PARAM",
                        "missing formula term",
                        {"metric": metric, "term": canonical_name},
                    )
                normalized[canonical_name] = Decimal(0)
                continue
            normalized[canonical_name] = _decimal(
                raw_term,
                "E_FORMULA_PARAM",
                f"formulas.{metric}.{canonical_name}",
            )
            if normalized[canonical_name] < 0:
                raise BusinessError(
                    "E_FORMULA_PARAM",
                    "formula terms must be non-negative",
                    {"metric": metric, "term": canonical_name},
                )
        formulas[metric] = normalized

    return PrecisionConfig(
        clock_step_ms=step,
        clock_rollback_tolerance_ms=rollback_tolerance,
        start_clock_ms=start_clock,
        scales=scales,
        rounding=rounding,
        float_tolerance=tolerance,
        error_budget=budgets,
        queue_threshold=threshold,
        sample_ratio=sample_ratio,
        formulas=formulas,
    )


def validate_workspace(
    model_value: Any,
    replicas_value: Any,
    requests_value: Any,
    config_value: Any,
) -> WorkspaceSpec:
    """Validate all four inputs before any persistent side effect."""
    model = _validate_model(model_value)
    replicas = _validate_replicas(replicas_value)
    requests = _validate_requests(requests_value)
    precision = _validate_config(config_value)

    for replica in replicas:
        for request in requests:
            if request.sequence_tokens > replica.max_sequence_length:
                raise BusinessError(
                    "E_SEQUENCE_LIMIT",
                    "replica max_sequence_length is smaller than a request input+output",
                    {
                        "replica_id": replica.replica_id,
                        "request_id": request.request_id,
                        "sequence_tokens": request.sequence_tokens,
                        "max_sequence_length": replica.max_sequence_length,
                    },
                )
    return WorkspaceSpec(
        model=model, replicas=replicas, requests=requests, precision=precision
    )


def load_workspace(
    model_path: str | Path,
    replicas_path: str | Path,
    requests_path: str | Path,
    config_path: str | Path,
) -> WorkspaceSpec:
    # Read all files first. Validation is intentionally completed before a caller
    # opens or creates a database.
    model_value = read_json(model_path)
    replicas_value = read_json(replicas_path)
    requests_value = read_json(requests_path)
    config_value = read_json(config_path)
    return validate_workspace(model_value, replicas_value, requests_value, config_value)
