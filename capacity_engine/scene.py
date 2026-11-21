from __future__ import annotations

import ipaddress
import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping
from urllib.parse import urlsplit

from .errors import BizError, validation_error
from .util import (
    canonical_json,
    decimal_str,
    is_int,
    parse_bool,
    parse_decimal,
    parse_nonnegative_int,
    parse_positive_int,
    require_list,
    require_mapping,
    sha256_text,
)


@dataclass(frozen=True)
class TenantConfig:
    name: str
    service: str
    success_rate_min: Decimal
    p95_latency_max_ms: Decimal
    concurrency_limit: int
    success_status_codes: tuple[int, ...]
    source: str


@dataclass(frozen=True)
class ServiceConfig:
    name: str
    address: str
    scheme: str
    host: str
    port: int
    base_path: str
    tenants: tuple[str, ...]


@dataclass(frozen=True)
class BatchPlan:
    batch_id: str
    service: str
    tenant: str
    concurrency_gradients: tuple[int, ...]
    duration_logical_ms: int
    request_pace_ms: int
    allow_early_termination: bool
    source: str


@dataclass(frozen=True)
class ClockConfig:
    start_ms: int
    window_ms: int
    step_table: tuple[int, ...]
    gradient_gap_ms: int


@dataclass(frozen=True)
class ToleranceConfig:
    success_rate: Decimal
    p95_latency_ms: Decimal
    degraded_success_rate: Decimal
    degraded_p95_latency_ms: Decimal


@dataclass(frozen=True)
class ErrorBudget:
    success_rate: Decimal
    p95_latency_ms: Decimal


@dataclass(frozen=True)
class PrecisionConfig:
    latency_scale: int
    percent_scale: int
    compare_tolerance: ToleranceConfig
    error_budget: ErrorBudget
    request_logical_budget_ms: Decimal


@dataclass(frozen=True)
class Scene:
    scene_id: str
    services: Mapping[str, ServiceConfig]
    tenants: Mapping[str, TenantConfig]
    batches: tuple[BatchPlan, ...]
    clock: ClockConfig
    precision: PrecisionConfig
    global_logical_time_ms: int
    document: Mapping[str, Any]
    canonical_text: str
    sha256: str

    @property
    def tenant_concurrency_limits(self) -> dict[str, int]:
        return {name: tenant.concurrency_limit for name, tenant in self.tenants.items()}


def load_scene(path: Path) -> Scene:
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise BizError(
            "E_SCENE_JSON",
            f"cannot read scene file: {exc}",
            details={"path": str(path)},
        ) from exc
    try:
        document = json.loads(raw_text, parse_float=Decimal)
    except Exception as exc:
        raise BizError(
            "E_SCENE_JSON",
            f"scene file is not valid JSON: {exc}",
            details={"path": str(path)},
        ) from exc
    return parse_scene(document)


def parse_scene(document: Any) -> Scene:
    root = require_mapping(document, "$")
    services_raw = require_list(root.get("services"), "services")
    batches_raw = require_list(root.get("batches"), "batches")
    clock_raw = require_mapping(root.get("clock"), "clock")
    precision_raw = require_mapping(root.get("precision"), "precision")

    scene_id = root.get("scene_id", "scene")
    if not isinstance(scene_id, str) or not scene_id:
        raise validation_error("E_SCENE_SHAPE", "scene_id must be a non-empty string", field="scene_id")

    services: dict[str, ServiceConfig] = {}
    tenants: dict[str, TenantConfig] = {}
    for service_index, item in enumerate(services_raw):
        source = f"services[{service_index}]"
        service_obj = require_mapping(item, source)
        name = service_obj.get("name")
        if not isinstance(name, str) or not name:
            raise validation_error("E_SCENE_SHAPE", "service name must be a non-empty string", field=f"{source}.name")
        if name in services:
            raise validation_error(
                "E_DUPLICATE_SERVICE",
                f"duplicate service name: {name}",
                field=f"{source}.name",
                service=name,
            )
        address_info = _parse_loopback_address(service_obj.get("address"), f"{source}.address")
        default_codes = service_obj.get("success_status_codes", list(range(200, 300)))
        if not isinstance(default_codes, list) or not default_codes:
            raise validation_error(
                "E_INVALID_SLO",
                "success_status_codes must be a non-empty integer array",
                field=f"{source}.success_status_codes",
            )
        status_codes: list[int] = []
        for code_value in default_codes:
            if not is_int(code_value) or code_value < 100 or code_value > 599:
                raise validation_error(
                    "E_INVALID_SLO",
                    "success status codes must be integers in [100, 599]",
                    field=f"{source}.success_status_codes",
                )
            status_codes.append(code_value)
        if len(set(status_codes)) != len(status_codes):
            raise validation_error(
                "E_INVALID_SLO",
                "success_status_codes must not contain duplicates",
                field=f"{source}.success_status_codes",
            )

        tenant_entries = service_obj.get("tenants")
        if isinstance(tenant_entries, dict):
            tenant_items = list(tenant_entries.items())
            tenant_objects = []
            for tenant_name, tenant_value in tenant_items:
                if not isinstance(tenant_name, str) or not tenant_name:
                    raise validation_error(
                        "E_INVALID_SLO",
                        "tenant name must be a non-empty string",
                        field=f"{source}.tenants",
                    )
                tenant_obj = dict(require_mapping(tenant_value, f"{source}.tenants.{tenant_name}"))
                tenant_obj.setdefault("name", tenant_name)
                tenant_objects.append((tenant_name, tenant_obj))
        elif isinstance(tenant_entries, list):
            tenant_objects = []
            for tenant_index, tenant_value in enumerate(tenant_entries):
                tenant_obj = require_mapping(tenant_value, f"{source}.tenants[{tenant_index}]")
                tenant_name = tenant_obj.get("name")
                if not isinstance(tenant_name, str) or not tenant_name:
                    raise validation_error(
                        "E_INVALID_SLO",
                        "tenant name must be a non-empty string",
                        field=f"{source}.tenants[{tenant_index}].name",
                    )
                tenant_objects.append((tenant_name, tenant_obj))
        else:
            raise validation_error(
                "E_SCENE_SHAPE",
                "service tenants must be an object or array",
                field=f"{source}.tenants",
            )

        service_tenant_names: list[str] = []
        for tenant_name, tenant_obj in tenant_objects:
            tenant_source = f"{source}.tenants.{tenant_name}"
            if tenant_name in tenants:
                raise validation_error(
                    "E_DUPLICATE_TENANT",
                    f"duplicate tenant name: {tenant_name}",
                    field=tenant_source,
                    tenant=tenant_name,
                )
            success_rate_min = _parse_success_threshold(tenant_obj, tenant_source)
            p95_raw = tenant_obj.get("p95_latency_max_ms", tenant_obj.get("p95_latency_max"))
            p95_latency_max = parse_decimal(p95_raw, f"{tenant_source}.p95_latency_max_ms", "E_INVALID_SLO")
            if p95_latency_max <= 0:
                raise validation_error(
                    "E_INVALID_SLO",
                    "p95 latency upper bound must be positive",
                    field=f"{tenant_source}.p95_latency_max_ms",
                )
            limit_value = tenant_obj.get("concurrency_limit", service_obj.get("concurrency_limit", 1))
            concurrency_limit = parse_positive_int(
                limit_value,
                f"{tenant_source}.concurrency_limit",
                "E_INVALID_SLO",
            )
            tenant_codes = tenant_obj.get("success_status_codes", status_codes)
            if not isinstance(tenant_codes, list) or not tenant_codes:
                raise validation_error(
                    "E_INVALID_SLO",
                    "tenant success_status_codes must be a non-empty integer array",
                    field=f"{tenant_source}.success_status_codes",
                )
            final_codes: list[int] = []
            for code_value in tenant_codes:
                if not is_int(code_value) or code_value < 100 or code_value > 599:
                    raise validation_error(
                        "E_INVALID_SLO",
                        "success status codes must be integers in [100, 599]",
                        field=f"{tenant_source}.success_status_codes",
                    )
                final_codes.append(code_value)
            tenant = TenantConfig(
                name=tenant_name,
                service=name,
                success_rate_min=success_rate_min,
                p95_latency_max_ms=p95_latency_max,
                concurrency_limit=concurrency_limit,
                success_status_codes=tuple(sorted(set(final_codes))),
                source=tenant_source,
            )
            tenants[tenant_name] = tenant
            service_tenant_names.append(tenant_name)

        if not service_tenant_names:
            raise validation_error(
                "E_INVALID_SLO",
                "service must declare at least one tenant",
                field=f"{source}.tenants",
            )
        services[name] = ServiceConfig(
            name=name,
            address=address_info["address"],
            scheme=address_info["scheme"],
            host=address_info["host"],
            port=address_info["port"],
            base_path=address_info["base_path"],
            tenants=tuple(service_tenant_names),
        )

    if not services:
        raise validation_error("E_INVALID_SLO", "at least one service is required", field="services")

    batches: list[BatchPlan] = []
    batch_ids: set[str] = set()
    for batch_index, item in enumerate(batches_raw):
        source = f"batches[{batch_index}]"
        batch_obj = require_mapping(item, source)
        batch_id = batch_obj.get("batch_id", batch_obj.get("batch_no"))
        if not isinstance(batch_id, str) or not batch_id:
            raise validation_error("E_INVALID_BATCH", "batch_id must be a non-empty string", field=f"{source}.batch_id")
        if batch_id in batch_ids:
            raise validation_error("E_DUPLICATE_BATCH", f"duplicate batch_id: {batch_id}", field=f"{source}.batch_id")
        batch_ids.add(batch_id)
        tenant_name = batch_obj.get("tenant")
        if not isinstance(tenant_name, str) or tenant_name not in tenants:
            raise validation_error(
                "E_UNKNOWN_TENANT",
                f"unknown tenant: {tenant_name}",
                field=f"{source}.tenant",
                tenant=tenant_name,
            )
        tenant = tenants[tenant_name]
        service_name = batch_obj.get("service", tenant.service)
        if not isinstance(service_name, str) or service_name not in services:
            raise validation_error(
                "E_UNKNOWN_SERVICE",
                f"unknown service: {service_name}",
                field=f"{source}.service",
                service=service_name,
            )
        if service_name != tenant.service:
            raise validation_error(
                "E_SERVICE_TENANT_MISMATCH",
                f"tenant {tenant_name} belongs to service {tenant.service}, not {service_name}",
                field=f"{source}.service",
                tenant=tenant_name,
                expected=tenant.service,
                actual=service_name,
            )
        gradients_raw = batch_obj.get(
            "concurrency_gradients",
            batch_obj.get("gradients", batch_obj.get("concurrency_gradient")),
        )
        gradients_values = require_list(gradients_raw, f"{source}.concurrency_gradients")
        gradients: list[int] = []
        for gradient_index, value in enumerate(gradients_values):
            gradient = parse_positive_int(value, f"{source}.concurrency_gradients[{gradient_index}]", "E_INVALID_GRADIENT")
            gradients.append(gradient)
        if not gradients:
            raise validation_error(
                "E_INVALID_GRADIENT",
                "concurrency gradient sequence must not be empty",
                field=f"{source}.concurrency_gradients",
            )
        for index in range(1, len(gradients)):
            if gradients[index] <= gradients[index - 1]:
                raise validation_error(
                    "E_INVALID_GRADIENT",
                    "concurrency gradients must be strictly increasing",
                    field=f"{source}.concurrency_gradients[{index}]",
                    previous=gradients[index - 1],
                    current=gradients[index],
                )
        duration = parse_positive_int(
            batch_obj.get("duration_logical_ms", batch_obj.get("duration_ms")),
            f"{source}.duration_logical_ms",
            "E_INVALID_DURATION",
        )
        pace = parse_positive_int(
            batch_obj.get("request_pace_ms", batch_obj.get("pace_ms")),
            f"{source}.request_pace_ms",
            "E_INVALID_PACE",
        )
        allow_early = parse_bool(
            batch_obj.get("allow_early_termination", False),
            f"{source}.allow_early_termination",
            "E_INVALID_BATCH",
        )
        batches.append(
            BatchPlan(
                batch_id=batch_id,
                service=service_name,
                tenant=tenant_name,
                concurrency_gradients=tuple(gradients),
                duration_logical_ms=duration,
                request_pace_ms=pace,
                allow_early_termination=allow_early,
                source=source,
            )
        )
    if not batches:
        raise validation_error("E_INVALID_BATCH", "at least one batch is required", field="batches")

    start_ms = parse_nonnegative_int(clock_raw.get("start_ms", 0), "clock.start_ms", "E_INVALID_CLOCK")
    window_ms = parse_positive_int(clock_raw.get("window_ms"), "clock.window_ms", "E_INVALID_CLOCK")
    steps_raw = clock_raw.get("step_table", clock_raw.get("steps", []))
    steps_list = require_list(steps_raw, "clock.step_table")
    steps: list[int] = []
    for index, value in enumerate(steps_list):
        step = parse_positive_int(value, f"clock.step_table[{index}]", "E_INVALID_CLOCK")
        steps.append(step)
    gradient_gap_ms = parse_nonnegative_int(
        clock_raw.get("gradient_gap_ms", 0),
        "clock.gradient_gap_ms",
        "E_INVALID_CLOCK",
    )
    clock = ClockConfig(
        start_ms=start_ms,
        window_ms=window_ms,
        step_table=tuple(steps),
        gradient_gap_ms=gradient_gap_ms,
    )

    latency_scale = parse_positive_int(precision_raw.get("latency_scale"), "precision.latency_scale", "E_INVALID_SCALE")
    percent_scale = parse_positive_int(precision_raw.get("percent_scale"), "precision.percent_scale", "E_INVALID_SCALE")
    tolerance = _parse_tolerance(precision_raw.get("compare_tolerance"), "precision.compare_tolerance")
    degraded_raw = precision_raw.get("degraded_margin")
    if degraded_raw is None:
        degraded_success = tolerance.success_rate / Decimal(10)
        degraded_p95 = tolerance.p95_latency_ms / Decimal(10)
    else:
        degraded = _parse_tolerance(degraded_raw, "precision.degraded_margin", require_typed=False)
        degraded_success = degraded.success_rate
        degraded_p95 = degraded.p95_latency_ms
    if degraded_success < 0 or degraded_p95 < 0:
        raise validation_error(
            "E_INVALID_TOLERANCE",
            "degraded margins must be non-negative",
            field="precision.degraded_margin",
        )
    tolerance_config = ToleranceConfig(
        success_rate=tolerance.success_rate,
        p95_latency_ms=tolerance.p95_latency_ms,
        degraded_success_rate=degraded_success,
        degraded_p95_latency_ms=degraded_p95,
    )
    budget_value = _parse_error_budget(precision_raw.get("error_budget"), "precision.error_budget")
    request_budget = parse_decimal(
        precision_raw.get("request_logical_budget_ms", 1000),
        "precision.request_logical_budget_ms",
        "E_INVALID_BUDGET",
    )
    if request_budget <= 0:
        raise validation_error(
            "E_INVALID_BUDGET",
            "request logical budget must be positive",
            field="precision.request_logical_budget_ms",
        )
    precision = PrecisionConfig(
        latency_scale=latency_scale,
        percent_scale=percent_scale,
        compare_tolerance=tolerance_config,
        error_budget=budget_value,
        request_logical_budget_ms=request_budget,
    )

    budget_raw = root.get("budget", {})
    budget_obj = require_mapping(budget_raw, "budget") if budget_raw is not None else {}
    global_budget_value = budget_obj.get("global_logical_time_ms", 10**12)
    global_budget = parse_positive_int(
        global_budget_value,
        "budget.global_logical_time_ms",
        "E_INVALID_BUDGET",
    )

    normalized_document = _normalize_json_value(root)
    canonical = canonical_json(normalized_document)
    return Scene(
        scene_id=scene_id,
        services=MappingProxyType(services),
        tenants=MappingProxyType(tenants),
        batches=tuple(batches),
        clock=clock,
        precision=precision,
        global_logical_time_ms=global_budget,
        document=MappingProxyType(normalized_document),
        canonical_text=canonical,
        sha256=sha256_text(canonical),
    )


def scene_document_from_stored(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text, parse_float=Decimal)
    except Exception as exc:
        raise BizError("E_SCHEMA_MISMATCH", f"stored scene snapshot is invalid: {exc}") from exc
    return require_mapping(value, "stored_scene")


def _parse_loopback_address(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, str) or not value:
        raise validation_error("E_INVALID_ADDRESS", "service address must be a non-empty string", field=field)
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"}:
        raise validation_error(
            "E_INVALID_ADDRESS",
            "service address must use http or https",
            field=field,
            address=value,
        )
    host = parsed.hostname
    if not host:
        raise validation_error("E_INVALID_ADDRESS", "service address must include a host", field=field, address=value)
    loopback = host.lower() in {"localhost"} or host == "::1"
    if not loopback:
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = False
    if not loopback:
        raise validation_error(
            "E_INVALID_ADDRESS",
            "service address must be a loopback address",
            field=field,
            address=value,
            host=host,
        )
    try:
        port = parsed.port
    except ValueError as exc:
        raise validation_error("E_INVALID_ADDRESS", "service address has an invalid port", field=field, address=value) from exc
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    if port <= 0 or port > 65535:
        raise validation_error("E_INVALID_ADDRESS", "service port is out of range", field=field, address=value)
    base_path = parsed.path or ""
    if base_path and not base_path.startswith("/"):
        raise validation_error("E_INVALID_ADDRESS", "service base path must start with /", field=field, address=value)
    return {
        "address": value.rstrip("/"),
        "scheme": parsed.scheme,
        "host": host,
        "port": port,
        "base_path": base_path.rstrip("/"),
    }


def _parse_success_threshold(tenant_obj: Mapping[str, Any], source: str) -> Decimal:
    if "success_rate_min" in tenant_obj:
        value = parse_decimal(tenant_obj["success_rate_min"], f"{source}.success_rate_min", "E_INVALID_SLO")
    elif "success_rate_min_percent" in tenant_obj:
        percent = parse_decimal(
            tenant_obj["success_rate_min_percent"],
            f"{source}.success_rate_min_percent",
            "E_INVALID_SLO",
        )
        value = percent / Decimal(100)
    else:
        raise validation_error(
            "E_INVALID_SLO",
            "tenant success rate lower bound is required",
            field=f"{source}.success_rate_min",
        )
    if value < 0 or value > 1:
        raise validation_error(
            "E_INVALID_SLO",
            "success rate lower bound must be in [0, 1]",
            field=f"{source}.success_rate_min",
            value=decimal_str(value),
        )
    return value


def _parse_tolerance(value: Any, field: str, require_typed: bool = True) -> ToleranceConfig:
    if isinstance(value, dict):
        success_raw = value.get("success_rate", value.get("success_rate_scaled"))
        p95_raw = value.get("p95_latency_ms", value.get("p95_latency_scaled"))
        if success_raw is None or p95_raw is None:
            raise validation_error(
                "E_INVALID_TOLERANCE",
                "compare tolerance object must contain success_rate and p95_latency_ms",
                field=field,
            )
        success = parse_decimal(success_raw, f"{field}.success_rate", "E_INVALID_TOLERANCE")
        p95 = parse_decimal(p95_raw, f"{field}.p95_latency_ms", "E_INVALID_TOLERANCE")
    elif require_typed or not isinstance(value, dict):
        scalar = parse_decimal(value, field, "E_INVALID_TOLERANCE")
        success = scalar
        p95 = scalar
    else:
        raise validation_error("E_INVALID_TOLERANCE", "invalid tolerance", field=field)
    if success <= 0 or p95 <= 0:
        raise validation_error(
            "E_INVALID_TOLERANCE",
            "comparison tolerance must be positive",
            field=field,
        )
    return ToleranceConfig(
        success_rate=success,
        p95_latency_ms=p95,
        degraded_success_rate=Decimal(0),
        degraded_p95_latency_ms=Decimal(0),
    )


def _parse_error_budget(value: Any, field: str) -> ErrorBudget:
    if isinstance(value, dict):
        success_raw = value.get("success_rate", value.get("success_rate_scaled"))
        p95_raw = value.get("p95_latency_ms", value.get("p95_latency_scaled"))
        if success_raw is None or p95_raw is None:
            raise validation_error(
                "E_INVALID_BUDGET",
                "error budget object must contain success_rate and p95_latency_ms",
                field=field,
            )
        success = parse_decimal(success_raw, f"{field}.success_rate", "E_INVALID_BUDGET")
        p95 = parse_decimal(p95_raw, f"{field}.p95_latency_ms", "E_INVALID_BUDGET")
    else:
        scalar = parse_decimal(value, field, "E_INVALID_BUDGET")
        success = scalar
        p95 = scalar
    if success <= 0 or p95 <= 0:
        raise validation_error("E_INVALID_BUDGET", "error budget must be positive", field=field)
    return ErrorBudget(success_rate=success, p95_latency_ms=p95)


def _normalize_json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return decimal_str(value)
    if isinstance(value, dict):
        return {str(key): _normalize_json_value(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, list):
        return [_normalize_json_value(item) for item in value]
    return value

