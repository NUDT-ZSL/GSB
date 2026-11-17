"""Parallel-able, integer-only replica demand estimation."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any, Iterable, Mapping

from .errors import DemandError
from .models import DemandEstimate, ServiceSpec

try:
    import numpy as _np
except ImportError:  # pragma: no cover
    _np = None


NUMPY_AVAILABLE = _np is not None


def _ceil_div(numerator: int, denominator: int) -> int:
    if denominator <= 0:
        raise ZeroDivisionError("denominator must be positive")
    if _np is not None and numerator <= 2**53 and denominator <= 2**53:
        return int(_np.ceil(_np.float64(numerator) / _np.float64(denominator)))
    return (numerator + denominator - 1) // denominator


def _missing_error(path: str, field: str, service: str | None = None) -> DemandError:
    details: dict[str, Any] = {"missing_inputs": [field]}
    if service is not None:
        details["service"] = service
    return DemandError("required demand input is missing or invalid", path, details)


def _read_mapping(value: Mapping[str, Any], key: str, path: str, service: str) -> Any:
    if key not in value:
        raise _missing_error(path, key, service)
    return value[key]


def _as_service_fields(
    service: ServiceSpec | Mapping[str, Any],
    minute: int,
) -> tuple[str, int, int, int, int, int, float]:
    if type(minute) is not int or minute < 0:
        raise DemandError(
            "minute must be a non-negative integer",
            "$.minute",
            {"missing_inputs": ["minute"], "actual": minute},
        )

    if isinstance(service, ServiceSpec):
        name = service.name
        requests = service.requests
        concurrency = service.concurrency_per_replica
        min_replicas = service.min_replicas
        max_replicas = service.max_replicas
        threshold = service.scale_threshold
    elif isinstance(service, Mapping):
        raw_name = service.get("name")
        name = raw_name if isinstance(raw_name, str) and raw_name else "<unknown>"
        base = f'$.services[{name}]'
        if "name" not in service:
            raise _missing_error(f"{base}.name", "name", name)
        if "requests" not in service:
            raise _missing_error(f"{base}.requests", "requests", name)
        requests_value = service["requests"]
        if not isinstance(requests_value, (list, tuple)):
            raise DemandError(
                "requests must be an array",
                f"{base}.requests",
                {"missing_inputs": ["requests"], "service": name},
            )
        requests = tuple(requests_value)
        concurrency = _read_mapping(
            service,
            "concurrency_per_replica",
            f"{base}.concurrency_per_replica",
            name,
        )
        min_replicas = _read_mapping(
            service,
            "min_replicas",
            f"{base}.min_replicas",
            name,
        )
        max_replicas = _read_mapping(
            service,
            "max_replicas",
            f"{base}.max_replicas",
            name,
        )
        threshold = _read_mapping(
            service,
            "scale_threshold",
            f"{base}.scale_threshold",
            name,
        )
    else:
        raise DemandError(
            "service declaration is missing or has an unsupported type",
            "$.services",
            {"missing_inputs": ["service"]},
        )

    base = f'$.services[{name}]'
    if type(concurrency) is not int or concurrency <= 0:
        raise _missing_error(f"{base}.concurrency_per_replica", "concurrency_per_replica", name)
    if type(min_replicas) is not int or min_replicas < 0:
        raise _missing_error(f"{base}.min_replicas", "min_replicas", name)
    if type(max_replicas) is not int or max_replicas <= 0:
        raise _missing_error(f"{base}.max_replicas", "max_replicas", name)
    if min_replicas > max_replicas:
        raise DemandError(
            "min_replicas must not exceed max_replicas",
            f"{base}.min_replicas",
            {"service": name, "missing_inputs": [], "min_replicas": min_replicas, "max_replicas": max_replicas},
        )
    if type(threshold) not in (int, float) or not (0 < float(threshold) <= 1):
        raise _missing_error(f"{base}.scale_threshold", "scale_threshold", name)

    if minute >= len(requests):
        path = f"{base}.requests[{minute}]"
        raise DemandError(
            "load for the requested minute is missing",
            path,
            {
                "service": name,
                "minute": minute,
                "missing_inputs": [f"requests[{minute}]"],
                "available_minutes": len(requests),
            },
        )
    request_count = requests[minute]
    if type(request_count) is not int or request_count < 0:
        raise DemandError(
            "minute request count must be a non-negative integer",
            f"{base}.requests[{minute}]",
            {
                "service": name,
                "minute": minute,
                "missing_inputs": [f"requests[{minute}]"],
                "actual": request_count,
            },
        )
    return (
        name,
        int(request_count),
        int(concurrency),
        int(min_replicas),
        int(max_replicas),
        float(threshold),
        minute,
    )


def estimate_demand(
    service: ServiceSpec | Mapping[str, Any],
    minute: int,
) -> DemandEstimate:
    """Estimate one service's demand with a stable integer ceiling rule."""

    (
        name,
        request_count,
        concurrency,
        min_replicas,
        max_replicas,
        threshold,
        minute,
    ) = _as_service_fields(service, minute)

    unclamped = _ceil_div(request_count, concurrency) if request_count else 0
    demand = min(max(unclamped, min_replicas), max_replicas)
    return DemandEstimate(
        service=name,
        minute=minute,
        request_count=request_count,
        capacity_per_replica=concurrency,
        unclamped_demand=unclamped,
        demand_replicas=demand,
        min_replicas=min_replicas,
        max_replicas=max_replicas,
        scale_threshold=threshold,
    )


def estimate_minute_parallel(
    services: Iterable[ServiceSpec],
    minute: int,
    max_workers: int | None = None,
) -> tuple[DemandEstimate, ...]:
    """Compute a minute in parallel and return a name-sorted stable snapshot."""

    ordered = tuple(sorted(services, key=lambda service: service.name))
    if not ordered:
        return ()
    worker_count = max_workers or min(32, len(ordered))
    with ThreadPoolExecutor(
        max_workers=max(1, worker_count),
        thread_name_prefix="demand-estimator",
    ) as executor:
        futures = {
            service.name: executor.submit(estimate_demand, service, minute)
            for service in ordered
        }
        results: list[DemandEstimate] = []
        for service in ordered:
            results.append(futures[service.name].result())
    return tuple(results)
