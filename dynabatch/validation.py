"""Strict parsing and business validation for deterministic JSON bundles."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Iterable

from .canonical import digest_json
from .errors import (
    BusinessError,
    E_CONFIG_BUDGET,
    E_CONFIG_CONCURRENCY,
    E_JSON_INVALID,
    E_MODEL,
    E_PRECISION_SCALE,
    E_PRECISION_TOLERANCE,
    E_REPLICA_BATCH_SIZE,
    E_REPLICA_COMPUTE,
    E_REPLICA_CONCURRENCY,
    E_REPLICA_DUPLICATE,
    E_REPLICA_MEMORY,
    E_REPLICA_WINDOW,
    E_REQUEST_DEADLINE,
    E_REQUEST_DUPLICATE,
    E_REQUEST_SEQ,
    E_REQUEST_TIME,
    E_REQUEST_TOKENS,
    E_SCHEMA,
    E_SEQUENCE_LENGTH,
    E_VERSION,
    E_VERSION_DUPLICATE,
)
from .models import (
    Bundle,
    ExecutionConfig,
    LayerSpec,
    ModelSpec,
    PrecisionConfig,
    ReplicaSpec,
    ReplicaVersion,
    RequestSpec,
    SchedulerConfig,
)


def _raise(code: str, message: str, **details: Any) -> None:
    raise BusinessError(code, message, details)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(float(value))


def _positive_int(value: Any, code: str, field: str, *, allow_zero: bool = False) -> int:
    if not _is_int(value) or (value < 0 if allow_zero else value <= 0):
        qualifier = "non-negative" if allow_zero else "positive"
        _raise(code, f"{field} must be a {qualifier} integer", field=field, value=value)
    return value


def _positive_float(value: Any, code: str, field: str) -> float:
    if not _is_number(value) or float(value) <= 0.0:
        _raise(code, f"{field} must be a finite positive number", field=field, value=value)
    return float(value)


def _non_negative_int(value: Any, code: str, field: str) -> int:
    return _positive_int(value, code, field, allow_zero=True)


def _text(value: Any, field: str, default: str | None = None) -> str:
    if value is None and default is not None:
        return default
    if not isinstance(value, str) or not value.strip():
        _raise(E_SCHEMA, f"{field} must be a non-empty string", field=field, value=value)
    return value.strip()


def _mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _raise(E_SCHEMA, f"{field} must be an object", field=field)
    return value


def _sequence(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        _raise(E_SCHEMA, f"{field} must be an array", field=field)
    return value


def _pick(mapping: dict[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in mapping:
            return mapping[name]
    return default


def _parse_model(raw: Any) -> ModelSpec:
    data = _mapping(raw, "model")
    layers_raw = _sequence(data.get("layers"), "model.layers")
    if not layers_raw:
        _raise(E_MODEL, "model.layers must not be empty", field="model.layers")
    layers: list[LayerSpec] = []
    names: set[str] = set()
    for index, item in enumerate(layers_raw):
        layer = _mapping(item, f"model.layers[{index}]")
        name = _text(_pick(layer, "name", "layer"), f"model.layers[{index}].name")
        if name in names:
            _raise(E_MODEL, "model layer names must be unique", layer=name)
        names.add(name)
        params = _positive_int(
            _pick(layer, "parameters", "params", "parameter_count"),
            E_MODEL,
            f"model.layers[{index}].parameters",
            allow_zero=True,
        )
        layers.append(LayerSpec(name=name, parameters=params))
    if sum(layer.parameters for layer in layers) <= 0:
        _raise(E_MODEL, "model must have a positive total parameter count")
    input_tokens = _positive_int(
        _pick(data, "input_tokens", "per_request_input_tokens", default=0),
        E_MODEL,
        "model.input_tokens",
        allow_zero=True,
    )
    output_tokens = _positive_int(
        _pick(data, "output_tokens", "per_request_output_tokens", default=0),
        E_MODEL,
        "model.output_tokens",
        allow_zero=True,
    )
    bytes_per_parameter = _positive_int(
        _pick(data, "bytes_per_parameter", default=2),
        E_MODEL,
        "model.bytes_per_parameter",
    )
    activation_bytes = _positive_int(
        _pick(data, "activation_bytes_per_token", default=4096),
        E_MODEL,
        "model.activation_bytes_per_token",
    )
    latency_factor = _positive_float(
        _pick(data, "latency_ms_per_compute_unit", default=10.0),
        E_MODEL,
        "model.latency_ms_per_compute_unit",
    )
    return ModelSpec(
        model_id=_text(_pick(data, "model_id", "id"), "model.model_id", "model"),
        layers=tuple(layers),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        bytes_per_parameter=bytes_per_parameter,
        activation_bytes_per_token=activation_bytes,
        latency_ms_per_compute_unit=latency_factor,
    )


def _parse_replica(raw: Any, index: int) -> ReplicaSpec:
    data = _mapping(raw, f"replicas[{index}]")
    replica_id = _text(
        _pick(data, "replica_id", "id"), f"replicas[{index}].replica_id"
    )
    compute_units = _positive_float(
        _pick(data, "compute_units", "compute"),
        E_REPLICA_COMPUTE,
        f"replicas[{index}].compute_units",
    )
    memory_mib = _positive_int(
        _pick(data, "memory_mib", "gpu_memory_mib"),
        E_REPLICA_MEMORY,
        f"replicas[{index}].memory_mib",
    )
    batch_window_ms = _positive_int(
        _pick(data, "batch_window_ms", "window_ms"),
        E_REPLICA_WINDOW,
        f"replicas[{index}].batch_window_ms",
    )
    max_batch_size = _positive_int(
        _pick(data, "max_batch_size", "batch_size"),
        E_REPLICA_BATCH_SIZE,
        f"replicas[{index}].max_batch_size",
    )
    max_sequence_length = _positive_int(
        _pick(data, "max_sequence_length", "sequence_length_limit"),
        E_SEQUENCE_LENGTH,
        f"replicas[{index}].max_sequence_length",
    )
    max_concurrent_batches = _positive_int(
        _pick(data, "max_concurrent_batches", "max_concurrency"),
        E_REPLICA_CONCURRENCY,
        f"replicas[{index}].max_concurrent_batches",
    )
    return ReplicaSpec(
        replica_id=replica_id,
        compute_units=compute_units,
        memory_mib=memory_mib,
        batch_window_ms=batch_window_ms,
        max_batch_size=max_batch_size,
        max_sequence_length=max_sequence_length,
        max_concurrent_batches=max_concurrent_batches,
    )


def _parse_versions(
    raw: Any, requests: tuple[RequestSpec, ...]
) -> tuple[ReplicaVersion, ...]:
    versions_raw = _sequence(raw, "replica_versions")
    if not versions_raw:
        _raise(E_VERSION, "replica_versions must not be empty", field="replica_versions")
    versions: list[ReplicaVersion] = []
    seen_versions: set[int] = set()
    seen_effective: set[int] = set()
    max_sequence_required = max((request.total_tokens for request in requests), default=0)
    for index, item in enumerate(versions_raw):
        data = _mapping(item, f"replica_versions[{index}]")
        version = _positive_int(
            data.get("version"), E_VERSION, f"replica_versions[{index}].version"
        )
        if version in seen_versions:
            _raise(E_VERSION_DUPLICATE, "replica version numbers must be unique", version=version)
        seen_versions.add(version)
        effective_at_ms = _non_negative_int(
            _pick(data, "effective_at_ms", "effective_ms", default=0),
            E_VERSION,
            f"replica_versions[{index}].effective_at_ms",
        )
        if effective_at_ms in seen_effective:
            _raise(
                E_VERSION,
                "replica versions must have distinct effective_at_ms values",
                effective_at_ms=effective_at_ms,
            )
        seen_effective.add(effective_at_ms)
        replicas_raw = _sequence(data.get("replicas"), f"replica_versions[{index}].replicas")
        if not replicas_raw:
            _raise(E_VERSION, "replica version must contain at least one replica")
        replica_ids: set[str] = set()
        replicas: list[ReplicaSpec] = []
        for replica_index, replica_raw in enumerate(replicas_raw):
            replica = _parse_replica(replica_raw, replica_index)
            if replica.replica_id in replica_ids:
                _raise(
                    E_REPLICA_DUPLICATE,
                    "replica IDs must be unique within a version",
                    version=version,
                    replica_id=replica.replica_id,
                )
            replica_ids.add(replica.replica_id)
            if replica.max_sequence_length < max_sequence_required:
                _raise(
                    E_SEQUENCE_LENGTH,
                    "replica sequence limit is smaller than a request token sum",
                    version=version,
                    replica_id=replica.replica_id,
                    max_sequence_length=replica.max_sequence_length,
                    required=max_sequence_required,
                )
            replicas.append(replica)
        versions.append(
            ReplicaVersion(
                version=version,
                effective_at_ms=effective_at_ms,
                replicas=tuple(replicas),
                fingerprint=digest_json([replica.to_dict() for replica in replicas]),
            )
        )
    versions.sort(key=lambda item: item.version)
    ordered_by_effective = sorted(versions, key=lambda item: (item.effective_at_ms, item.version))
    if [item.version for item in versions] != [item.version for item in ordered_by_effective]:
        _raise(
            E_VERSION,
            "replica versions must be ordered consistently by version and effective time",
        )
    return tuple(versions)


def _parse_requests(raw: Any) -> tuple[RequestSpec, ...]:
    requests_raw = _sequence(raw, "requests")
    if not requests_raw:
        _raise(E_SCHEMA, "requests must not be empty", field="requests")
    requests: list[RequestSpec] = []
    seen: set[int] = set()
    for index, item in enumerate(requests_raw):
        data = _mapping(item, f"requests[{index}]")
        seq = _positive_int(
            _pick(data, "seq", "request_seq", "request_id"),
            E_REQUEST_SEQ,
            f"requests[{index}].seq",
        )
        if seq in seen:
            _raise(E_REQUEST_DUPLICATE, "request sequence numbers must be unique", seq=seq)
        seen.add(seq)
        arrival_ms = _non_negative_int(
            _pick(data, "arrival_ms", "arrival", "arrival_clock_ms"),
            E_REQUEST_TIME,
            f"requests[{index}].arrival_ms",
        )
        priority_raw = _pick(data, "priority", default=0)
        if not _is_int(priority_raw):
            _raise(E_SCHEMA, "request priority must be an integer", seq=seq)
        input_tokens = _positive_int(
            _pick(data, "input_tokens", "input_len"),
            E_REQUEST_TOKENS,
            f"requests[{index}].input_tokens",
        )
        output_tokens = _positive_int(
            _pick(data, "max_output_tokens", "output_tokens", "max_output_len"),
            E_REQUEST_TOKENS,
            f"requests[{index}].max_output_tokens",
        )
        deadline_ms = _non_negative_int(
            _pick(data, "deadline_ms", "expected_deadline_ms", "deadline_clock_ms"),
            E_REQUEST_TIME,
            f"requests[{index}].deadline_ms",
        )
        if deadline_ms < arrival_ms:
            _raise(
                E_REQUEST_DEADLINE,
                "request deadline must not be earlier than arrival",
                seq=seq,
                arrival_ms=arrival_ms,
                deadline_ms=deadline_ms,
            )
        requests.append(
            RequestSpec(
                seq=seq,
                arrival_ms=arrival_ms,
                priority=priority_raw,
                input_tokens=input_tokens,
                max_output_tokens=output_tokens,
                deadline_ms=deadline_ms,
            )
        )
    return tuple(sorted(requests, key=lambda item: (item.arrival_ms, item.seq)))


def _parse_precision(raw: Any) -> PrecisionConfig:
    if raw is None:
        raw = {}
    data = _mapping(raw, "precision")
    compute_scale = _positive_int(
        _pick(data, "compute_scale", "compute_fixed_scale"),
        E_PRECISION_SCALE,
        "precision.compute_scale",
    )
    memory_scale = _positive_int(
        _pick(data, "memory_scale", "memory_fixed_scale"),
        E_PRECISION_SCALE,
        "precision.memory_scale",
    )
    latency_scale = _positive_int(
        _pick(data, "latency_scale", "queue_scale", "delay_scale"),
        E_PRECISION_SCALE,
        "precision.latency_scale",
    )
    tolerance = _positive_float(
        data.get("tolerance"), E_PRECISION_TOLERANCE, "precision.tolerance"
    )
    rounding = _pick(data, "rounding", default="half_up")
    if rounding not in {"half_up", "half_even", "toward_zero"}:
        _raise(
            E_PRECISION_SCALE,
            "precision.rounding must be half_up, half_even, or toward_zero",
            rounding=rounding,
        )
    return PrecisionConfig(
        compute_scale=compute_scale,
        memory_scale=memory_scale,
        latency_scale=latency_scale,
        tolerance=tolerance,
        rounding=rounding,
    )


def _parse_scheduler(raw: Any) -> SchedulerConfig:
    if raw is None:
        raw = {}
    data = _mapping(raw, "scheduler")
    budget = _positive_int(
        _pick(data, "logical_time_budget_ms", "time_budget_ms", "round_time_budget_ms"),
        E_CONFIG_BUDGET,
        "scheduler.logical_time_budget_ms",
        allow_zero=True,
    )
    max_concurrent = _positive_int(
        _pick(data, "max_concurrent_batches", "max_batches_per_round"),
        E_CONFIG_CONCURRENCY,
        "scheduler.max_concurrent_batches",
    )
    return SchedulerConfig(
        logical_time_budget_ms=budget,
        max_concurrent_batches=max_concurrent,
    )


def _parse_execution(raw: Any) -> ExecutionConfig:
    if raw is None:
        raw = {}
    data = _mapping(raw, "execution")
    fail_batches_raw = _pick(data, "fail_batches", default=[])
    fail_batches = _sequence(fail_batches_raw, "execution.fail_batches")
    if not all(isinstance(item, str) and item for item in fail_batches):
        _raise(E_SCHEMA, "execution.fail_batches must contain non-empty strings")
    mode = _pick(data, "mode", default="deterministic")
    if mode not in {"deterministic", "success", "failure_injection"}:
        _raise(E_SCHEMA, "execution.mode is not supported", mode=mode)
    overhead = _non_negative_int(
        _pick(data, "latency_overhead_ms", default=0),
        E_SCHEMA,
        "execution.latency_overhead_ms",
    )
    return ExecutionConfig(
        mode=mode,
        fail_batches=tuple(fail_batches),
        latency_overhead_ms=overhead,
    )


def validate_bundle(raw: Any) -> Bundle:
    data = _mapping(raw, "bundle")
    schema_version = data.get("schema_version", 1)
    if schema_version != 1:
        _raise(E_SCHEMA, "unsupported schema_version", schema_version=schema_version)
    model = _parse_model(data.get("model"))
    requests = _parse_requests(data.get("requests"))
    versions = _parse_versions(data.get("replica_versions"), requests)
    precision = _parse_precision(data.get("precision"))
    scheduler = _parse_scheduler(data.get("scheduler"))
    execution = _parse_execution(data.get("execution"))
    return Bundle(
        model=model,
        replica_versions=versions,
        requests=requests,
        precision=precision,
        scheduler=scheduler,
        execution=execution,
        digest=digest_json(data),
        raw=data,
    )


def load_bundle(path: str | Path) -> Bundle:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        _raise(E_JSON_INVALID, "unable to read bundle", path=str(path), error=str(exc))
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        _raise(
            E_JSON_INVALID,
            "bundle is not valid JSON",
            path=str(path),
            line=exc.lineno,
            column=exc.colno,
        )
    return validate_bundle(raw)


def validate_many(items: Iterable[Any]) -> None:
    for item in items:
        validate_bundle(item)
