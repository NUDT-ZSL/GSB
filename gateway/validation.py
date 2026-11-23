"""Input parsing and business validation.

All validation happens before a database is opened for write.  Therefore an
invalid ``load`` never creates a database or a partial ledger.
"""

from __future__ import annotations

import math
from typing import Any

from .canonical import canonical_json, sha256_obj
from .errors import GatewayError


SCHEMA_VERSION = 1
OUTCOME_VALUES = {"success", "failure", "rejected", "expired", "timeout", "abort"}


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, ValueError):
        return False


def _obj(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GatewayError("E_INPUT_SCHEMA", f"{field} must be an object", field=field)
    return value


def _list(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise GatewayError("E_INPUT_SCHEMA", f"{field} must be a list", field=field)
    return value


def _string(value: Any, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and value == ""):
        raise GatewayError(
            "E_INPUT_SCHEMA",
            f"{field} must be a non-empty string",
            field=field,
        )
    return value


def _int(
    value: Any,
    field: str,
    *,
    minimum: int | None = None,
    code: str = "E_INPUT_SCHEMA",
    **details: Any,
) -> int:
    if not _is_int(value):
        raise GatewayError(code, f"{field} must be an integer", field=field, **details)
    if minimum is not None and value < minimum:
        raise GatewayError(
            code,
            f"{field} must be >= {minimum}",
            field=field,
            value=value,
            **details,
        )
    return value


def _positive_int(value: Any, field: str, *, code: str = "E_INPUT_SCHEMA", **details: Any) -> int:
    return _int(value, field, minimum=1, code=code, **details)


def _positive_number(value: Any, field: str, **details: Any) -> float:
    if not _is_number(value) or float(value) <= 0.0:
        raise GatewayError(
            "E_INVALID_COMPUTE",
            f"{field} must be a finite positive number",
            field=field,
            **details,
        )
    return float(value)


def _schema(doc: Any, name: str) -> dict[str, Any]:
    obj = _obj(doc, name)
    version = obj.get("schema_version", SCHEMA_VERSION)
    if version != SCHEMA_VERSION:
        raise GatewayError(
            "E_INPUT_SCHEMA_VERSION",
            f"{name}.schema_version must be {SCHEMA_VERSION}",
            field=f"{name}.schema_version",
            value=version,
        )
    return obj


def _token_settings(obj: Any, field: str) -> dict[str, int | None]:
    token = _obj(obj, field)
    capacity = _positive_int(
        token.get("capacity"),
        f"{field}.capacity",
        code="E_INVALID_TOKEN_CAPACITY",
    )
    rate = _positive_int(
        token.get("refill_rate_per_second"),
        f"{field}.refill_rate_per_second",
        code="E_INVALID_TOKEN_RATE",
    )
    max_inflight_raw = token.get("max_inflight", capacity)
    max_inflight = _positive_int(
        max_inflight_raw,
        f"{field}.max_inflight",
        code="E_INVALID_CONCURRENCY_LIMIT",
    )
    return {
        "capacity": capacity,
        "refill_rate_per_second": rate,
        "max_inflight": max_inflight,
    }


def normalize_config(doc: Any) -> dict[str, Any]:
    raw = _schema(doc, "config")
    config_version = _positive_int(
        raw.get("config_version"),
        "config.config_version",
        code="E_INVALID_CONFIG_VERSION",
    )
    global_token = _token_settings(raw.get("global"), "config.global")
    service_token = _token_settings(raw.get("service"), "config.service")

    overrides_raw = raw.get("service_overrides", {})
    overrides_obj = _obj(overrides_raw, "config.service_overrides")
    overrides: dict[str, dict[str, int | None]] = {}
    for name in sorted(overrides_obj):
        _string(name, "config.service_overrides key")
        overrides[name] = _token_settings(
            overrides_obj[name], f"config.service_overrides.{name}"
        )

    round_budget = _positive_int(
        raw.get("round_budget_ms"),
        "config.round_budget_ms",
        code="E_INVALID_ROUND_BUDGET",
    )
    dispatch_timeout = _positive_int(
        raw.get("dispatch_timeout_ms"),
        "config.dispatch_timeout_ms",
        code="E_INVALID_DISPATCH_TIMEOUT",
    )

    backoff_raw = _obj(raw.get("backoff"), "config.backoff")
    base_ms = _positive_int(
        backoff_raw.get("base_ms"), "config.backoff.base_ms", code="E_INVALID_BACKOFF"
    )
    cap_ms = _positive_int(
        backoff_raw.get("cap_ms"), "config.backoff.cap_ms", code="E_INVALID_BACKOFF"
    )
    if cap_ms < base_ms:
        raise GatewayError(
            "E_INVALID_BACKOFF",
            "config.backoff.cap_ms must be >= config.backoff.base_ms",
            field="config.backoff.cap_ms",
            base_ms=base_ms,
            cap_ms=cap_ms,
        )
    max_generation = _positive_int(
        backoff_raw.get("max_generation"),
        "config.backoff.max_generation",
        code="E_INVALID_BACKOFF",
    )

    fixed_raw = _obj(raw.get("fixed_point"), "config.fixed_point")
    scale = _positive_int(
        fixed_raw.get("scale"), "config.fixed_point.scale", code="E_INVALID_FIXED_POINT"
    )
    tolerance = _positive_int(
        fixed_raw.get("tolerance_ms"),
        "config.fixed_point.tolerance_ms",
        code="E_INVALID_FIXED_POINT",
    )

    scheduling_raw = raw.get("scheduling", {})
    scheduling = _obj(scheduling_raw, "config.scheduling")
    priority_min = _int(
        scheduling.get("priority_min", 0),
        "config.scheduling.priority_min",
        code="E_REQUEST_PRIORITY",
    )
    priority_max = _int(
        scheduling.get("priority_max", 9),
        "config.scheduling.priority_max",
        code="E_REQUEST_PRIORITY",
    )
    if priority_min > priority_max:
        raise GatewayError(
            "E_REQUEST_PRIORITY",
            "config.scheduling.priority_min must be <= priority_max",
            priority_min=priority_min,
            priority_max=priority_max,
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "config_version": config_version,
        "global": global_token,
        "service": service_token,
        "service_overrides": overrides,
        "round_budget_ms": round_budget,
        "dispatch_timeout_ms": dispatch_timeout,
        "backoff": {
            "base_ms": base_ms,
            "cap_ms": cap_ms,
            "max_generation": max_generation,
        },
        "fixed_point": {
            "scale": scale,
            "tolerance_ms": tolerance,
            "refill_denominator": 1000,
        },
        "scheduling": {
            "priority_min": priority_min,
            "priority_max": priority_max,
        },
    }


def normalize_services(doc: Any) -> dict[str, Any]:
    raw = _schema(doc, "services")
    services_raw = _list(raw.get("services"), "services.services")
    if not services_raw:
        raise GatewayError("E_INPUT_SCHEMA", "services.services must not be empty")

    names: set[str] = set()
    services: list[dict[str, Any]] = []
    for index, item_raw in enumerate(services_raw):
        field = f"services.services[{index}]"
        item = _obj(item_raw, field)
        name = _string(item.get("name"), f"{field}.name")
        if name in names:
            raise GatewayError(
                "E_DUPLICATE_SERVICE",
                f"duplicate service name: {name}",
                service=name,
            )
        names.add(name)
        model_version = _string(item.get("model_version"), f"{field}.model_version")
        batch_window = _positive_int(
            item.get("batch_window_ms"),
            f"{field}.batch_window_ms",
            code="E_INVALID_BATCH_WINDOW",
            service=name,
        )
        replicas_raw = _list(item.get("replicas"), f"{field}.replicas")
        if not replicas_raw:
            raise GatewayError(
                "E_INVALID_REPLICAS",
                f"service {name} must contain at least one ready replica",
                service=name,
            )

        replica_ids: set[str] = set()
        replicas: list[dict[str, Any]] = []
        for replica_index, replica_raw in enumerate(replicas_raw):
            replica_field = f"{field}.replicas[{replica_index}]"
            replica = _obj(replica_raw, replica_field)
            replica_id = _string(
                replica.get("replica_id"), f"{replica_field}.replica_id"
            )
            if replica_id in replica_ids:
                raise GatewayError(
                    "E_DUPLICATE_REPLICA",
                    f"duplicate replica id {replica_id} in service {name}",
                    service=name,
                    replica_id=replica_id,
                )
            replica_ids.add(replica_id)
            slots = _positive_int(
                replica.get("slots"),
                f"{replica_field}.slots",
                code="E_INVALID_SLOTS",
                service=name,
                replica_id=replica_id,
            )
            compute_units = _positive_number(
                replica.get("compute_units"),
                f"{replica_field}.compute_units",
                service=name,
                replica_id=replica_id,
            )
            replicas.append(
                {
                    "replica_id": replica_id,
                    "slots": slots,
                    "compute_units": compute_units,
                }
            )
        replicas.sort(key=lambda value: value["replica_id"])
        services.append(
            {
                "name": name,
                "model_version": model_version,
                "batch_window_ms": batch_window,
                "replicas": replicas,
            }
        )
    services.sort(key=lambda value: value["name"])
    return {"schema_version": SCHEMA_VERSION, "services": services}


def normalize_requests(doc: Any, services: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    raw = _schema(doc, "requests")
    requests_raw = _list(raw.get("requests"), "requests.requests")
    service_names = {item["name"] for item in services["services"]}
    priority_min = config["scheduling"]["priority_min"]
    priority_max = config["scheduling"]["priority_max"]

    ids: set[int] = set()
    requests: list[dict[str, Any]] = []
    for index, item_raw in enumerate(requests_raw):
        field = f"requests.requests[{index}]"
        item = _obj(item_raw, field)
        request_id = _positive_int(
            item.get("request_id"),
            f"{field}.request_id",
            code="E_INVALID_REQUEST_ID",
        )
        if request_id in ids:
            raise GatewayError(
                "E_DUPLICATE_REQUEST",
                f"duplicate request id: {request_id}",
                request_id=request_id,
            )
        ids.add(request_id)
        service = _string(item.get("service"), f"{field}.service")
        if service not in service_names:
            raise GatewayError(
                "E_SERVICE_NOT_FOUND",
                f"request {request_id} references unknown service {service}",
                request_id=request_id,
                service=service,
            )
        arrival_ms = _int(
            item.get("arrival_ms"),
            f"{field}.arrival_ms",
            minimum=0,
            code="E_INVALID_ARRIVAL_CLOCK",
            request_id=request_id,
            service=service,
        )
        input_tokens = _positive_int(
            item.get("input_tokens"),
            f"{field}.input_tokens",
            code="E_INVALID_TOKEN_COUNT",
            request_id=request_id,
            service=service,
        )
        output_tokens = _positive_int(
            item.get("output_tokens"),
            f"{field}.output_tokens",
            code="E_INVALID_TOKEN_COUNT",
            request_id=request_id,
            service=service,
        )
        priority = _int(
            item.get("priority"),
            f"{field}.priority",
            code="E_REQUEST_PRIORITY",
            request_id=request_id,
            service=service,
        )
        if priority < priority_min or priority > priority_max:
            raise GatewayError(
                "E_REQUEST_PRIORITY",
                f"request {request_id} priority must be in [{priority_min}, {priority_max}]",
                request_id=request_id,
                service=service,
                priority=priority,
                priority_min=priority_min,
                priority_max=priority_max,
            )
        requests.append(
            {
                "request_id": request_id,
                "service": service,
                "arrival_ms": arrival_ms,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "priority": priority,
            }
        )
    requests.sort(key=lambda value: value["request_id"])
    return {"schema_version": SCHEMA_VERSION, "requests": requests}


def normalize_clock_steps(doc: Any) -> dict[str, Any] | None:
    if doc is None:
        return None
    raw = _schema(doc, "clock_steps")
    steps_raw = _list(raw.get("steps"), "clock_steps.steps")
    steps: list[dict[str, Any]] = []
    seen_round_ids: set[str] = set()
    for index, step_raw in enumerate(steps_raw):
        field = f"clock_steps.steps[{index}]"
        step = _obj(step_raw, field)
        clock_ms = _int(
            step.get("clock_ms"),
            f"{field}.clock_ms",
            minimum=0,
            code="E_INVALID_CLOCK",
        )
        budget_ms = step.get("budget_ms")
        if budget_ms is not None:
            budget_ms = _positive_int(
                budget_ms, f"{field}.budget_ms", code="E_INVALID_ROUND_BUDGET"
            )
        round_id = step.get("round_id")
        if round_id is not None:
            round_id = _string(round_id, f"{field}.round_id")
            if round_id in seen_round_ids:
                raise GatewayError(
                    "E_DUPLICATE_ROUND_ID",
                    f"duplicate clock-step round_id: {round_id}",
                    round_id=round_id,
                )
            seen_round_ids.add(round_id)
        outcomes = step.get("outcomes", [])
        outcomes = _list(outcomes, f"{field}.outcomes")
        for outcome_index, outcome_raw in enumerate(outcomes):
            outcome_field = f"{field}.outcomes[{outcome_index}]"
            outcome = _obj(outcome_raw, outcome_field)
            _positive_int(
                outcome.get("request_id"),
                f"{outcome_field}.request_id",
                code="E_INVALID_COMMAND",
            )
            _positive_int(
                outcome.get("generation"),
                f"{outcome_field}.generation",
                code="E_INVALID_COMMAND",
            )
            _string(outcome.get("replica_id"), f"{outcome_field}.replica_id")
            value = outcome.get("outcome")
            if value not in OUTCOME_VALUES:
                raise GatewayError(
                    "E_INVALID_COMMAND",
                    f"{outcome_field}.outcome is invalid",
                    field=f"{outcome_field}.outcome",
                    value=value,
                )
            if "clock_ms" in outcome:
                outcome_clock = _int(
                    outcome["clock_ms"],
                    f"{outcome_field}.clock_ms",
                    minimum=0,
                    code="E_INVALID_CLOCK",
                )
                if outcome_clock != clock_ms:
                    raise GatewayError(
                        "E_INVALID_COMMAND",
                        "outcome clock_ms must equal its step clock_ms",
                        field=f"{outcome_field}.clock_ms",
                        expected_clock_ms=clock_ms,
                        outcome_clock_ms=outcome_clock,
                    )
            if "reason" in outcome and not isinstance(outcome["reason"], str):
                raise GatewayError(
                    "E_INVALID_COMMAND",
                    f"{outcome_field}.reason must be a string",
                    field=f"{outcome_field}.reason",
                )
        services = step.get("services", [])
        services = _list(services, f"{field}.services")
        for service_name in services:
            _string(service_name, f"{field}.services[]")
        requests = step.get("requests", [])
        requests = _list(requests, f"{field}.requests")
        for request_id in requests:
            _positive_int(
                request_id,
                f"{field}.requests[]",
                code="E_INVALID_COMMAND",
            )
        force_targets = step.get("force_targets", {})
        force_targets = _obj(force_targets, f"{field}.force_targets")
        for request_key, replica_id in force_targets.items():
            try:
                parsed_request_id = int(request_key)
            except (TypeError, ValueError) as exc:
                raise GatewayError(
                    "E_INVALID_COMMAND",
                    "force_targets key must be a request id",
                    field=f"{field}.force_targets",
                ) from exc
            if parsed_request_id <= 0:
                raise GatewayError(
                    "E_INVALID_COMMAND",
                    "force_targets request id must be positive",
                    field=f"{field}.force_targets",
                )
            _string(replica_id, f"{field}.force_targets.{request_key}")
        for boolean_field in ("dispatch", "recover_only", "abort_overdue"):
            if boolean_field in step and not isinstance(step[boolean_field], bool):
                raise GatewayError(
                    "E_INVALID_COMMAND",
                    f"{field}.{boolean_field} must be a boolean",
                    field=f"{field}.{boolean_field}",
                )
        if "max_launches" in step:
            _int(
                step["max_launches"],
                f"{field}.max_launches",
                minimum=0,
                code="E_INVALID_COMMAND",
            )
        steps.append(step)
    return {"schema_version": SCHEMA_VERSION, "steps": steps}


def normalize_inputs(
    services_doc: Any,
    requests_doc: Any,
    config_doc: Any,
    clock_steps_doc: Any = None,
) -> dict[str, Any]:
    config = normalize_config(config_doc)
    services = normalize_services(services_doc)
    service_names = {item["name"] for item in services["services"]}
    for override_name in config["service_overrides"]:
        if override_name not in service_names:
            raise GatewayError(
                "E_SERVICE_NOT_FOUND",
                f"config.service_overrides references unknown service {override_name}",
                service=override_name,
            )
    requests = normalize_requests(requests_doc, services, config)
    clock_steps = normalize_clock_steps(clock_steps_doc)

    fingerprint_source = {
        "config": config,
        "services": services,
        "requests": requests,
    }
    fingerprint = sha256_obj(fingerprint_source)
    blobs: dict[str, Any] = {
        "config": config,
        "services": services,
        "requests": requests,
    }
    if clock_steps is not None:
        blobs["clock_steps"] = clock_steps
    return {
        "config": config,
        "services": services,
        "requests": requests,
        "clock_steps": clock_steps,
        "fingerprint": fingerprint,
        "blobs": blobs,
    }


def input_fingerprint(blobs: dict[str, Any]) -> str:
    """Fingerprint only business inputs which determine the ledger genesis."""

    return sha256_obj(
        {
            "config": blobs["config"],
            "services": blobs["services"],
            "requests": blobs["requests"],
        }
    )


def canonical_blob(payload: Any) -> str:
    return canonical_json(payload)
