from __future__ import annotations

import copy
import hashlib
import ipaddress
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .clock import ClockSegment, ClockSpec
from .errors import ValidationError, err


ERROR_CATEGORIES: tuple[str, ...] = (
    "status_mismatch",
    "connection_refused",
    "request_timeout",
    "malformed_response",
    "target_service_interrupted",
)


@dataclass(frozen=True)
class SLO:
    min_success_rate: float
    max_p95_latency_ms: float
    degraded_success_margin: float = 0.01
    degraded_latency_ratio: float = 1.20


@dataclass(frozen=True)
class Tenant:
    name: str
    slo: SLO
    profile: dict[str, Any]
    max_parallel_gradients: int = 1


@dataclass(frozen=True)
class Service:
    name: str
    host: str
    port: int
    expected_status: int
    tenants: tuple[Tenant, ...]
    refused_port: int | None = None


@dataclass(frozen=True)
class GradientSpec:
    index: int
    concurrency: int
    duration_ms: int
    pace_ms: int
    window_ms: int
    request_timeout_ms: int
    logical_start_ms: int

    @property
    def window_count(self) -> int:
        return math.ceil(self.duration_ms / self.window_ms)

    def window_span(self, window_index: int) -> tuple[int, int]:
        local_start = window_index * self.window_ms
        local_end = min(self.duration_ms, local_start + self.window_ms)
        return (
            self.logical_start_ms + local_start,
            self.logical_start_ms + local_end,
        )


@dataclass(frozen=True)
class BatchPlan:
    batch_id: str
    tenant: str
    gradients: tuple[GradientSpec, ...]
    allow_early_termination: bool


@dataclass(frozen=True)
class Scenario:
    version: int
    seed: int
    services: tuple[Service, ...]
    plans: tuple[BatchPlan, ...]
    clock: ClockSpec
    global_time_budget_ms: int
    raw: dict[str, Any]
    source_path: str | None = None

    @property
    def tenants(self) -> tuple[Tenant, ...]:
        return tuple(tenant for service in self.services for tenant in service.tenants)

    @property
    def tenant_map(self) -> dict[str, Tenant]:
        return {tenant.name: tenant for tenant in self.tenants}

    @property
    def tenant_service_map(self) -> dict[str, Service]:
        return {
            tenant.name: service
            for service in self.services
            for tenant in service.tenants
        }

    @property
    def plan_map(self) -> dict[str, BatchPlan]:
        return {plan.tenant: plan for plan in self.plans}

    def canonical_json(self) -> str:
        return json.dumps(
            self.raw,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def scenario_hash(self, seed: int | None = None) -> str:
        effective_seed = self.seed if seed is None else int(seed)
        payload = f"{self.canonical_json()}\nseed={effective_seed}".encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def default_run_id(self, seed: int | None = None) -> str:
        return "run-" + self.scenario_hash(seed)[:20]


def _require_dict(value: Any, *, path: str, code: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise err(code, "expected a JSON object", path=path, value=value)
    return value


def _require_list(value: Any, *, path: str, code: str) -> list[Any]:
    if not isinstance(value, list):
        raise err(code, "expected a JSON array", path=path, value=value)
    return value


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _positive_int(value: Any, *, path: str, code: str = "E_BAD_PLAN") -> int:
    if not _is_int(value) or value <= 0:
        raise err(code, "must be a positive integer", path=path, value=value)
    return value


def _nonnegative_number(value: Any, *, path: str, code: str) -> float:
    if not _is_number(value) or float(value) < 0:
        raise err(code, "must be a finite non-negative number", path=path, value=value)
    return float(value)


def _pick(source: dict[str, Any], names: Iterable[str], default: Any = None) -> Any:
    for name in names:
        if name in source:
            return source[name]
    return default


def _parse_slo(tenant_raw: dict[str, Any], *, path: str) -> SLO:
    slo_raw = _pick(tenant_raw, ("slo", "thresholds"))
    if not isinstance(slo_raw, dict):
        raise err("E_BAD_SLO", "tenant SLO object is required", path=f"{path}.slo")

    success = _pick(
        slo_raw,
        (
            "min_success_rate",
            "success_rate_min",
            "success_rate_minimum",
            "min_success",
        ),
    )
    latency = _pick(
        slo_raw,
        (
            "max_p95_latency_ms",
            "p95_latency_ms_max",
            "p95_latency_max_ms",
            "p95_limit_ms",
        ),
    )
    if not _is_number(success) or not (0.0 < float(success) <= 1.0):
        raise err(
            "E_BAD_SLO",
            "min_success_rate must be in (0, 1]",
            path=f"{path}.slo.min_success_rate",
            value=success,
        )
    if not _is_number(latency) or float(latency) <= 0:
        raise err(
            "E_BAD_SLO",
            "max_p95_latency_ms must be a positive number",
            path=f"{path}.slo.max_p95_latency_ms",
            value=latency,
        )

    success_margin = _pick(
        slo_raw,
        ("degraded_success_margin", "degrade_success_margin"),
        0.01,
    )
    latency_ratio = _pick(
        slo_raw,
        ("degraded_latency_ratio", "degrade_latency_ratio"),
        1.20,
    )
    if not _is_number(success_margin) or not (0.0 <= float(success_margin) < 1.0):
        raise err(
            "E_BAD_SLO",
            "degraded_success_margin must be in [0, 1)",
            path=f"{path}.slo.degraded_success_margin",
            value=success_margin,
        )
    if not _is_number(latency_ratio) or float(latency_ratio) < 1.0:
        raise err(
            "E_BAD_SLO",
            "degraded_latency_ratio must be >= 1",
            path=f"{path}.slo.degraded_latency_ratio",
            value=latency_ratio,
        )
    return SLO(
        min_success_rate=float(success),
        max_p95_latency_ms=float(latency),
        degraded_success_margin=float(success_margin),
        degraded_latency_ratio=float(latency_ratio),
    )


def _validate_profile(profile: Any, *, path: str) -> dict[str, Any]:
    if profile is None:
        return {"status_distribution": {"200": 1.0}}
    profile = _require_dict(profile, path=path, code="E_BAD_SERVICE")
    normalized = copy.deepcopy(profile)
    distribution = normalized.get("status_distribution")
    if distribution is None:
        normalized["status_distribution"] = {"200": 1.0}
        distribution = normalized["status_distribution"]
    if not isinstance(distribution, dict) or not distribution:
        raise err(
            "E_BAD_SERVICE",
            "status_distribution must be a non-empty object",
            path=f"{path}.status_distribution",
            value=distribution,
        )
    total = 0.0
    clean_distribution: dict[str, float] = {}
    for status, weight in distribution.items():
        try:
            status_int = int(status)
        except (TypeError, ValueError):
            raise err(
                "E_BAD_SERVICE",
                "status key must be an HTTP status integer",
                path=f"{path}.status_distribution",
                value=status,
            )
        if not (100 <= status_int <= 599):
            raise err(
                "E_BAD_SERVICE",
                "HTTP status must be in [100, 599]",
                path=f"{path}.status_distribution",
                value=status,
            )
        if not _is_number(weight) or float(weight) < 0:
            raise err(
                "E_BAD_SERVICE",
                "status weight must be a non-negative finite number",
                path=f"{path}.status_distribution.{status}",
                value=weight,
            )
        clean_distribution[str(status_int)] = float(weight)
        total += float(weight)
    if total <= 0:
        raise err(
            "E_BAD_SERVICE",
            "status distribution weights must have a positive sum",
            path=f"{path}.status_distribution",
        )
    normalized["status_distribution"] = {
        key: value / total for key, value in sorted(clean_distribution.items())
    }

    latency = normalized.get("latency", {})
    if latency is not None:
        if not isinstance(latency, dict):
            raise err(
                "E_BAD_SERVICE",
                "latency profile must be an object",
                path=f"{path}.latency",
                value=latency,
            )
        for key in (
            "base_ms",
            "jitter_ms",
            "per_concurrency_ms",
            "overload_penalty_ms",
            "slow_extra_ms",
        ):
            if key in latency:
                _nonnegative_number(
                    latency[key],
                    path=f"{path}.latency.{key}",
                    code="E_BAD_SERVICE",
                )
    for key in (
        "slow_windows",
        "fault_windows",
        "malformed_windows",
        "refused_windows",
    ):
        value = normalized.get(key, [])
        if value is not None and not isinstance(value, list):
            raise err(
                "E_BAD_SERVICE",
                f"{key} must be an array",
                path=f"{path}.{key}",
                value=value,
            )
    return normalized


def _normalize_tenant(
    raw: Any,
    *,
    service_name: str,
    path: str,
    default_parallel: int,
) -> Tenant:
    raw = _require_dict(raw, path=path, code="E_BAD_SERVICE")
    name = _pick(raw, ("name", "tenant", "tenant_id"))
    if not isinstance(name, str) or not name.strip():
        raise err("E_BAD_SERVICE", "tenant name is required", path=f"{path}.name")
    name = name.strip()
    slo = _parse_slo(raw, path=path)
    profile = _validate_profile(
        _pick(raw, ("profile", "response_profile", "responses"), {}),
        path=f"{path}.profile",
    )
    parallel = _pick(
        raw,
        ("max_parallel_gradients", "parallel_gradients", "concurrency_budget"),
        default_parallel,
    )
    if not _is_int(parallel) or parallel <= 0:
        raise err(
            "E_BAD_PLAN",
            "max_parallel_gradients must be a positive integer",
            path=f"{path}.max_parallel_gradients",
            value=parallel,
        )
    return Tenant(
        name=name,
        slo=slo,
        profile=profile,
        max_parallel_gradients=parallel,
    )


def _parse_services(
    raw_value: Any,
    *,
    default_parallel: int,
) -> tuple[Service, ...]:
    if isinstance(raw_value, dict):
        service_items = [
            {**value, "name": value.get("name", key)}
            for key, value in raw_value.items()
            if isinstance(value, dict)
        ]
    else:
        service_items = _require_list(
            raw_value,
            path="$.services",
            code="E_BAD_SERVICE",
        )
    if not service_items:
        raise err("E_BAD_SERVICE", "at least one target service is required")

    services: list[Service] = []
    service_names: set[str] = set()
    tenant_names: set[str] = set()
    for service_index, item in enumerate(service_items):
        path = f"$.services[{service_index}]"
        item = _require_dict(item, path=path, code="E_BAD_SERVICE")
        name = _pick(item, ("name", "service", "service_name"))
        if not isinstance(name, str) or not name.strip():
            raise err("E_BAD_SERVICE", "service name is required", path=f"{path}.name")
        name = name.strip()
        if name in service_names:
            raise err(
                "E_BAD_SERVICE",
                f"duplicate service name: {name}",
                path=f"{path}.name",
                value=name,
            )
        service_names.add(name)

        host = _pick(item, ("host", "listen_host", "address", "bind"))
        if not isinstance(host, str) or not host.strip():
            raise err("E_BAD_SERVICE", "listen address is required", path=f"{path}.host")
        host = host.strip()
        if host.lower() == "localhost":
            host = "127.0.0.1"
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            raise err(
                "E_BAD_SERVICE",
                "listen address must be a literal loopback IP address",
                path=f"{path}.host",
                value=host,
            )
        if not address.is_loopback:
            raise err(
                "E_BAD_SERVICE",
                "listen address must be loopback",
                path=f"{path}.host",
                value=host,
            )

        port = _pick(item, ("port", "listen_port", "listening_port"))
        if not _is_int(port) or not (1 <= port <= 65535):
            raise err(
                "E_BAD_SERVICE",
                "listen port must be in [1, 65535]",
                path=f"{path}.port",
                value=port,
            )
        expected_status = _pick(item, ("expected_status", "success_status"), 200)
        if not _is_int(expected_status) or not (100 <= expected_status <= 599):
            raise err(
                "E_BAD_SERVICE",
                "expected_status must be an HTTP status integer",
                path=f"{path}.expected_status",
                value=expected_status,
            )
        refused_port = item.get("refused_port")
        if refused_port is not None and (
            not _is_int(refused_port) or not (1 <= refused_port <= 65535)
        ):
            raise err(
                "E_BAD_SERVICE",
                "refused_port must be in [1, 65535]",
                path=f"{path}.refused_port",
                value=refused_port,
            )

        tenants_raw = _pick(item, ("tenants", "tenant_slos", "slo"))
        if isinstance(tenants_raw, dict):
            tenant_items = [
                {**value, "name": value.get("name", key)}
                for key, value in tenants_raw.items()
                if isinstance(value, dict)
            ]
        else:
            tenant_items = _require_list(
                tenants_raw,
                path=f"{path}.tenants",
                code="E_BAD_SERVICE",
            )
        if not tenant_items:
            raise err(
                "E_BAD_SERVICE",
                "service must declare at least one tenant",
                path=f"{path}.tenants",
            )

        tenants: list[Tenant] = []
        for tenant_index, tenant_raw in enumerate(tenant_items):
            tenant_path = f"{path}.tenants[{tenant_index}]"
            tenant = _normalize_tenant(
                tenant_raw,
                service_name=name,
                path=tenant_path,
                default_parallel=default_parallel,
            )
            if tenant.name in tenant_names:
                raise err(
                    "E_BAD_SERVICE",
                    f"duplicate tenant name: {tenant.name}",
                    path=f"{tenant_path}.name",
                    value=tenant.name,
                )
            tenant_names.add(tenant.name)
            tenants.append(tenant)

        services.append(
            Service(
                name=name,
                host=host,
                port=int(port),
                expected_status=int(expected_status),
                tenants=tuple(tenants),
                refused_port=refused_port,
            )
        )
    return tuple(services)


def _parse_clock(
    raw_value: Any,
    *,
    global_budget_override: Any,
) -> tuple[ClockSpec, int]:
    raw_clock = _require_dict(raw_value, path="$.logical_clock", code="E_BAD_CLOCK")
    budget_value = (
        global_budget_override
        if global_budget_override is not None
        else _pick(
            raw_clock,
            ("global_budget_ms", "global_time_budget_ms", "budget_ms"),
        )
    )
    steps_raw = _pick(raw_clock, ("steps", "table", "segments"))
    if steps_raw is None:
        tick_ms = _pick(raw_clock, ("tick_ms", "step_ms"))
        if not _is_int(tick_ms) or tick_ms <= 0:
            raise err(
                "E_BAD_CLOCK",
                "logical_clock requires a positive tick_ms or a steps table",
                path="$.logical_clock.tick_ms",
                value=tick_ms,
            )
        if not _is_int(budget_value) or budget_value <= 0:
            raise err(
                "E_BAD_CLOCK",
                "global_time_budget_ms must be a positive integer",
                path="$.global_time_budget_ms",
                value=budget_value,
            )
        return (
            ClockSpec(
                segments=(ClockSegment(0, int(budget_value), int(tick_ms)),),
                start_ms=0,
            ),
            int(budget_value),
        )

    steps_raw = _require_list(
        steps_raw,
        path="$.logical_clock.steps",
        code="E_BAD_CLOCK",
    )
    if not steps_raw:
        raise err("E_BAD_CLOCK", "logical clock step table cannot be empty")
    start_ms = _pick(raw_clock, ("start_ms", "from_ms"), 0)
    if not _is_int(start_ms) or start_ms != 0:
        raise err(
            "E_BAD_CLOCK",
            "logical clock must start at logical millisecond 0",
            path="$.logical_clock.start_ms",
            value=start_ms,
        )

    segments: list[ClockSegment] = []
    previous_to = 0
    for index, step in enumerate(steps_raw):
        path = f"$.logical_clock.steps[{index}]"
        step = _require_dict(step, path=path, code="E_BAD_CLOCK")
        from_ms = step.get("from_ms", previous_to)
        step_ms = _pick(step, ("step_ms", "tick_ms"))
        to_ms = step.get("to_ms")
        duration_ms = _pick(step, ("duration_ms", "span_ms"))
        if not _is_int(from_ms) or from_ms != previous_to:
            raise err(
                "E_BAD_CLOCK",
                "clock segments must be contiguous and start at 0",
                path=f"{path}.from_ms",
                value=from_ms,
            )
        if not _is_int(step_ms) or step_ms <= 0:
            raise err(
                "E_BAD_CLOCK",
                "step_ms must be a positive integer",
                path=f"{path}.step_ms",
                value=step_ms,
            )
        if to_ms is None:
            if duration_ms is not None:
                if not _is_int(duration_ms) or duration_ms <= 0:
                    raise err(
                        "E_BAD_CLOCK",
                        "duration_ms must be a positive integer",
                        path=f"{path}.duration_ms",
                        value=duration_ms,
                    )
                to_ms = from_ms + int(duration_ms)
            elif index == len(steps_raw) - 1 and _is_int(budget_value):
                to_ms = int(budget_value)
            else:
                raise err(
                    "E_BAD_CLOCK",
                    "clock segment requires to_ms or duration_ms",
                    path=path,
                )
        if not _is_int(to_ms) or to_ms <= from_ms:
            raise err(
                "E_BAD_CLOCK",
                "to_ms must be greater than from_ms",
                path=f"{path}.to_ms",
                value=to_ms,
            )
        segments.append(ClockSegment(from_ms, int(to_ms), int(step_ms)))
        previous_to = int(to_ms)

    if budget_value is None:
        budget_value = previous_to
    if not _is_int(budget_value) or budget_value <= 0:
        raise err(
            "E_BAD_CLOCK",
            "global_time_budget_ms must be a positive integer",
            path="$.global_time_budget_ms",
            value=budget_value,
        )
    if previous_to < int(budget_value):
        raise err(
            "E_BAD_CLOCK",
            "logical clock step table does not cover the global time budget",
            path="$.logical_clock.steps",
            value={"covered_to_ms": previous_to, "budget_ms": budget_value},
        )
    return ClockSpec(segments=tuple(segments), start_ms=0), int(budget_value)


def _per_gradient_value(
    value: Any,
    *,
    count: int,
    default: Any,
    path: str,
    code: str = "E_BAD_PLAN",
) -> list[Any]:
    if value is None:
        value = default
    if isinstance(value, list):
        if len(value) != count:
            raise err(
                code,
                f"expected one value per gradient ({count})",
                path=path,
                value=value,
            )
        return value
    return [value] * count


def _parse_plans(
    raw_value: Any,
    *,
    tenant_map: dict[str, Tenant],
    clock: ClockSpec,
    default_window_ms: Any,
) -> tuple[BatchPlan, ...]:
    plans_raw = _require_list(raw_value, path="$.plans", code="E_BAD_PLAN")
    if not plans_raw:
        raise err("E_BAD_PLAN", "at least one batch plan is required", path="$.plans")
    plans: list[BatchPlan] = []
    batch_ids: set[str] = set()
    plan_tenants: set[str] = set()
    for plan_index, raw in enumerate(plans_raw):
        path = f"$.plans[{plan_index}]"
        raw = _require_dict(raw, path=path, code="E_BAD_PLAN")
        batch_id = _pick(raw, ("batch_id", "batch", "id"))
        if not isinstance(batch_id, str) or not batch_id.strip():
            raise err("E_BAD_PLAN", "batch_id is required", path=f"{path}.batch_id")
        batch_id = batch_id.strip()
        if batch_id in batch_ids:
            raise err(
                "E_BAD_PLAN",
                f"duplicate batch_id: {batch_id}",
                path=f"{path}.batch_id",
                value=batch_id,
            )
        batch_ids.add(batch_id)

        tenant_name = _pick(raw, ("tenant", "tenant_id", "tenant_name"))
        if not isinstance(tenant_name, str) or tenant_name not in tenant_map:
            raise err(
                "E_BAD_PLAN",
                "batch plan references an unknown tenant",
                path=f"{path}.tenant",
                value=tenant_name,
            )
        if tenant_name in plan_tenants:
            raise err(
                "E_BAD_PLAN",
                "one capacity ladder per tenant is supported in a run",
                path=f"{path}.tenant",
                value=tenant_name,
            )
        plan_tenants.add(tenant_name)

        ladder = _pick(
            raw,
            (
                "concurrency_gradients",
                "concurrency_ladder",
                "gradients",
                "concurrency",
            ),
        )
        ladder = _require_list(
            ladder,
            path=f"{path}.concurrency_gradients",
            code="E_BAD_PLAN",
        )
        if not ladder:
            raise err(
                "E_BAD_PLAN",
                "concurrency ladder cannot be empty",
                path=f"{path}.concurrency_gradients",
            )
        concurrency_values: list[int] = []
        previous = 0
        for gradient_index, value in enumerate(ladder, start=1):
            if not _is_int(value) or value <= 0:
                raise err(
                    "E_BAD_PLAN",
                    "concurrency gradient must be a positive integer",
                    path=f"{path}.concurrency_gradients[{gradient_index - 1}]",
                    value=value,
                )
            if value <= previous:
                raise err(
                    "E_BAD_PLAN",
                    "concurrency ladder must be strictly increasing",
                    path=f"{path}.concurrency_gradients[{gradient_index - 1}]",
                    value=value,
                )
            previous = value
            concurrency_values.append(value)

        count = len(concurrency_values)
        durations = _per_gradient_value(
            _pick(raw, ("duration_ms", "gradient_duration_ms", "durations_ms")),
            count=count,
            default=3000,
            path=f"{path}.duration_ms",
        )
        paces = _per_gradient_value(
            _pick(raw, ("pace_ms", "request_pace_ms", "pacing_ms", "requests_pace_ms")),
            count=count,
            default=100,
            path=f"{path}.pace_ms",
        )
        windows = _per_gradient_value(
            _pick(raw, ("window_ms", "observation_window_ms", "windows_ms")),
            count=count,
            default=default_window_ms,
            path=f"{path}.window_ms",
        )
        timeouts = _per_gradient_value(
            _pick(raw, ("request_timeout_ms", "single_request_budget_ms")),
            count=count,
            default=250,
            path=f"{path}.request_timeout_ms",
        )
        allow_early = _pick(raw, ("allow_early_termination", "early_termination"), True)
        if not isinstance(allow_early, bool):
            raise err(
                "E_BAD_PLAN",
                "allow_early_termination must be a boolean",
                path=f"{path}.allow_early_termination",
                value=allow_early,
            )

        gradients: list[GradientSpec] = []
        logical_start = 0
        for offset, concurrency in enumerate(concurrency_values):
            gradient_path = f"{path}.concurrency_gradients[{offset}]"
            duration_ms = _positive_int(
                durations[offset], path=f"{path}.duration_ms[{offset}]"
            )
            pace_ms = _positive_int(paces[offset], path=f"{path}.pace_ms[{offset}]")
            window_ms = _positive_int(
                windows[offset], path=f"{path}.window_ms[{offset}]"
            )
            timeout_ms = _positive_int(
                timeouts[offset], path=f"{path}.request_timeout_ms[{offset}]"
            )
            if pace_ms > duration_ms:
                raise err(
                    "E_BAD_PLAN",
                    "pace_ms cannot exceed duration_ms",
                    path=f"{path}.pace_ms[{offset}]",
                    value=pace_ms,
                )
            if window_ms > duration_ms:
                raise err(
                    "E_BAD_PLAN",
                    "window_ms cannot exceed duration_ms",
                    path=f"{path}.window_ms[{offset}]",
                    value=window_ms,
                )
            try:
                clock.segment_at(logical_start + duration_ms - 1)
            except ValueError:
                raise err(
                    "E_BAD_CLOCK",
                    "gradient extends beyond the logical clock table",
                    path=gradient_path,
                )
            gradients.append(
                GradientSpec(
                    index=offset + 1,
                    concurrency=int(concurrency),
                    duration_ms=duration_ms,
                    pace_ms=pace_ms,
                    window_ms=window_ms,
                    request_timeout_ms=timeout_ms,
                    logical_start_ms=logical_start,
                )
            )
            logical_start += duration_ms
        plans.append(
            BatchPlan(
                batch_id=batch_id,
                tenant=tenant_name,
                gradients=tuple(gradients),
                allow_early_termination=allow_early,
            )
        )
    return tuple(plans)


def parse_scenario(raw: dict[str, Any], *, source_path: str | None = None) -> Scenario:
    raw = copy.deepcopy(_require_dict(raw, path="$", code="E_BAD_PLAN"))
    version = raw.get("version", 1)
    if not _is_int(version) or version != 1:
        raise err("E_BAD_PLAN", "unsupported scenario version", path="$.version", value=version)
    seed = raw.get("seed", 0)
    if not _is_int(seed):
        raise err("E_BAD_PLAN", "seed must be an integer", path="$.seed", value=seed)

    defaults = raw.get("defaults", {})
    if defaults is None:
        defaults = {}
    defaults = _require_dict(defaults, path="$.defaults", code="E_BAD_PLAN")
    default_parallel = defaults.get("max_parallel_gradients", 1)
    if not _is_int(default_parallel) or default_parallel <= 0:
        raise err(
            "E_BAD_PLAN",
            "defaults.max_parallel_gradients must be a positive integer",
            path="$.defaults.max_parallel_gradients",
            value=default_parallel,
        )

    services_raw = _pick(raw, ("services", "targets", "target_services"))
    if services_raw is None:
        raise err("E_BAD_SERVICE", "services is required", path="$.services")
    services = _parse_services(services_raw, default_parallel=int(default_parallel))

    if "logical_clock" not in raw:
        raise err("E_BAD_CLOCK", "logical_clock is required", path="$.logical_clock")
    clock, budget = _parse_clock(
        raw["logical_clock"],
        global_budget_override=raw.get("global_time_budget_ms"),
    )

    plans_raw = _pick(raw, ("plans", "batches", "batch_plans"))
    if plans_raw is None:
        raise err("E_BAD_PLAN", "plans is required", path="$.plans")
    default_window = defaults.get("window_ms", raw["logical_clock"].get("window_ms", 1000))
    plans = _parse_plans(
        plans_raw,
        tenant_map={tenant.name: tenant for service in services for tenant in service.tenants},
        clock=clock,
        default_window_ms=default_window,
    )

    scenario = Scenario(
        version=1,
        seed=int(seed),
        services=services,
        plans=plans,
        clock=clock,
        global_time_budget_ms=budget,
        raw=raw,
        source_path=source_path,
    )
    _validate_planned_windows(scenario)
    return scenario


def _validate_planned_windows(scenario: Scenario) -> None:
    from .workload import count_window_requests

    for plan in scenario.plans:
        for gradient in plan.gradients:
            for window_index in range(gradient.window_count):
                count = count_window_requests(gradient, window_index)
                if count <= 0:
                    raise err(
                        "E_BAD_PLAN",
                        "each observation window must contain at least one request",
                        path=(
                            f"$.plans[batch={plan.batch_id}]"
                            f".gradients[{gradient.index - 1}]"
                        ),
                        value={"window_index": window_index},
                    )


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_scenario(path: str | Path) -> Scenario:
    source = Path(path)
    try:
        text = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise err("E_SCENARIO_IO", f"cannot read scenario: {exc}", path=str(source))
    try:
        raw = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except (json.JSONDecodeError, ValueError) as exc:
        raise err("E_BAD_JSON", f"invalid scenario JSON: {exc}", path=str(source))
    return parse_scenario(raw, source_path=str(source))


def scenario_from_stored_json(raw: dict[str, Any]) -> Scenario:
    return parse_scenario(raw)

