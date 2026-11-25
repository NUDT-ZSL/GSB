from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

from .errors import CapDiagError, ErrorCode
from .jsonutil import load_json
from .model import InstanceSpec, Scenario, ServiceSpec, TraceBundle, TraceRecord


KNOWN_METRICS = (
    "cpu_utilization",
    "memory_utilization",
    "latency_p95_ms",
    "throughput_attainment",
)


def is_real_number(value: Any) -> bool:
    return isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)


def parse_decimal(value: Any, *, field_name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise CapDiagError(
            ErrorCode.SCHEMA,
            f"{field_name} must be a decimal number or numeric string",
            details={"field": field_name, "value": repr(value)},
        )
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise CapDiagError(
            ErrorCode.SCHEMA,
            f"{field_name} is not a valid decimal",
            details={"field": field_name, "value": repr(value)},
        ) from exc
    if not result.is_finite():
        raise CapDiagError(
            ErrorCode.SCHEMA,
            f"{field_name} must be finite",
            details={"field": field_name},
        )
    return result


def require_positive_decimal(value: Any, field_name: str) -> Decimal:
    result = parse_decimal(value, field_name=field_name)
    if result <= 0:
        raise CapDiagError(
            ErrorCode.SCHEMA,
            f"{field_name} must be positive",
            details={"field": field_name, "value": str(result)},
        )
    return result


def require_nonnegative_decimal(value: Any, field_name: str) -> Decimal:
    result = parse_decimal(value, field_name=field_name)
    if result < 0:
        raise CapDiagError(
            ErrorCode.SCHEMA,
            f"{field_name} must be non-negative",
            details={"field": field_name, "value": str(result)},
        )
    return result


def require_nonnegative_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CapDiagError(
            ErrorCode.SCHEMA,
            f"{field_name} must be a non-negative integer",
            details={"field": field_name, "value": repr(value)},
        )
    return value


def require_positive_int(value: Any, field_name: str) -> int:
    result = require_nonnegative_int(value, field_name)
    if result <= 0:
        raise CapDiagError(
            ErrorCode.SCHEMA,
            f"{field_name} must be a positive integer",
            details={"field": field_name, "value": repr(value)},
        )
    return result


def load_scenario(path: str | Path) -> Scenario:
    data = load_json(path)
    if not isinstance(data, dict):
        raise CapDiagError(ErrorCode.SCHEMA, "scenario root must be an object")

    raw_services = data.get("services")
    if not isinstance(raw_services, list) or not raw_services:
        raise CapDiagError(ErrorCode.SCHEMA, "scenario.services must be a non-empty list")

    origin = require_nonnegative_int(
        data.get("origin_logical_ms", 0), "origin_logical_ms"
    )
    tolerance = parse_decimal(
        data.get("precision_tolerance", "0.00005"),
        field_name="precision_tolerance",
    )
    if tolerance <= 0:
        raise CapDiagError(ErrorCode.SCHEMA, "precision_tolerance must be positive")

    services: list[ServiceSpec] = []
    instance_to_service: dict[str, str] = {}
    seen_service_names: set[str] = set()

    for index, raw_service in enumerate(raw_services):
        if not isinstance(raw_service, dict):
            raise CapDiagError(
                ErrorCode.SCHEMA,
                "each service must be an object",
                details={"index": index},
            )
        name = raw_service.get("service_name")
        if not isinstance(name, str) or not name:
            raise CapDiagError(
                ErrorCode.SCHEMA,
                "service_name must be a non-empty string",
                details={"index": index},
            )
        if name in seen_service_names:
            raise CapDiagError(
                ErrorCode.DUPLICATE_SERVICE,
                f"duplicate service name: {name}",
                details={"service_name": name},
            )
        seen_service_names.add(name)

        level = raw_service.get("service_level")
        if not isinstance(level, str) or not level:
            raise CapDiagError(
                ErrorCode.SCHEMA,
                "service_level must be a non-empty string",
                details={"service_name": name},
            )
        service_rate = require_nonnegative_decimal(
            raw_service.get("expected_request_rate"),
            f"services[{index}].expected_request_rate",
        )

        raw_instances = raw_service.get("instances")
        if not isinstance(raw_instances, list) or not raw_instances:
            raise CapDiagError(
                ErrorCode.SCHEMA,
                "service.instances must be a non-empty list",
                details={"service_name": name},
            )

        instances: list[InstanceSpec] = []
        seen_instance_ids: set[str] = set()
        for inst_index, raw_instance in enumerate(raw_instances):
            if not isinstance(raw_instance, dict):
                raise CapDiagError(
                    ErrorCode.SCHEMA,
                    "instance must be an object",
                    details={"service_name": name, "index": inst_index},
                )
            instance_id = raw_instance.get("instance_id")
            if not isinstance(instance_id, str) or not instance_id:
                raise CapDiagError(
                    ErrorCode.SCHEMA,
                    "instance_id must be a non-empty string",
                    details={"service_name": name, "index": inst_index},
                )
            if instance_id in instance_to_service:
                raise CapDiagError(
                    ErrorCode.DUPLICATE_INSTANCE,
                    f"duplicate instance_id: {instance_id}",
                    details={
                        "instance_id": instance_id,
                        "first_service": instance_to_service[instance_id],
                        "second_service": name,
                    },
                )
            if instance_id in seen_instance_ids:
                raise CapDiagError(
                    ErrorCode.DUPLICATE_INSTANCE,
                    f"duplicate instance_id in service: {instance_id}",
                    details={"instance_id": instance_id, "service_name": name},
                )
            seen_instance_ids.add(instance_id)
            instance_rate = require_nonnegative_decimal(
                raw_instance.get("expected_request_rate", service_rate),
                f"instances[{inst_index}].expected_request_rate",
            )
            instances.append(InstanceSpec(instance_id, instance_rate))
            instance_to_service[instance_id] = name

        services.append(
            ServiceSpec(
                service_name=name,
                service_level=level,
                expected_request_rate=service_rate,
                instances=tuple(instances),
            )
        )

    return Scenario(
        scenario_id=str(data.get("scenario_id", "scenario")),
        origin_logical_ms=origin,
        precision_tolerance=tolerance,
        services=tuple(services),
        instance_to_service=instance_to_service,
    )


def _trace_error(
    line_no: int,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "line_number": line_no,
        "error_code": code,
        "error": message,
        "details": details or {},
    }


def parse_trace_lines(lines: Iterable[str], scenario: Scenario) -> TraceBundle:
    bundle = TraceBundle()
    last_seq_by_instance: dict[str, int] = {}

    for line_number, line in enumerate(lines, start=1):
        text = line.strip()
        if not text:
            continue
        try:
            row = json.loads(text, parse_float=Decimal)
        except json.JSONDecodeError as exc:
            bundle.errors.append(
                _trace_error(
                    line_number,
                    ErrorCode.SCHEMA,
                    "trace line is not valid JSON",
                    {"json_error": str(exc)},
                )
            )
            continue

        if not isinstance(row, dict):
            bundle.errors.append(
                _trace_error(line_number, ErrorCode.SCHEMA, "trace row must be an object")
            )
            continue

        instance_id = row.get("instance_id")
        service_name = row.get("service_name")
        if not isinstance(instance_id, str) or not isinstance(service_name, str):
            bundle.errors.append(
                _trace_error(
                    line_number,
                    ErrorCode.SCHEMA,
                    "service_name and instance_id must be strings",
                )
            )
            continue

        expected_service = scenario.service_for_instance(instance_id)
        if expected_service is None or service_name != expected_service:
            bundle.errors.append(
                _trace_error(
                    line_number,
                    ErrorCode.SCHEMA,
                    "trace service/instance is not declared in scenario",
                    {
                        "service_name": service_name,
                        "instance_id": instance_id,
                        "declared_service": expected_service,
                    },
                )
            )
            continue

        try:
            seq = row.get("request_seq")
            if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
                raise CapDiagError(
                    ErrorCode.TRACE_SEQUENCE,
                    "request_seq must be a non-negative integer",
                    {"value": repr(seq)},
                )
            previous = last_seq_by_instance.get(instance_id, -1)
            if seq <= previous:
                raise CapDiagError(
                    ErrorCode.TRACE_SEQUENCE,
                    "request_seq must be strictly increasing within instance",
                    {"instance_id": instance_id, "previous": previous, "actual": seq},
                )

            clock = row.get("logical_clock_ms")
            if isinstance(clock, bool) or not isinstance(clock, int) or clock < 0:
                raise CapDiagError(
                    ErrorCode.TRACE_CLOCK,
                    "logical_clock_ms must be a non-negative integer",
                    {"value": repr(clock)},
                )

            try:
                duration = require_nonnegative_decimal(row.get("duration_ms"), "duration_ms")
            except CapDiagError as exc:
                raise CapDiagError(
                    ErrorCode.TRACE_FIELD,
                    "duration_ms must be a non-negative decimal number",
                    exc.details,
                ) from exc

            success = row.get("success")
            dropped = row.get("dropped")
            if not isinstance(success, bool) or not isinstance(dropped, bool):
                raise CapDiagError(
                    ErrorCode.TRACE_FIELD,
                    "success and dropped must be booleans",
                    {"success": repr(success), "dropped": repr(dropped)},
                )
        except CapDiagError as exc:
            bundle.errors.append(
                _trace_error(
                    line_number,
                    exc.code,
                    exc.message,
                    {**exc.details, "service_name": service_name, "instance_id": instance_id},
                )
            )
            continue

        last_seq_by_instance[instance_id] = seq
        bundle.records.append(
            TraceRecord(
                request_seq=seq,
                service_name=service_name,
                instance_id=instance_id,
                logical_clock_ms=clock,
                duration_ms=duration,
                success=success,
                dropped=dropped,
                source_line=line_number,
            )
        )

    return bundle


def load_trace(path: str | Path, scenario: Scenario) -> TraceBundle:
    with Path(path).open("r", encoding="utf-8") as handle:
        return parse_trace_lines(handle, scenario)


def load_metric_config(path: str | Path) -> dict:
    data = load_json(path)
    validate_metric_config(data)
    return data


def validate_metric_config(data: Any) -> None:
    if not isinstance(data, dict):
        raise CapDiagError(ErrorCode.SCHEMA, "metric config root must be an object")
    metrics = data.get("metrics")
    if not isinstance(metrics, dict):
        raise CapDiagError(ErrorCode.SCHEMA, "metric config must contain metrics object")

    missing = [name for name in KNOWN_METRICS if name not in metrics]
    if missing:
        raise CapDiagError(
            ErrorCode.SCHEMA,
            "metric config is missing required metrics",
            {"missing": missing},
        )

    canonical_window: tuple[int, int] | None = None
    for name, definition in metrics.items():
        if name not in KNOWN_METRICS:
            raise CapDiagError(
                ErrorCode.SCHEMA,
                f"unknown metric definition: {name}",
                {"known_metrics": list(KNOWN_METRICS)},
            )
        if not isinstance(definition, dict):
            raise CapDiagError(ErrorCode.SCHEMA, f"metric {name} must be an object")
        scale = definition.get("scale")
        if isinstance(scale, bool) or not isinstance(scale, int) or scale <= 0:
            raise CapDiagError(
                ErrorCode.METRIC_SCALE,
                f"metrics.{name}.scale must be a positive integer",
                details={"metric": name, "scale": repr(scale)},
            )
        length = require_positive_int(
            definition.get("window_length_ms"),
            f"metrics.{name}.window_length_ms",
        )
        step = require_positive_int(
            definition.get("window_step_ms"),
            f"metrics.{name}.window_step_ms",
        )
        if length < step:
            raise CapDiagError(
                ErrorCode.WINDOW_CONFIG,
                "window_length_ms must be greater than or equal to window_step_ms",
                {"metric": name, "length": length, "step": step},
            )
        window = (length, step)
        if canonical_window is None:
            canonical_window = window
        elif window != canonical_window:
            raise CapDiagError(
                ErrorCode.WINDOW_CONFIG,
                "all metric definitions must declare the same window partition",
                {"metric": name, "window": list(window), "canonical": list(canonical_window)},
            )

        kind = definition.get("kind")
        if kind not in {"linear_ratio", "latency_p95", "throughput_ratio"}:
            raise CapDiagError(
                ErrorCode.SCHEMA,
                f"metric {name} has unsupported kind",
                {"kind": kind},
            )

    if metrics["cpu_utilization"]["kind"] != "linear_ratio":
        raise CapDiagError(ErrorCode.SCHEMA, "cpu_utilization must use linear_ratio")
    if metrics["memory_utilization"]["kind"] != "linear_ratio":
        raise CapDiagError(ErrorCode.SCHEMA, "memory_utilization must use linear_ratio")
    if metrics["latency_p95_ms"]["kind"] != "latency_p95":
        raise CapDiagError(ErrorCode.SCHEMA, "latency_p95_ms must use latency_p95")
    if metrics["throughput_attainment"]["kind"] != "throughput_ratio":
        raise CapDiagError(
            ErrorCode.SCHEMA,
            "throughput_attainment must use throughput_ratio",
        )
