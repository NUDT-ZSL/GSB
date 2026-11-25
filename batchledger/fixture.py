"""Fixture loading and strict, side-effect-free validation."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any, Iterable

from .errors import BusinessError, ValidationError
from .models import (
    AccuracySpec,
    Fixture,
    InjectionSpec,
    LayerSpec,
    ModelSpec,
    ReplicaSpec,
    RequestSpec,
)

SCHEMA_VERSION = 1
INJECTION_PHASES = {
    "window_open",
    "batch_adjudication",
    "batch_commit",
    "window_commit",
    "batch_execute",
}
ROUNDING_MODES = {"half_up", "floor", "ceil"}


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _issue(code: str, path: str, message: str, value: Any = None) -> dict[str, Any]:
    result: dict[str, Any] = {"code": code, "path": path, "message": message}
    if value is not None:
        if isinstance(value, float) and not math.isfinite(value):
            value = repr(value)
        result["value"] = value
    return result


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _positive_finite(
    value: Any, path: str, code: str, issues: list[dict[str, Any]]
) -> float | None:
    numeric: float | None = None
    if _is_number(value):
        try:
            numeric = float(value)
        except (OverflowError, ValueError):
            numeric = None
    if numeric is None or not math.isfinite(numeric) or numeric <= 0:
        issues.append(_issue(code, path, "must be a positive finite number", value))
        return None
    return numeric


def _nonnegative_finite(
    value: Any, path: str, code: str, issues: list[dict[str, Any]]
) -> float | None:
    numeric: float | None = None
    if _is_number(value):
        try:
            numeric = float(value)
        except (OverflowError, ValueError):
            numeric = None
    if numeric is None or not math.isfinite(numeric) or numeric < 0:
        issues.append(_issue(code, path, "must be a non-negative finite number", value))
        return None
    return numeric


def _positive_int(
    value: Any, path: str, code: str, issues: list[dict[str, Any]]
) -> int | None:
    if not _is_int(value) or value <= 0:
        issues.append(_issue(code, path, "must be a positive integer", value))
        return None
    return value


def _nonnegative_int(
    value: Any, path: str, code: str, issues: list[dict[str, Any]]
) -> int | None:
    if not _is_int(value) or value < 0:
        issues.append(_issue(code, path, "must be a non-negative integer", value))
        return None
    return value


def _required(
    obj: dict[str, Any],
    names: Iterable[str],
    path: str,
    code: str,
    issues: list[dict[str, Any]],
) -> tuple[Any, str] | tuple[None, None]:
    for name in names:
        if name in obj:
            return obj[name], name
    issues.append(
        _issue(
            code,
            path,
            "missing required field; accepted names: " + ", ".join(names),
        )
    )
    return None, None


def _read_json_file(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise BusinessError(
            "E_FIXTURE_READ", f"cannot read fixture file {path}: {exc}"
        ) from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValidationError(
            [
                _issue(
                    "E_JSON_INVALID",
                    str(path),
                    f"invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}",
                )
            ]
        ) from exc


def _load_component_bundle(path: Path) -> dict[str, Any]:
    if path.is_dir():
        manifest_path = path / "manifest.json"
        if manifest_path.exists():
            manifest = _read_json_file(manifest_path)
            if not isinstance(manifest, dict):
                raise ValidationError(
                    [
                        _issue(
                            "E_MANIFEST_INVALID",
                            str(manifest_path),
                            "manifest must be a JSON object",
                        )
                    ]
                )
            bundle: dict[str, Any] = {}
            if "schema_version" in manifest:
                bundle["schema_version"] = manifest["schema_version"]
            for key, default_name in (
                ("model", "model.json"),
                ("replicas", "replicas.json"),
                ("requests", "requests.json"),
                ("accuracy", "accuracy.json"),
                ("injections", "injections.json"),
            ):
                if key in manifest and isinstance(manifest[key], (dict, list)):
                    bundle[key] = manifest[key]
                    continue
                relative = manifest.get(key, default_name)
                if key == "injections" and not (path / relative).exists():
                    bundle[key] = []
                    continue
                component_path = path / str(relative)
                if not component_path.exists():
                    raise BusinessError(
                        "E_FIXTURE_MISSING",
                        f"missing fixture component: {component_path}",
                    )
                bundle[key] = _read_json_file(component_path)
            return bundle

        bundle = {}
        for key, name in (
            ("model", "model.json"),
            ("replicas", "replicas.json"),
            ("requests", "requests.json"),
            ("accuracy", "accuracy.json"),
        ):
            component_path = path / name
            if not component_path.exists():
                raise BusinessError(
                    "E_FIXTURE_MISSING", f"missing fixture component: {component_path}"
                )
            bundle[key] = _read_json_file(component_path)
        injection_path = path / "injections.json"
        bundle["injections"] = (
            _read_json_file(injection_path) if injection_path.exists() else []
        )
        return bundle

    data = _read_json_file(path)
    if not isinstance(data, dict):
        raise ValidationError(
            [_issue("E_FIXTURE_INVALID", str(path), "fixture must be a JSON object")]
        )
    if all(key in data for key in ("model", "replicas", "requests", "accuracy")):
        data.setdefault("injections", [])
        return data

    referenced = False
    bundle = {}
    for key, default_name in (
        ("model", "model.json"),
        ("replicas", "replicas.json"),
        ("requests", "requests.json"),
        ("accuracy", "accuracy.json"),
        ("injections", "injections.json"),
    ):
        if key not in data:
            continue
        value = data[key]
        if isinstance(value, str):
            referenced = True
            component_path = (path.parent / value).resolve()
            bundle[key] = _read_json_file(component_path)
        else:
            bundle[key] = value
    if referenced and all(key in bundle for key in ("model", "replicas", "requests", "accuracy")):
        bundle.setdefault("injections", [])
        return bundle
    raise ValidationError(
        [
            _issue(
                "E_FIXTURE_INVALID",
                str(path),
                "expected a component bundle or a manifest referencing model/replicas/requests/accuracy",
            )
        ]
    )


def _parse_layers(raw: Any, issues: list[dict[str, Any]]) -> tuple[LayerSpec, ...]:
    layers: list[LayerSpec] = []
    if isinstance(raw, dict):
        items = [{"name": name, "params": params} for name, params in raw.items()]
    elif isinstance(raw, list):
        items = raw
    else:
        issues.append(_issue("E_MODEL_LAYERS_INVALID", "model.layers", "must be a list or object"))
        return ()
    names: set[str] = set()
    for index, item in enumerate(items):
        path = f"model.layers[{index}]"
        if not isinstance(item, dict):
            issues.append(_issue("E_MODEL_LAYER_INVALID", path, "must be an object"))
            continue
        name, _ = _required(item, ("name", "layer_name"), path, "E_MODEL_LAYER_NAME_INVALID", issues)
        if not isinstance(name, str) or not name.strip():
            issues.append(_issue("E_MODEL_LAYER_NAME_INVALID", f"{path}.name", "must be a non-empty string"))
            continue
        if name in names:
            issues.append(_issue("E_MODEL_LAYER_NAME_DUPLICATE", f"{path}.name", "duplicate layer name", name))
            continue
        names.add(name)
        params, _ = _required(item, ("params", "parameters", "param_count"), path, "E_MODEL_LAYER_PARAMS_INVALID", issues)
        if not _is_int(params) or params < 0:
            issues.append(_issue("E_MODEL_LAYER_PARAMS_INVALID", f"{path}.params", "must be a non-negative integer", params))
            continue
        layers.append(LayerSpec(name=name, params=params))
    if not layers and not issues:
        issues.append(_issue("E_MODEL_LAYERS_INVALID", "model.layers", "must contain at least one layer"))
    return tuple(layers)


def _parse_model(raw: Any, issues: list[dict[str, Any]]) -> ModelSpec | None:
    if not isinstance(raw, dict):
        issues.append(_issue("E_MODEL_INVALID", "model", "must be an object"))
        return None
    model_id, _ = _required(raw, ("model_id", "id", "name"), "model", "E_MODEL_ID_INVALID", issues)
    if not isinstance(model_id, str) or not model_id.strip():
        issues.append(_issue("E_MODEL_ID_INVALID", "model.model_id", "must be a non-empty string", model_id))
        model_id = None
    layers_raw, _ = _required(raw, ("layers",), "model", "E_MODEL_LAYERS_INVALID", issues)
    layers = _parse_layers(layers_raw, issues) if layers_raw is not None else ()
    flops, _ = _required(
        raw,
        ("flops_per_parameter_per_token", "flops_per_param_per_token"),
        "model",
        "E_MODEL_FLOPS_INVALID",
        issues,
    )
    flops_value = _positive_finite(
        flops, "model.flops_per_parameter_per_token", "E_MODEL_FLOPS_INVALID", issues
    )
    memory_base, _ = _required(
        raw,
        ("memory_base_mib", "base_memory_mib"),
        "model",
        "E_MODEL_MEMORY_BASE_INVALID",
        issues,
    )
    memory_base_value = _nonnegative_finite(
        memory_base, "model.memory_base_mib", "E_MODEL_MEMORY_BASE_INVALID", issues
    )
    kv, _ = _required(
        raw,
        ("kv_mib_per_token", "kv_cache_mib_per_token"),
        "model",
        "E_MODEL_KV_INVALID",
        issues,
    )
    kv_value = _nonnegative_finite(kv, "model.kv_mib_per_token", "E_MODEL_KV_INVALID", issues)
    activation, _ = _required(
        raw,
        ("activation_mib_per_token", "activation_memory_mib_per_token"),
        "model",
        "E_MODEL_ACTIVATION_INVALID",
        issues,
    )
    activation_value = _nonnegative_finite(
        activation,
        "model.activation_mib_per_token",
        "E_MODEL_ACTIVATION_INVALID",
        issues,
    )
    overhead, _ = _required(
        raw,
        ("batch_overhead_ms", "batch_overhead_logical_ms"),
        "model",
        "E_MODEL_OVERHEAD_INVALID",
        issues,
    )
    overhead_value = _nonnegative_finite(
        overhead, "model.batch_overhead_ms", "E_MODEL_OVERHEAD_INVALID", issues
    )
    if (
        model_id is None
        or not layers
        or flops_value is None
        or memory_base_value is None
        or kv_value is None
        or activation_value is None
        or overhead_value is None
    ):
        return None
    if sum(layer.params for layer in layers) <= 0:
        issues.append(_issue("E_MODEL_PARAMS_INVALID", "model.layers", "total parameter count must be positive"))
        return None
    return ModelSpec(
        model_id=model_id,
        layers=layers,
        flops_per_parameter_per_token=flops_value,
        memory_base_mib=memory_base_value,
        kv_mib_per_token=kv_value,
        activation_mib_per_token=activation_value,
        batch_overhead_ms=overhead_value,
    )


def _parse_replicas(raw: Any, issues: list[dict[str, Any]]) -> tuple[ReplicaSpec, ...]:
    if isinstance(raw, dict):
        items = []
        for replica_id, value in raw.items():
            if not isinstance(value, dict):
                issues.append(_issue("E_REPLICA_INVALID", f"replicas.{replica_id}", "must be an object"))
                continue
            item = dict(value)
            item.setdefault("replica_id", replica_id)
            items.append(item)
    elif isinstance(raw, list):
        items = raw
    else:
        issues.append(_issue("E_REPLICA_INVALID", "replicas", "must be a non-empty list"))
        return ()
    replicas: list[ReplicaSpec] = []
    ids: set[str] = set()
    for index, item in enumerate(items):
        path = f"replicas[{index}]"
        if not isinstance(item, dict):
            issues.append(_issue("E_REPLICA_INVALID", path, "must be an object"))
            continue
        replica_id, _ = _required(item, ("replica_id", "id"), path, "E_REPLICA_ID_INVALID", issues)
        if not isinstance(replica_id, str) or not replica_id.strip():
            issues.append(_issue("E_REPLICA_ID_INVALID", f"{path}.replica_id", "must be a non-empty string", replica_id))
            continue
        if replica_id in ids:
            issues.append(_issue("E_REPLICA_ID_DUPLICATE", f"{path}.replica_id", "duplicate replica id", replica_id))
            continue
        ids.add(replica_id)
        compute_raw, _ = _required(
            item,
            ("compute_capacity", "available_compute", "compute"),
            path,
            "E_REPLICA_COMPUTE_INVALID",
            issues,
        )
        compute = _positive_finite(
            compute_raw, f"{path}.compute_capacity", "E_REPLICA_COMPUTE_INVALID", issues
        )
        memory_raw, _ = _required(
            item,
            ("memory_limit_mib", "memory_limit", "max_memory_mib"),
            path,
            "E_REPLICA_MEMORY_INVALID",
            issues,
        )
        memory = _positive_finite(
            memory_raw, f"{path}.memory_limit_mib", "E_REPLICA_MEMORY_INVALID", issues
        )
        window_raw, _ = _required(
            item,
            ("window_ms", "batch_window_ms", "batch_window_logical_ms"),
            path,
            "E_REPLICA_WINDOW_INVALID",
            issues,
        )
        window = _positive_int(window_raw, f"{path}.window_ms", "E_REPLICA_WINDOW_INVALID", issues)
        batch_raw, _ = _required(
            item,
            ("max_batch_size", "batch_size_limit"),
            path,
            "E_REPLICA_MAX_BATCH_INVALID",
            issues,
        )
        max_batch = _positive_int(
            batch_raw, f"{path}.max_batch_size", "E_REPLICA_MAX_BATCH_INVALID", issues
        )
        sequence_raw, _ = _required(
            item,
            ("max_sequence_length", "sequence_length_limit", "max_seq_len"),
            path,
            "E_REPLICA_SEQUENCE_INVALID",
            issues,
        )
        max_sequence = _positive_int(
            sequence_raw,
            f"{path}.max_sequence_length",
            "E_REPLICA_SEQUENCE_INVALID",
            issues,
        )
        if (
            compute is None
            or memory is None
            or window is None
            or max_batch is None
            or max_sequence is None
        ):
            continue
        replicas.append(
            ReplicaSpec(
                replica_id=replica_id,
                compute_capacity=compute,
                memory_limit_mib=memory,
                window_ms=window,
                max_batch_size=max_batch,
                max_sequence_length=max_sequence,
            )
        )
    if not replicas and not issues:
        issues.append(_issue("E_REPLICA_INVALID", "replicas", "must contain at least one replica"))
    return tuple(sorted(replicas, key=lambda replica: replica.replica_id))


def _parse_requests(raw: Any, issues: list[dict[str, Any]]) -> tuple[RequestSpec, ...]:
    if not isinstance(raw, list):
        issues.append(_issue("E_REQUESTS_INVALID", "requests", "must be a list"))
        return ()
    requests: list[RequestSpec] = []
    ids: set[int] = set()
    for index, item in enumerate(raw):
        path = f"requests[{index}]"
        if not isinstance(item, dict):
            issues.append(_issue("E_REQUEST_INVALID", path, "must be an object"))
            continue
        request_id, _ = _required(
            item, ("request_id", "serial", "id"), path, "E_REQUEST_ID_INVALID", issues
        )
        if not _is_int(request_id) or request_id < 0:
            issues.append(
                _issue(
                    "E_REQUEST_ID_INVALID",
                    f"{path}.request_id",
                    "must be a non-negative integer",
                    request_id,
                )
            )
            continue
        if request_id in ids:
            issues.append(
                _issue(
                    "E_REQUEST_ID_DUPLICATE",
                    f"{path}.request_id",
                    "duplicate request id",
                    request_id,
                )
            )
            continue
        ids.add(request_id)
        arrival_raw, _ = _required(
            item,
            ("arrival_ms", "arrival_clock_ms", "arrival"),
            path,
            "E_REQUEST_ARRIVAL_INVALID",
            issues,
        )
        arrival = _nonnegative_int(
            arrival_raw, f"{path}.arrival_ms", "E_REQUEST_ARRIVAL_INVALID", issues
        )
        priority_raw, _ = _required(
            item, ("priority",), path, "E_REQUEST_PRIORITY_INVALID", issues
        )
        priority = _nonnegative_int(
            priority_raw, f"{path}.priority", "E_REQUEST_PRIORITY_INVALID", issues
        )
        input_raw, _ = _required(
            item, ("input_tokens",), path, "E_REQUEST_TOKENS_INVALID", issues
        )
        input_tokens = _nonnegative_int(
            input_raw, f"{path}.input_tokens", "E_REQUEST_TOKENS_INVALID", issues
        )
        output_raw, _ = _required(
            item,
            ("max_output_tokens", "output_tokens", "max_new_tokens"),
            path,
            "E_REQUEST_TOKENS_INVALID",
            issues,
        )
        max_output = _nonnegative_int(
            output_raw, f"{path}.max_output_tokens", "E_REQUEST_TOKENS_INVALID", issues
        )
        deadline_raw, _ = _required(
            item,
            ("deadline_ms", "expected_deadline_ms", "deadline"),
            path,
            "E_REQUEST_DEADLINE_INVALID",
            issues,
        )
        deadline = _nonnegative_int(
            deadline_raw, f"{path}.deadline_ms", "E_REQUEST_DEADLINE_INVALID", issues
        )
        if arrival is None or priority is None or input_tokens is None or max_output is None or deadline is None:
            continue
        if input_tokens + max_output <= 0:
            issues.append(
                _issue(
                    "E_REQUEST_TOKENS_INVALID",
                    path,
                    "input_tokens + max_output_tokens must be positive",
                )
            )
            continue
        requests.append(
            RequestSpec(
                request_id=request_id,
                arrival_ms=arrival,
                priority=priority,
                input_tokens=input_tokens,
                max_output_tokens=max_output,
                deadline_ms=deadline,
            )
        )
    return tuple(sorted(requests, key=lambda request: request.request_id))


def _parse_accuracy(raw: Any, issues: list[dict[str, Any]]) -> AccuracySpec | None:
    if not isinstance(raw, dict):
        issues.append(_issue("E_ACCURACY_INVALID", "accuracy", "must be an object"))
        return None
    scales: dict[str, float | None] = {}
    for metric, names, code in (
        ("compute", ("compute_scale", "compute_fixed_scale"), "E_ACCURACY_SCALE_INVALID"),
        ("memory", ("memory_scale", "memory_fixed_scale"), "E_ACCURACY_SCALE_INVALID"),
        ("latency", ("latency_scale", "duration_scale", "latency_fixed_scale"), "E_ACCURACY_SCALE_INVALID"),
    ):
        value, _ = _required(raw, names, "accuracy", code, issues)
        scales[metric] = _positive_finite(value, f"accuracy.{metric}_scale", code, issues)
    tolerance_raw, _ = _required(
        raw,
        ("float_tolerance", "tolerance"),
        "accuracy",
        "E_ACCURACY_TOLERANCE_INVALID",
        issues,
    )
    tolerance = _positive_finite(
        tolerance_raw,
        "accuracy.float_tolerance",
        "E_ACCURACY_TOLERANCE_INVALID",
        issues,
    )
    budget_raw, _ = _required(
        raw,
        ("error_budget", "error_upper_bound_budget", "max_error"),
        "accuracy",
        "E_ACCURACY_BUDGET_INVALID",
        issues,
    )
    budget = _positive_finite(
        budget_raw, "accuracy.error_budget", "E_ACCURACY_BUDGET_INVALID", issues
    )
    rounding = raw.get("rounding_mode", "half_up")
    if rounding not in ROUNDING_MODES:
        issues.append(
            _issue(
                "E_ACCURACY_ROUNDING_INVALID",
                "accuracy.rounding_mode",
                "must be one of: " + ", ".join(sorted(ROUNDING_MODES)),
                rounding,
            )
        )
    if (
        scales["compute"] is None
        or scales["memory"] is None
        or scales["latency"] is None
        or tolerance is None
        or budget is None
        or rounding not in ROUNDING_MODES
    ):
        return None
    return AccuracySpec(
        compute_scale=scales["compute"],
        memory_scale=scales["memory"],
        latency_scale=scales["latency"],
        float_tolerance=tolerance,
        error_budget=budget,
        rounding_mode=rounding,
    )


def _parse_injections(
    raw: Any,
    replica_ids: set[str],
    issues: list[dict[str, Any]],
) -> tuple[InjectionSpec, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        issues.append(_issue("E_INJECTION_INVALID", "injections", "must be a list"))
        return ()
    injections: list[InjectionSpec] = []
    ids: set[str] = set()
    for index, item in enumerate(raw):
        path = f"injections[{index}]"
        if not isinstance(item, dict):
            issues.append(_issue("E_INJECTION_INVALID", path, "must be an object"))
            continue
        injection_id = item.get("injection_id", f"inj-{index + 1:03d}")
        if not isinstance(injection_id, str) or not injection_id.strip():
            issues.append(
                _issue("E_INJECTION_ID_INVALID", f"{path}.injection_id", "must be a non-empty string", injection_id)
            )
            continue
        if injection_id in ids:
            issues.append(
                _issue("E_INJECTION_ID_DUPLICATE", f"{path}.injection_id", "duplicate injection id", injection_id)
            )
            continue
        ids.add(injection_id)
        replica_id = item.get("replica_id")
        if replica_id is not None and (not isinstance(replica_id, str) or replica_id not in replica_ids):
            issues.append(
                _issue(
                    "E_INJECTION_REPLICA_INVALID",
                    f"{path}.replica_id",
                    "must be null or an existing replica id",
                    replica_id,
                )
            )
            continue
        window_index = item.get("window_index")
        if not _is_int(window_index) or window_index < 0:
            issues.append(
                _issue(
                    "E_INJECTION_WINDOW_INVALID",
                    f"{path}.window_index",
                    "must be a non-negative integer",
                    window_index,
                )
            )
            continue
        phase = item.get("phase")
        if phase not in INJECTION_PHASES:
            issues.append(
                _issue(
                    "E_INJECTION_PHASE_INVALID",
                    f"{path}.phase",
                    "must be one of: " + ", ".join(sorted(INJECTION_PHASES)),
                    phase,
                )
            )
            continue
        batch_index = item.get("batch_index")
        if batch_index is not None and (not _is_int(batch_index) or batch_index < 0):
            issues.append(
                _issue(
                    "E_INJECTION_BATCH_INVALID",
                    f"{path}.batch_index",
                    "must be null or a non-negative integer",
                    batch_index,
                )
            )
            continue
        signal_name = item.get("signal", "SIGKILL")
        if signal_name != "SIGKILL":
            issues.append(
                _issue(
                    "E_INJECTION_SIGNAL_INVALID",
                    f"{path}.signal",
                    "only SIGKILL is supported",
                    signal_name,
                )
            )
            continue
        enabled = item.get("enabled", True)
        if not isinstance(enabled, bool):
            issues.append(
                _issue("E_INJECTION_ENABLED_INVALID", f"{path}.enabled", "must be a boolean", enabled)
            )
            continue
        sequence = item.get("sequence", index)
        if not _is_int(sequence) or sequence < 0:
            issues.append(
                _issue("E_INJECTION_SEQUENCE_INVALID", f"{path}.sequence", "must be a non-negative integer", sequence)
            )
            continue
        injections.append(
            InjectionSpec(
                injection_id=injection_id,
                replica_id=replica_id,
                window_index=window_index,
                phase=phase,
                batch_index=batch_index,
                signal_name=signal_name,
                enabled=enabled,
                sequence=sequence,
            )
        )
    return tuple(sorted(injections, key=lambda item: (item.sequence, item.injection_id)))


def request_compute_units(model: ModelSpec, request: RequestSpec) -> float:
    return (
        request.sequence_tokens
        * model.total_params
        * model.flops_per_parameter_per_token
        / 1_000_000_000.0
    )


def request_kv_memory_mib(model: ModelSpec, request: RequestSpec) -> float:
    return (
        request.sequence_tokens * model.kv_mib_per_token
        + request.input_tokens * model.activation_mib_per_token
    )


def _feasibility_issues(
    model: ModelSpec,
    replicas: tuple[ReplicaSpec, ...],
    requests: tuple[RequestSpec, ...],
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for request in requests:
        sequence_ok = [
            replica
            for replica in replicas
            if replica.max_sequence_length >= request.sequence_tokens
        ]
        if not sequence_ok:
            issues.append(
                _issue(
                    "E_REQUEST_SEQUENCE_EXCEEDED",
                    f"requests.request_id={request.request_id}",
                    "no replica supports the request input+output sequence length",
                    {
                        "sequence_tokens": request.sequence_tokens,
                        "limits": {
                            replica.replica_id: replica.max_sequence_length
                            for replica in replicas
                        },
                    },
                )
            )
            continue
        try:
            compute = request_compute_units(model, request)
            memory = model.memory_base_mib + request_kv_memory_mib(model, request)
        except (OverflowError, ValueError):
            issues.append(
                _issue(
                    "E_REQUEST_ESTIMATE_INVALID",
                    f"requests.request_id={request.request_id}",
                    "request estimates overflow finite numeric range",
                )
            )
            continue
        if not math.isfinite(compute) or not math.isfinite(memory):
            issues.append(
                _issue(
                    "E_REQUEST_ESTIMATE_INVALID",
                    f"requests.request_id={request.request_id}",
                    "request estimates must be finite",
                )
            )
            continue
        feasible = [
            replica
            for replica in sequence_ok
            if compute <= replica.compute_capacity
            and memory <= replica.memory_limit_mib
            and request.max_output_tokens <= replica.max_sequence_length
        ]
        if not feasible:
            issues.append(
                _issue(
                    "E_REQUEST_INFEASIBLE",
                    f"requests.request_id={request.request_id}",
                    "no replica can hold one request within compute and memory budgets",
                    {
                        "single_request_compute": compute,
                        "single_request_memory_mib": memory,
                    },
                )
            )
    return issues


def validate_and_build(
    bundle: dict[str, Any], source: str = "<memory>"
) -> Fixture:
    issues: list[dict[str, Any]] = []
    if not isinstance(bundle, dict):
        raise ValidationError(
            [_issue("E_FIXTURE_INVALID", "$", "fixture must be a JSON object")]
        )
    schema_version = bundle.get("schema_version", SCHEMA_VERSION)
    if not _is_int(schema_version) or schema_version != SCHEMA_VERSION:
        issues.append(
            _issue(
                "E_SCHEMA_VERSION_INVALID",
                "schema_version",
                f"only schema_version={SCHEMA_VERSION} is supported",
                schema_version,
            )
        )
    model = _parse_model(bundle.get("model"), issues)
    replicas = _parse_replicas(bundle.get("replicas"), issues)
    requests = _parse_requests(bundle.get("requests"), issues)
    accuracy = _parse_accuracy(bundle.get("accuracy"), issues)
    replica_ids = {replica.replica_id for replica in replicas}
    injections = _parse_injections(bundle.get("injections", []), replica_ids, issues)
    if model is not None and replicas and requests:
        issues.extend(_feasibility_issues(model, replicas, requests))
    if issues or model is None or accuracy is None:
        raise ValidationError(issues)

    canonical = {
        "schema_version": SCHEMA_VERSION,
        "model": model.to_dict(),
        "replicas": [replica.to_dict() for replica in replicas],
        "requests": [request.to_dict() for request in requests],
        "accuracy": accuracy.to_dict(),
        "injections": [injection.to_dict() for injection in injections],
    }
    return Fixture(
        schema_version=SCHEMA_VERSION,
        model=model,
        replicas=replicas,
        requests=requests,
        accuracy=accuracy,
        injections=injections,
        canonical=canonical,
        content_hash=content_hash(canonical),
        source=source,
    )


def load_fixture(path: str | Path) -> Fixture:
    fixture_path = Path(path).expanduser()
    if not fixture_path.exists():
        raise BusinessError("E_FIXTURE_NOT_FOUND", f"fixture not found: {fixture_path}")
    bundle = _load_component_bundle(fixture_path)
    return validate_and_build(bundle, source=str(fixture_path.resolve()))
