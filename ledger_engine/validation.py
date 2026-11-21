from __future__ import annotations

from fractions import Fraction
from typing import Any, Iterable

from .errors import LedgerError
from .jsonutil import fraction_from, load_json
from .model import (
    DEFAULT_DERIVED_SCALES,
    DERIVED_ORDER,
    PARAM_ORDER,
    LedgerConfig,
    ParamSpec,
    Patch,
    ServiceDef,
    Snapshot,
    topological_order,
)


ROUNDING_MODES = {
    "ROUND_CEILING",
    "ROUND_DOWN",
    "ROUND_FLOOR",
    "ROUND_HALF_DOWN",
    "ROUND_HALF_EVEN",
    "ROUND_HALF_UP",
    "ROUND_UP",
    "ROUND_05UP",
}

_PARAM_ALIASES = {
    "scales": "param_scales",
    "error_budget": "error_budgets",
    "error_bounds": "error_budgets",
    "budgets": "error_budgets",
}


def _bad_scale(message: str, **details: Any) -> None:
    raise LedgerError("E_BAD_SCALE", message, details)


def _bad_budget(message: str, **details: Any) -> None:
    raise LedgerError("E_BAD_BUDGET", message, details)


def _as_scale(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _bad_scale(f"{field} must be an integer between 0 and 18", field=field)
    if value < 0 or value > 18:
        _bad_scale(f"{field} must be between 0 and 18", field=field, value=value)
    return value


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise LedgerError("E_BAD_CONFIG", f"{field} must be a positive integer")
    if isinstance(value, int) and value > 0:
        return value
    raise LedgerError("E_BAD_CONFIG", f"{field} must be a positive integer")


def parse_config(payload: Any) -> LedgerConfig:
    if not isinstance(payload, dict):
        raise LedgerError("E_BAD_CONFIG", "ledger config must be a JSON object")
    normalized = dict(payload)
    for alias, canonical in _PARAM_ALIASES.items():
        if canonical not in normalized and alias in normalized:
            normalized[canonical] = normalized[alias]
    if "param_scales" not in normalized and isinstance(
        normalized.get("parameters"), dict
    ):
        normalized["param_scales"] = {
            name: item.get("scale")
            for name, item in normalized["parameters"].items()
            if isinstance(item, dict)
        }
        normalized["error_budgets"] = {
            name: item.get("error_budget")
            for name, item in normalized["parameters"].items()
            if isinstance(item, dict)
        }

    scales = normalized.get("param_scales")
    budgets = normalized.get("error_budgets")
    if not isinstance(scales, dict):
        _bad_scale("param_scales must be an object")
    if not isinstance(budgets, dict):
        _bad_budget("error_budgets must be an object")

    parameters: dict[str, ParamSpec] = {}
    for name in PARAM_ORDER:
        if name not in scales:
            _bad_scale(f"missing scale for parameter {name}", parameter=name)
        if name not in budgets:
            _bad_budget(f"missing error budget for parameter {name}", parameter=name)
        scale = _as_scale(scales[name], f"param_scales.{name}")
        try:
            budget = fraction_from(budgets[name], field=f"error_budgets.{name}")
        except LedgerError as exc:
            raise LedgerError(
                "E_BAD_BUDGET",
                f"error budget for {name} is not a finite decimal",
                {"parameter": name},
            ) from exc
        if budget < 0:
            _bad_budget(
                f"error budget for {name} must be non-negative", parameter=name
            )
        parameters[name] = ParamSpec(scale=scale, error_budget=budget)

    derived_payload = normalized.get("derived_scales", DEFAULT_DERIVED_SCALES)
    if not isinstance(derived_payload, dict):
        _bad_scale("derived_scales must be an object")
    derived_scales: dict[str, int] = {}
    for name in DERIVED_ORDER:
        if name not in derived_payload:
            _bad_scale(f"missing derived scale for {name}", derived=name)
        derived_scales[name] = _as_scale(
            derived_payload[name], f"derived_scales.{name}"
        )

    try:
        tolerance = fraction_from(
            normalized.get("compare_tolerance", "0.000001"),
            field="compare_tolerance",
        )
    except LedgerError as exc:
        raise LedgerError(
            "E_BAD_TOLERANCE",
            "compare_tolerance must be a finite positive number",
        ) from exc
    if tolerance <= 0:
        raise LedgerError(
            "E_BAD_TOLERANCE", "compare_tolerance must be greater than zero"
        )

    step = _positive_int(normalized.get("logical_clock_step", 1), "logical_clock_step")
    concurrency = _positive_int(
        normalized.get("max_concurrency", 4), "max_concurrency"
    )
    if concurrency > 64:
        raise LedgerError(
            "E_BAD_CONFIG", "max_concurrency must not exceed 64"
        )
    precision = _positive_int(
        normalized.get("truth_precision", 80), "truth_precision"
    )
    if precision < 28 or precision > 2000:
        raise LedgerError(
            "E_BAD_CONFIG", "truth_precision must be between 28 and 2000"
        )
    rounding = normalized.get("truth_rounding", "ROUND_HALF_EVEN")
    if rounding not in ROUNDING_MODES:
        raise LedgerError(
            "E_BAD_CONFIG",
            "truth_rounding is not a supported decimal rounding mode",
            {"value": rounding},
        )
    higher_increment = _positive_int(
        normalized.get("higher_scale_increment", 6),
        "higher_scale_increment",
    )
    if higher_increment > 18:
        raise LedgerError(
            "E_BAD_SCALE", "higher_scale_increment must not exceed 18"
        )

    return LedgerConfig(
        parameters=parameters,
        derived_scales=derived_scales,
        compare_tolerance=tolerance,
        logical_clock_step=step,
        max_concurrency=concurrency,
        truth_precision=precision,
        truth_rounding=rounding,
        higher_scale_increment=higher_increment,
    )


def parse_topology(payload: Any) -> tuple[ServiceDef, ...]:
    if isinstance(payload, list):
        service_payload = payload
    elif isinstance(payload, dict):
        service_payload = payload.get("services")
    else:
        service_payload = None
    if not isinstance(service_payload, list):
        raise LedgerError(
            "E_BAD_TOPOLOGY", "topology must contain a services array"
        )

    seen: set[str] = set()
    services: list[ServiceDef] = []
    for index, item in enumerate(service_payload):
        if not isinstance(item, dict):
            raise LedgerError(
                "E_BAD_TOPOLOGY",
                "each service must be an object",
                {"index": index},
            )
        name = item.get("name")
        layer = item.get("layer")
        if not isinstance(name, str) or not name:
            raise LedgerError(
                "E_BAD_TOPOLOGY",
                "service name must be a non-empty string",
                {"index": index},
            )
        if not isinstance(layer, str) or not layer:
            raise LedgerError(
                "E_BAD_TOPOLOGY",
                "service layer must be a non-empty string",
                {"service": name},
            )
        if name in seen:
            raise LedgerError(
                "E_BAD_TOPOLOGY", "service names must be unique", {"service": name}
            )
        seen.add(name)
        dependencies = item.get("depends_on", [])
        if not isinstance(dependencies, list) or any(
            not isinstance(dependency, str) or not dependency
            for dependency in dependencies
        ):
            raise LedgerError(
                "E_BAD_TOPOLOGY",
                "depends_on must be an array of non-empty service names",
                {"service": name},
            )
        if len(set(dependencies)) != len(dependencies):
            raise LedgerError(
                "E_BAD_TOPOLOGY",
                "duplicate dependency names are not allowed",
                {"service": name},
            )
        if name in dependencies:
            raise LedgerError(
                "E_CYCLE",
                "a service cannot depend on itself",
                {"service": name},
            )
        services.append(
            ServiceDef(
                name=name,
                layer=layer,
                depends_on=tuple(sorted(dependencies)),
            )
        )

    by_name = {service.name: service for service in services}
    for service in services:
        for dependency in service.depends_on:
            if dependency not in by_name:
                raise LedgerError(
                    "E_BAD_TOPOLOGY",
                    "dependency references an unknown service",
                    {"service": service.name, "missing": dependency},
                )
    topological_order(services)
    return tuple(sorted(services, key=lambda service: service.name))


def parse_layers(
    payload: Any, services: Iterable[ServiceDef]
) -> dict[str, dict[str, Fraction]]:
    if isinstance(payload, dict):
        layers_payload = payload.get("layers", payload)
    else:
        layers_payload = None
    if not isinstance(layers_payload, dict):
        raise LedgerError("E_BAD_LAYER", "layers must be a JSON object")

    service_list = list(services)
    result: dict[str, dict[str, Fraction]] = {}
    for service in service_list:
        raw_values = layers_payload.get(service.layer)
        if not isinstance(raw_values, dict):
            raise LedgerError(
                "E_BAD_LAYER",
                "service layer has no parameter object",
                {"service": service.name, "layer": service.layer},
            )
        values: dict[str, Fraction] = {}
        for name in PARAM_ORDER:
            if name not in raw_values:
                raise LedgerError(
                    "E_BAD_PARAM",
                    "layer is missing a required parameter",
                    {"layer": service.layer, "parameter": name},
                )
            values[name] = _validate_parameter(name, raw_values[name], service.layer)
        result[service.layer] = values

    for service in service_list:
        if service.layer not in result:
            raise LedgerError(
                "E_BAD_LAYER",
                "service references an unknown layer",
                {"service": service.name, "layer": service.layer},
            )
    return {layer: dict(sorted(values.items())) for layer, values in sorted(result.items())}


def _validate_parameter(name: str, value: Any, layer: str) -> Fraction:
    if name in ("timeout_ms", "capacity"):
        parsed = fraction_from(value, field=f"layers.{layer}.{name}")
        if parsed.denominator != 1 or parsed <= 0:
            raise LedgerError(
                "E_BAD_PARAM",
                f"{name} must be a positive integer",
                {"layer": layer, "parameter": name, "value": str(value)},
            )
        return parsed
    if name == "retry_multiplier":
        parsed = fraction_from(value, field=f"layers.{layer}.{name}")
        if parsed <= 0:
            raise LedgerError(
                "E_BAD_PARAM",
                "retry_multiplier must be positive",
                {"layer": layer, "parameter": name, "value": str(value)},
            )
        return parsed
    if name == "sample_rate":
        parsed = fraction_from(value, field=f"layers.{layer}.{name}")
        if not (0 < parsed < 1):
            raise LedgerError(
                "E_BAD_PARAM",
                "sample_rate must be strictly between zero and one",
                {"layer": layer, "parameter": name, "value": str(value)},
            )
        return parsed
    if name == "weight":
        parsed = fraction_from(value, field=f"layers.{layer}.{name}")
        if parsed < 0:
            raise LedgerError(
                "E_BAD_PARAM",
                "weight must be non-negative",
                {"layer": layer, "parameter": name, "value": str(value)},
            )
        return parsed
    raise LedgerError("E_BAD_PARAM", f"unknown parameter {name}")


def validate_snapshot(snapshot: Snapshot) -> None:
    services = snapshot.services
    names = [service.name for service in services]
    if len(set(names)) != len(names):
        raise LedgerError("E_BAD_TOPOLOGY", "service names must be unique")
    by_name = snapshot.service_map()
    for service in services:
        if service.layer not in snapshot.layers:
            raise LedgerError(
                "E_BAD_LAYER",
                "service layer has no parameters",
                {"service": service.name, "layer": service.layer},
            )
        for dependency in service.depends_on:
            if dependency not in by_name:
                raise LedgerError(
                    "E_BAD_TOPOLOGY",
                    "dependency references an unknown service",
                    {"service": service.name, "missing": dependency},
                )
    topological_order(services)
    for layer, values in snapshot.layers.items():
        for name in PARAM_ORDER:
            if name not in values:
                raise LedgerError(
                    "E_BAD_PARAM",
                    "layer is missing a required parameter",
                    {"layer": layer, "parameter": name},
                )
            _validate_parameter(name, values[name], layer)


def parse_patch(payload: Any) -> Patch:
    if not isinstance(payload, dict):
        raise LedgerError("E_BAD_PATCH", "patch must be a JSON object")
    operations = payload.get("operations")
    if not isinstance(operations, list):
        raise LedgerError("E_BAD_PATCH", "patch must contain an operations array")
    normalized_operations: list[dict[str, Any]] = []
    for index, item in enumerate(operations):
        if not isinstance(item, dict):
            raise LedgerError(
                "E_BAD_PATCH",
                "patch operation must be an object",
                {"index": index},
            )
        operation = item.get("op")
        if operation not in ("set_param", "delete_param"):
            raise LedgerError(
                "E_BAD_PATCH",
                "unsupported patch operation",
                {"index": index, "op": operation},
            )
        layer = item.get("layer")
        parameter = item.get("parameter")
        if not isinstance(layer, str) or not layer:
            raise LedgerError(
                "E_BAD_PATCH",
                "patch layer must be a non-empty string",
                {"index": index},
            )
        if parameter not in PARAM_ORDER:
            raise LedgerError(
                "E_BAD_PATCH",
                "patch parameter is not supported",
                {"index": index, "parameter": parameter},
            )
        normalized: dict[str, Any] = {
            "layer": layer,
            "op": operation,
            "parameter": parameter,
        }
        if operation == "set_param":
            if "value" not in item:
                raise LedgerError(
                    "E_BAD_PATCH",
                    "set_param requires a value",
                    {"index": index},
                )
            try:
                _validate_parameter(parameter, item["value"], layer)
            except LedgerError as exc:
                raise LedgerError(
                    "E_BAD_PATCH",
                    "patch value is not valid for its parameter",
                    {
                        "index": index,
                        "layer": layer,
                        "parameter": parameter,
                        "reason": exc.message,
                    },
                ) from exc
            value = fraction_from(item["value"], field=f"patch.operations[{index}].value")
            normalized["value"] = value
        normalized_operations.append(normalized)

    patch_id = payload.get("patch_id")
    if patch_id is None:
        from .jsonutil import hash_json

        patch_id = "p-" + hash_json(
            [dict(op, value=str(op.get("value"))) for op in normalized_operations]
        )[:24]
    if not isinstance(patch_id, str) or not patch_id:
        raise LedgerError("E_BAD_PATCH", "patch_id must be a non-empty string")
    description = payload.get("description", "")
    if not isinstance(description, str):
        raise LedgerError("E_BAD_PATCH", "patch description must be a string")
    return Patch(
        patch_id=patch_id,
        operations=tuple(normalized_operations),
        description=description,
    )


def load_patch(path: str) -> Patch:
    return parse_patch(load_json(path))


def parse_sample(
    topology_path: str,
    layers_path: str,
    config_path: str,
    *,
    seed: int = 0,
) -> Snapshot:
    config = parse_config(load_json(config_path))
    services = parse_topology(load_json(topology_path))
    layers = parse_layers(load_json(layers_path), services)
    snapshot = Snapshot(services=services, layers=layers, config=config, seed=seed)
    validate_snapshot(snapshot)
    return snapshot


def apply_patch(snapshot: Snapshot, base: Snapshot, patch: Patch) -> Snapshot:
    layers = {
        layer: dict(values)
        for layer, values in snapshot.layers.items()
    }
    for index, operation in enumerate(patch.operations):
        layer = operation["layer"]
        parameter = operation["parameter"]
        if layer not in base.layers:
            raise LedgerError(
                "E_BAD_PATCH",
                "patch references a layer that is absent from the base sample",
                {"index": index, "layer": layer},
            )
        if operation["op"] == "set_param":
            layers.setdefault(layer, dict(base.layers[layer]))
            layers[layer][parameter] = operation["value"]
        else:
            layers.setdefault(layer, dict(base.layers[layer]))
            layers[layer][parameter] = base.layers[layer][parameter]
    candidate = Snapshot(
        services=snapshot.services,
        layers={
            layer: dict(sorted(values.items()))
            for layer, values in sorted(layers.items())
        },
        config=snapshot.config,
        seed=snapshot.seed,
    )
    validate_snapshot(candidate)
    return candidate


def changed_sources(old: Snapshot, new: Snapshot) -> list[str]:
    changes: list[str] = []
    layers = sorted(set(old.layers) | set(new.layers))
    for layer in layers:
        old_values = old.layers.get(layer, {})
        new_values = new.layers.get(layer, {})
        for parameter in PARAM_ORDER:
            if old_values.get(parameter) != new_values.get(parameter):
                changes.append(f"layers.{layer}.{parameter}")
    old_services = {service.name: service for service in old.services}
    new_services = {service.name: service for service in new.services}
    for name in sorted(set(old_services) | set(new_services)):
        if old_services.get(name) != new_services.get(name):
            changes.append(f"services.{name}")
    return changes
