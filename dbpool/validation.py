from __future__ import annotations

import hashlib
import ipaddress
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .errors import (
    E_DUPLICATE_APPLICATION,
    E_DUPLICATE_ENDPOINT,
    E_INVALID_APPLICATION,
    E_INVALID_CLOCK_STEP,
    E_INVALID_CONFIG,
    E_INVALID_ENDPOINT,
    E_INVALID_JSON,
    E_INVALID_LOOPBACK_ADDRESS,
    E_POOL_BOUNDARY,
    E_TENANT_POOL_LABEL,
    PoolError,
)
from .model import Application, Catalog, Endpoint, PoolConfig, Trace


def _is_int(value: Any) -> bool:
    return type(value) is int


def _required_int(
    value: Any,
    field_name: str,
    *,
    code: str,
    operation: str,
    positive: bool = False,
    nonnegative: bool = False,
) -> int:
    if not _is_int(value):
        raise PoolError(
            code,
            f"{field_name} must be an integer",
            operation=operation,
            field=field_name,
            value=value,
        )
    if positive and value <= 0:
        raise PoolError(
            code,
            f"{field_name} must be a positive integer",
            operation=operation,
            field=field_name,
            value=value,
        )
    if nonnegative and value < 0:
        raise PoolError(
            code,
            f"{field_name} must be a non-negative integer",
            operation=operation,
            field=field_name,
            value=value,
        )
    return value


def _required_text(
    value: Any,
    field_name: str,
    *,
    code: str,
    operation: str,
) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PoolError(
            code,
            f"{field_name} must be a non-empty string",
            operation=operation,
            field=field_name,
            value=value,
        )
    return value.strip()


def _read_json(path: str | Path, operation: str) -> tuple[dict[str, Any], str]:
    source = Path(path)
    try:
        raw = source.read_bytes()
    except OSError as exc:
        raise PoolError(
            E_INVALID_JSON,
            "unable to read JSON sample",
            operation=operation,
            path=str(source),
            reason=str(exc),
        ) from exc
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PoolError(
            E_INVALID_JSON,
            "sample is not valid UTF-8 JSON",
            operation=operation,
            path=str(source),
            reason=str(exc),
        ) from exc
    if not isinstance(parsed, dict):
        raise PoolError(
            E_INVALID_JSON,
            "top-level JSON value must be an object",
            operation=operation,
            path=str(source),
        )
    return parsed, hashlib.sha256(raw).hexdigest()


def parse_loopback_address(value: Any, *, endpoint_name: str) -> tuple[str, int, str]:
    if not isinstance(value, str) or not value.strip():
        raise PoolError(
            E_INVALID_LOOPBACK_ADDRESS,
            "address must be a loopback tcp address",
            operation="load",
            endpoint=endpoint_name,
            address=value,
        )
    text = value.strip()
    if "://" in text:
        parsed = urlsplit(text)
        if parsed.scheme != "tcp" or parsed.path not in ("", "/"):
            raise PoolError(
                E_INVALID_LOOPBACK_ADDRESS,
                "only tcp:// loopback addresses are accepted",
                operation="load",
                endpoint=endpoint_name,
                address=value,
            )
        host = parsed.hostname
        try:
            port = parsed.port
        except ValueError as exc:
            raise PoolError(
                E_INVALID_LOOPBACK_ADDRESS,
                "address port is invalid",
                operation="load",
                endpoint=endpoint_name,
                address=value,
            ) from exc
    else:
        if text.startswith("["):
            closing = text.find("]")
            if closing < 0 or not text[closing + 1 :].startswith(":"):
                raise PoolError(
                    E_INVALID_LOOPBACK_ADDRESS,
                    "IPv6 address must use [address]:port",
                    operation="load",
                    endpoint=endpoint_name,
                    address=value,
                )
            host = text[1:closing]
            port_text = text[closing + 2 :]
        else:
            if text.count(":") != 1:
                raise PoolError(
                    E_INVALID_LOOPBACK_ADDRESS,
                    "address must use host:port",
                    operation="load",
                    endpoint=endpoint_name,
                    address=value,
                )
            host, port_text = text.rsplit(":", 1)
        try:
            port = int(port_text)
        except ValueError as exc:
            raise PoolError(
                E_INVALID_LOOPBACK_ADDRESS,
                "address port is not an integer",
                operation="load",
                endpoint=endpoint_name,
                address=value,
            ) from exc
    if host is None or not (1 <= port <= 65535):
        raise PoolError(
            E_INVALID_LOOPBACK_ADDRESS,
            "address must contain a valid tcp port",
            operation="load",
            endpoint=endpoint_name,
            address=value,
        )
    if host == "localhost":
        normalized_host = "127.0.0.1"
    else:
        try:
            parsed_ip = ipaddress.ip_address(host)
        except ValueError as exc:
            raise PoolError(
                E_INVALID_LOOPBACK_ADDRESS,
                "only literal loopback IPs or localhost are accepted",
                operation="load",
                endpoint=endpoint_name,
                address=value,
            ) from exc
        if not parsed_ip.is_loopback:
            raise PoolError(
                E_INVALID_LOOPBACK_ADDRESS,
                "address is not a loopback address",
                operation="load",
                endpoint=endpoint_name,
                address=value,
            )
        normalized_host = str(parsed_ip)
    display_host = f"[{normalized_host}]" if ":" in normalized_host else normalized_host
    normalized = f"tcp://{display_host}:{port}"
    return normalized_host, port, normalized


def parse_endpoints_object(data: dict[str, Any]) -> tuple[tuple[Endpoint, ...], str]:
    version = _required_int(
        data.get("version", 1),
        "version",
        code=E_INVALID_ENDPOINT,
        operation="load",
        positive=True,
    )
    raw_endpoints = data.get("endpoints")
    if not isinstance(raw_endpoints, list) or not raw_endpoints:
        raise PoolError(
            E_INVALID_ENDPOINT,
            "endpoints must be a non-empty array",
            operation="load",
            field="endpoints",
        )
    endpoints: list[Endpoint] = []
    names: set[str] = set()
    for index, item in enumerate(raw_endpoints):
        if not isinstance(item, dict):
            raise PoolError(
                E_INVALID_ENDPOINT,
                "endpoint entry must be an object",
                operation="load",
                index=index,
            )
        name = _required_text(
            item.get("name"),
            "name",
            code=E_INVALID_ENDPOINT,
            operation="load",
        )
        if name in names:
            raise PoolError(
                E_DUPLICATE_ENDPOINT,
                "endpoint name must be unique",
                operation="load",
                endpoint=name,
                index=index,
            )
        names.add(name)
        host, port, normalized_address = parse_loopback_address(
            item.get("address"),
            endpoint_name=name,
        )
        max_connections = _required_int(
            item.get("max_connections"),
            "max_connections",
            code=E_INVALID_ENDPOINT,
            operation="load",
            positive=True,
        )
        build_latency_ms = _required_int(
            item.get("build_latency_ms"),
            "build_latency_ms",
            code=E_INVALID_ENDPOINT,
            operation="load",
            nonnegative=True,
        )
        healthy = item.get("healthy")
        if not isinstance(healthy, bool):
            raise PoolError(
                E_INVALID_ENDPOINT,
                "healthy must be a boolean",
                operation="load",
                endpoint=name,
                field="healthy",
                value=healthy,
            )
        raw_labels = item.get("pool_labels", item.get("pool_label", ["default"]))
        if isinstance(raw_labels, str):
            raw_labels = [raw_labels]
        if not isinstance(raw_labels, list) or not raw_labels:
            raise PoolError(
                E_INVALID_ENDPOINT,
                "pool_labels must be a non-empty array",
                operation="load",
                endpoint=name,
                field="pool_labels",
            )
        labels: list[str] = []
        for raw_label in raw_labels:
            label = _required_text(
                raw_label,
                "pool_label",
                code=E_INVALID_ENDPOINT,
                operation="load",
            )
            if label not in labels:
                labels.append(label)
        endpoints.append(
            Endpoint(
                name=name,
                address=normalized_address,
                host=host,
                port=port,
                max_connections=max_connections,
                build_latency_ms=build_latency_ms,
                healthy=healthy,
                pool_labels=tuple(sorted(labels)),
            )
        )
    canonical = {
        "version": version,
        "endpoints": [endpoint.canonical() for endpoint in endpoints],
    }
    digest = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return tuple(endpoints), digest


def parse_config_object(data: dict[str, Any]) -> tuple[PoolConfig, str]:
    version = _required_int(
        data.get("version"),
        "version",
        code=E_INVALID_CONFIG,
        operation="load",
        positive=True,
    )
    max_total = _required_int(
        data.get("max_total_connections"),
        "max_total_connections",
        code=E_INVALID_CONFIG,
        operation="load",
        positive=True,
    )
    quota = _required_int(
        data.get("per_tenant_quota"),
        "per_tenant_quota",
        code=E_INVALID_CONFIG,
        operation="load",
        positive=True,
    )
    idle_timeout = _required_int(
        data.get("idle_timeout_ms"),
        "idle_timeout_ms",
        code=E_INVALID_CONFIG,
        operation="load",
        positive=True,
    )
    min_idle = _required_int(
        data.get("min_idle"),
        "min_idle",
        code=E_INVALID_CONFIG,
        operation="load",
        nonnegative=True,
    )
    round_budget = _required_int(
        data.get("round_budget_ms"),
        "round_budget_ms",
        code=E_INVALID_CONFIG,
        operation="load",
        positive=True,
    )
    clock_step = _required_int(
        data.get("clock_step_ms"),
        "clock_step_ms",
        code=E_INVALID_CLOCK_STEP,
        operation="load",
        positive=True,
    )
    if idle_timeout < clock_step:
        raise PoolError(
            E_POOL_BOUNDARY,
            "idle_timeout_ms must be at least clock_step_ms",
            operation="load",
            idle_timeout_ms=idle_timeout,
            clock_step_ms=clock_step,
        )
    if min_idle > max_total:
        raise PoolError(
            E_POOL_BOUNDARY,
            "min_idle must not exceed max_total_connections",
            operation="load",
            min_idle=min_idle,
            max_total_connections=max_total,
        )

    tolerance = data.get("tolerance", {})
    if not isinstance(tolerance, dict):
        raise PoolError(
            E_INVALID_CONFIG,
            "tolerance must be an object",
            operation="load",
            field="tolerance",
        )
    bucket_width = tolerance.get("bucket_width_ms", data.get("bucket_width_ms", clock_step))
    switch_margin = tolerance.get(
        "switch_margin_ratio",
        data.get("switch_margin_ratio", 0.15),
    )
    tiered_min_idle = tolerance.get(
        "tiered_min_idle",
        data.get("tiered_min_idle", max(1, min_idle)),
    )
    idle_age_tolerance = tolerance.get(
        "idle_age_ms",
        data.get("idle_age_tolerance_ms", 0),
    )
    bucket_width = _required_int(
        bucket_width,
        "tolerance.bucket_width_ms",
        code=E_INVALID_CONFIG,
        operation="load",
        positive=True,
    )
    tiered_min_idle = _required_int(
        tiered_min_idle,
        "tolerance.tiered_min_idle",
        code=E_INVALID_CONFIG,
        operation="load",
        positive=True,
    )
    idle_age_tolerance = _required_int(
        idle_age_tolerance,
        "tolerance.idle_age_ms",
        code=E_INVALID_CONFIG,
        operation="load",
        nonnegative=True,
    )
    if not isinstance(switch_margin, (int, float)) or isinstance(switch_margin, bool):
        raise PoolError(
            E_INVALID_CONFIG,
            "tolerance.switch_margin_ratio must be a number",
            operation="load",
            field="tolerance.switch_margin_ratio",
            value=switch_margin,
        )
    switch_margin = float(switch_margin)
    if not (0.0 <= switch_margin < 1.0):
        raise PoolError(
            E_INVALID_CONFIG,
            "tolerance.switch_margin_ratio must be in [0, 1)",
            operation="load",
            field="tolerance.switch_margin_ratio",
            value=switch_margin,
        )
    if tiered_min_idle > max_total + 1:
        raise PoolError(
            E_POOL_BOUNDARY,
            "tiered_min_idle must not exceed max_total_connections + 1",
            operation="load",
            tiered_min_idle=tiered_min_idle,
            max_total_connections=max_total,
        )

    policy = data.get("policy", {})
    if not isinstance(policy, dict):
        raise PoolError(
            E_INVALID_CONFIG,
            "policy must be an object",
            operation="load",
            field="policy",
        )
    mode = policy.get("mode", data.get("mode", "auto"))
    if mode not in {"auto", "exact", "tiered"}:
        raise PoolError(
            E_INVALID_CONFIG,
            "policy.mode must be auto, exact, or tiered",
            operation="load",
            field="policy.mode",
            value=mode,
        )

    raw_mapping = data.get("tenant_pool_labels", {})
    if not isinstance(raw_mapping, dict):
        raise PoolError(
            E_INVALID_CONFIG,
            "tenant_pool_labels must be an object",
            operation="load",
            field="tenant_pool_labels",
        )
    mapping: dict[str, tuple[str, ...]] = {}
    for raw_tenant, raw_labels in raw_mapping.items():
        tenant = _required_text(
            raw_tenant,
            "tenant",
            code=E_INVALID_CONFIG,
            operation="load",
        )
        if not isinstance(raw_labels, list) or not raw_labels:
            raise PoolError(
                E_INVALID_CONFIG,
                "tenant pool labels must be a non-empty array",
                operation="load",
                tenant=tenant,
            )
        labels = sorted(
            {
                _required_text(
                    label,
                    "tenant pool label",
                    code=E_INVALID_CONFIG,
                    operation="load",
                )
                for label in raw_labels
            }
        )
        mapping[tenant] = tuple(labels)

    config = PoolConfig(
        version=version,
        max_total_connections=max_total,
        per_tenant_quota=quota,
        idle_timeout_ms=idle_timeout,
        min_idle=min_idle,
        round_budget_ms=round_budget,
        clock_step_ms=clock_step,
        bucket_width_ms=bucket_width,
        switch_margin_ratio=round(switch_margin, 6),
        tiered_min_idle=tiered_min_idle,
        idle_age_tolerance_ms=idle_age_tolerance,
        mode=mode,
        tenant_pool_labels=mapping,
    )
    digest = hashlib.sha256(
        json.dumps(config.canonical(), sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    return config, digest


def parse_trace_object(data: dict[str, Any]) -> tuple[Trace, str]:
    version = _required_int(
        data.get("version", 1),
        "version",
        code=E_INVALID_APPLICATION,
        operation="load",
        positive=True,
    )
    raw_apps = data.get("applications")
    if not isinstance(raw_apps, list):
        raise PoolError(
            E_INVALID_APPLICATION,
            "applications must be an array",
            operation="load",
            field="applications",
        )
    applications: list[Application] = []
    keys: set[tuple[int, int]] = set()
    for index, item in enumerate(raw_apps):
        if not isinstance(item, dict):
            raise PoolError(
                E_INVALID_APPLICATION,
                "application entry must be an object",
                operation="load",
                index=index,
            )
        seq = _required_int(
            item.get("seq"),
            "seq",
            code=E_INVALID_APPLICATION,
            operation="load",
            positive=True,
        )
        app_version = _required_int(
            item.get("version", 1),
            "version",
            code=E_INVALID_APPLICATION,
            operation="load",
            positive=True,
        )
        key = (seq, app_version)
        if key in keys:
            raise PoolError(
                E_DUPLICATE_APPLICATION,
                "application seq/version must be unique",
                operation="load",
                seq=seq,
                version=app_version,
                index=index,
            )
        keys.add(key)
        tenant = _required_text(
            item.get("tenant"),
            "tenant",
            code=E_INVALID_APPLICATION,
            operation="load",
        )
        pool_label = _required_text(
            item.get("pool_label"),
            "pool_label",
            code=E_INVALID_APPLICATION,
            operation="load",
        )
        apply_clock_ms = _required_int(
            item.get("apply_clock_ms"),
            "apply_clock_ms",
            code=E_INVALID_APPLICATION,
            operation="load",
            nonnegative=True,
        )
        hold_ms = _required_int(
            item.get("hold_ms"),
            "hold_ms",
            code=E_INVALID_APPLICATION,
            operation="load",
            nonnegative=True,
        )
        release = item.get("release")
        if not isinstance(release, bool):
            raise PoolError(
                E_INVALID_APPLICATION,
                "release must be a boolean",
                operation="load",
                seq=seq,
                version=app_version,
                field="release",
                value=release,
            )
        applications.append(
            Application(
                seq=seq,
                version=app_version,
                tenant=tenant,
                pool_label=pool_label,
                apply_clock_ms=apply_clock_ms,
                hold_ms=hold_ms,
                release=release,
            )
        )
    final_clock = data.get("final_clock_ms")
    if final_clock is not None:
        final_clock = _required_int(
            final_clock,
            "final_clock_ms",
            code=E_INVALID_APPLICATION,
            operation="load",
            nonnegative=True,
        )
    trace = Trace(
        version=version,
        applications=tuple(sorted(applications, key=lambda app: (app.seq, app.version))),
        final_clock_ms=final_clock,
    )
    digest = hashlib.sha256(
        json.dumps(trace.canonical(), sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    return trace, digest


def validate_catalog(
    config: PoolConfig,
    endpoints: tuple[Endpoint, ...],
    trace: Trace,
) -> dict[str, tuple[str, ...]]:
    endpoint_labels = {
        label for endpoint in endpoints for label in endpoint.pool_labels
    }
    mapping: dict[str, set[str]] = {
        tenant: set(labels) for tenant, labels in config.tenant_pool_labels.items()
    }
    for app in trace.applications:
        mapping.setdefault(app.tenant, set()).add(app.pool_label)
    for tenant, labels in sorted(mapping.items()):
        configured = config.tenant_pool_labels.get(tenant)
        if configured is not None:
            for label in sorted(labels):
                if label not in configured:
                    raise PoolError(
                        E_TENANT_POOL_LABEL,
                        "application pool label is not allowed for tenant",
                        operation="load",
                        tenant=tenant,
                        pool_label=label,
                        allowed=list(configured),
                    )
        for label in sorted(labels):
            if label not in endpoint_labels:
                raise PoolError(
                    E_UNKNOWN_POOL_LABEL,
                    "no endpoint serves the requested pool label",
                    operation="load",
                    tenant=tenant,
                    pool_label=label,
                    endpoint_labels=sorted(endpoint_labels),
                )
    healthy_labels = {
        label
        for endpoint in endpoints
        if endpoint.healthy
        for label in endpoint.pool_labels
    }
    if not healthy_labels:
        raise PoolError(
            E_ENDPOINT_UNHEALTHY,
            "no healthy endpoint is available",
            operation="load",
        )
    return {
        tenant: tuple(sorted(labels)) for tenant, labels in sorted(mapping.items())
    }


def combined_digest(
    config_digest: str,
    endpoint_digest: str,
    trace_digest: str,
) -> str:
    return hashlib.sha256(
        f"{config_digest}:{endpoint_digest}:{trace_digest}".encode("ascii")
    ).hexdigest()


def load_catalog(
    endpoints_path: str | Path,
    config_path: str | Path,
    trace_path: str | Path,
) -> Catalog:
    endpoint_data, _ = _read_json(endpoints_path, "load")
    config_data, _ = _read_json(config_path, "load")
    trace_data, _ = _read_json(trace_path, "load")
    endpoints, endpoint_digest = parse_endpoints_object(endpoint_data)
    config, config_digest = parse_config_object(config_data)
    trace, trace_digest = parse_trace_object(trace_data)
    mapping = validate_catalog(config, endpoints, trace)
    sample_digest = combined_digest(config_digest, endpoint_digest, trace_digest)
    return Catalog(
        config=config,
        endpoints=endpoints,
        trace=trace,
        tenant_pool_labels=mapping,
        endpoint_digest=endpoint_digest,
        config_digest=config_digest,
        trace_digest=trace_digest,
        sample_digest=sample_digest,
    )
