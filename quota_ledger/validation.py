"""Validation and normalization for quota, limiter, and event documents."""

from __future__ import annotations

from typing import Any

from .errors import (
    E_BAD_EVENT,
    E_BAD_HIERARCHY,
    E_BAD_LIMITER,
    E_BAD_QUOTA,
    LedgerError,
)
from .models import Event, LimiterConfig, QuotaConfig, QuotaLayer, TenantRateConfig
from .utils import (
    event_error,
    fraction_text,
    parse_positive_rate,
    require_bool,
    require_int,
    require_text,
)


def _as_mapping(value: Any, where: str, code: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise LedgerError(code, f"{where} must be an object")
    return value


def _as_list(value: Any, where: str, code: str) -> list[Any]:
    if not isinstance(value, list):
        raise LedgerError(code, f"{where} must be an array")
    return value


def _optional_name(
    item: dict[str, Any],
    primary: str,
    aliases: tuple[str, ...],
    where: str,
    code: str,
) -> str:
    value = item.get(primary)
    if value is None:
        for alias in aliases:
            if alias in item:
                value = item[alias]
                break
    if value is None:
        raise LedgerError(code, f"{where}.{primary} is required")
    return require_text(value, f"{where}.{primary}", code)


def _quota_values(item: dict[str, Any], where: str) -> dict[str, int]:
    values: dict[str, int] = {}
    for field in ("max_bytes", "max_objects"):
        values[field] = require_int(
            item.get(field),
            f"{where}.{field}",
            E_BAD_QUOTA,
            minimum=1,
        )
    for field in ("soft_pct", "hard_pct"):
        values[field] = require_int(
            item.get(field),
            f"{where}.{field}",
            E_BAD_QUOTA,
            minimum=0,
        )
        if values[field] > 100:
            raise LedgerError(
                E_BAD_QUOTA,
                f"{where}.{field} must be between 0 and 100",
                {"field": f"{where}.{field}", "value": values[field]},
            )
    if values["soft_pct"] > values["hard_pct"]:
        raise LedgerError(
            E_BAD_QUOTA,
            f"{where}.soft_pct must not exceed hard_pct",
            {
                "soft_pct": values["soft_pct"],
                "hard_pct": values["hard_pct"],
            },
        )
    return values


def parse_quota_document(document: Any) -> QuotaConfig:
    root = _as_mapping(document, "quota document", E_BAD_HIERARCHY)
    version = root.get("version", 1)
    if version != 1:
        raise LedgerError(E_BAD_HIERARCHY, "unsupported quota document version")
    tenant_items = _as_list(root.get("tenants"), "quota.tenants", E_BAD_HIERARCHY)
    if not tenant_items:
        raise LedgerError(E_BAD_HIERARCHY, "quota.tenants must not be empty")

    layers: list[QuotaLayer] = []
    seen_tenants: set[str] = set()
    normalized_tenants: list[dict[str, Any]] = []

    for tenant_index, tenant_item_raw in enumerate(tenant_items):
        where_t = f"quota.tenants[{tenant_index}]"
        tenant_item = _as_mapping(tenant_item_raw, where_t, E_BAD_HIERARCHY)
        tenant = _optional_name(
            tenant_item,
            "name",
            ("tenant",),
            where_t,
            E_BAD_HIERARCHY,
        )
        if tenant in seen_tenants:
            raise LedgerError(
                E_BAD_HIERARCHY,
                f"duplicate tenant: {tenant}",
                {"tenant": tenant},
            )
        seen_tenants.add(tenant)
        tenant_quota = _quota_values(tenant_item, where_t)
        layers.append(QuotaLayer("tenant", tenant, "", "", **tenant_quota))

        bucket_items = _as_list(
            tenant_item.get("buckets"),
            f"{where_t}.buckets",
            E_BAD_HIERARCHY,
        )
        if not bucket_items:
            raise LedgerError(
                E_BAD_HIERARCHY,
                f"tenant {tenant} must declare at least one bucket",
            )
        seen_buckets: set[str] = set()
        normalized_buckets: list[dict[str, Any]] = []
        for bucket_index, bucket_item_raw in enumerate(bucket_items):
            where_b = f"{where_t}.buckets[{bucket_index}]"
            bucket_item = _as_mapping(bucket_item_raw, where_b, E_BAD_HIERARCHY)
            bucket = _optional_name(
                bucket_item,
                "name",
                ("bucket",),
                where_b,
                E_BAD_HIERARCHY,
            )
            if bucket in seen_buckets:
                raise LedgerError(
                    E_BAD_HIERARCHY,
                    f"duplicate bucket {bucket} in tenant {tenant}",
                    {"tenant": tenant, "bucket": bucket},
                )
            seen_buckets.add(bucket)
            bucket_quota = _quota_values(bucket_item, where_b)
            layers.append(QuotaLayer("bucket", tenant, bucket, "", **bucket_quota))

            prefix_items = _as_list(
                bucket_item.get("prefixes"),
                f"{where_b}.prefixes",
                E_BAD_HIERARCHY,
            )
            if not prefix_items:
                raise LedgerError(
                    E_BAD_HIERARCHY,
                    f"bucket {tenant}/{bucket} must declare at least one prefix",
                )
            seen_prefixes: set[str] = set()
            normalized_prefixes: list[dict[str, Any]] = []
            for prefix_index, prefix_item_raw in enumerate(prefix_items):
                where_p = f"{where_b}.prefixes[{prefix_index}]"
                prefix_item = _as_mapping(prefix_item_raw, where_p, E_BAD_HIERARCHY)
                prefix = _optional_name(
                    prefix_item,
                    "key",
                    ("prefix", "name"),
                    where_p,
                    E_BAD_HIERARCHY,
                )
                if prefix in seen_prefixes:
                    raise LedgerError(
                        E_BAD_HIERARCHY,
                        f"duplicate prefix {prefix} in bucket {tenant}/{bucket}",
                        {
                            "tenant": tenant,
                            "bucket": bucket,
                            "prefix": prefix,
                        },
                    )
                seen_prefixes.add(prefix)
                prefix_quota = _quota_values(prefix_item, where_p)
                layers.append(
                    QuotaLayer("prefix", tenant, bucket, prefix, **prefix_quota)
                )
                normalized_prefixes.append({"key": prefix, **prefix_quota})

            normalized_prefixes.sort(key=lambda item: item["key"])
            normalized_buckets.append(
                {
                    "name": bucket,
                    **bucket_quota,
                    "prefixes": normalized_prefixes,
                }
            )
        normalized_buckets.sort(key=lambda item: item["name"])
        normalized_tenants.append(
            {
                "name": tenant,
                **tenant_quota,
                "buckets": normalized_buckets,
            }
        )

    normalized_tenants.sort(key=lambda item: item["name"])
    normalized_document = {"version": 1, "tenants": normalized_tenants}
    return QuotaConfig(normalized_document, layers)


def parse_limiter_document(
    document: Any,
    quota: QuotaConfig,
) -> LimiterConfig:
    root = _as_mapping(document, "limiter document", E_BAD_LIMITER)
    version = root.get("version", 1)
    if version != 1:
        raise LedgerError(E_BAD_LIMITER, "unsupported limiter document version")
    tolerance = require_int(
        root.get("clock_rollback_tolerance_ms", 0),
        "limiter.clock_rollback_tolerance_ms",
        E_BAD_LIMITER,
        minimum=0,
    )

    raw_tenants = root.get("tenants")
    items: list[dict[str, Any]] = []
    if isinstance(raw_tenants, dict):
        for tenant, value in raw_tenants.items():
            if not isinstance(value, dict):
                raise LedgerError(
                    E_BAD_LIMITER,
                    f"limiter tenant {tenant} must be an object",
                )
            items.append({"tenant": tenant, **value})
    else:
        tenant_list = _as_list(raw_tenants, "limiter.tenants", E_BAD_LIMITER)
        for index, item in enumerate(tenant_list):
            items.append(_as_mapping(item, f"limiter.tenants[{index}]", E_BAD_LIMITER))

    policies: dict[str, TenantRateConfig] = {}
    normalized_tenants: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        where = f"limiter.tenants[{index}]"
        tenant = _optional_name(
            item,
            "tenant",
            ("name",),
            where,
            E_BAD_LIMITER,
        )
        if tenant in policies:
            raise LedgerError(E_BAD_LIMITER, f"duplicate limiter tenant: {tenant}")
        requests_per_second = require_int(
            item.get("requests_per_second"),
            f"{where}.requests_per_second",
            E_BAD_LIMITER,
            minimum=1,
        )
        bucket_capacity = require_int(
            item.get("bucket_capacity"),
            f"{where}.bucket_capacity",
            E_BAD_LIMITER,
            minimum=1,
        )
        burst_depth = require_int(
            item.get("burst_depth", 0),
            f"{where}.burst_depth",
            E_BAD_LIMITER,
            minimum=0,
        )
        backoff_window_ms = require_int(
            item.get("backoff_window_ms", 0),
            f"{where}.backoff_window_ms",
            E_BAD_LIMITER,
            minimum=0,
        )
        refill_rate = parse_positive_rate(
            item.get("refill_rate_per_second"),
            f"{where}.refill_rate_per_second",
        )
        policy = TenantRateConfig(
            tenant=tenant,
            requests_per_second=requests_per_second,
            bucket_capacity=bucket_capacity,
            refill_rate_per_second=refill_rate,
            burst_depth=burst_depth,
            backoff_window_ms=backoff_window_ms,
        )
        policies[tenant] = policy
        normalized_tenants.append(
            {
                "tenant": tenant,
                "requests_per_second": requests_per_second,
                "bucket_capacity": bucket_capacity,
                "refill_rate_per_second": fraction_text(refill_rate),
                "burst_depth": burst_depth,
                "backoff_window_ms": backoff_window_ms,
            }
        )

    expected = set(quota.tenants)
    actual = set(policies)
    if expected != actual:
        raise LedgerError(
            E_BAD_LIMITER,
            "limiter tenant set must match quota tenant set",
            {
                "missing": sorted(expected - actual),
                "unknown": sorted(actual - expected),
            },
        )
    normalized_document = {
        "version": 1,
        "clock_rollback_tolerance_ms": tolerance,
        "tenants": sorted(normalized_tenants, key=lambda item: item["tenant"]),
    }
    return LimiterConfig(normalized_document, policies, tolerance)


def parse_event(
    item: Any,
    quota: QuotaConfig | None = None,
) -> Event:
    event_item = _as_mapping(item, "event", E_BAD_EVENT)

    def get(primary: str, aliases: tuple[str, ...] = ()) -> Any:
        if primary in event_item:
            return event_item[primary]
        for alias in aliases:
            if alias in event_item:
                return event_item[alias]
        return None

    seq = require_int(get("seq", ("event_seq", "sequence")), "event.seq", E_BAD_EVENT, minimum=1)
    tenant = require_text(get("tenant"), "event.tenant", E_BAD_EVENT)
    bucket = require_text(get("bucket"), "event.bucket", E_BAD_EVENT)
    prefix = require_text(get("prefix"), "event.prefix", E_BAD_EVENT)
    object_key = require_text(
        get("object_key", ("key",)),
        "event.object_key",
        E_BAD_EVENT,
    )
    size_bytes = require_int(get("bytes", ("size_bytes",)), "event.bytes", E_BAD_EVENT, minimum=0)
    deleted = require_bool(get("deleted", ("delete",)), "event.deleted", E_BAD_EVENT)
    clock_ms = require_int(
        get("clock_ms", ("logical_clock_ms", "clock")),
        "event.clock_ms",
        E_BAD_EVENT,
        minimum=0,
    )
    idempotency_key = require_text(
        get("idempotency_key", ("idem",)),
        "event.idempotency_key",
        E_BAD_EVENT,
    )

    if quota is not None and not quota.has_path(tenant, bucket, prefix):
        raise LedgerError(
            E_BAD_HIERARCHY,
            "event hierarchy is not declared",
            {"tenant": tenant, "bucket": bucket, "prefix": prefix},
        )

    return Event(
        seq=seq,
        tenant=tenant,
        bucket=bucket,
        prefix=prefix,
        object_key=object_key,
        bytes=size_bytes,
        deleted=deleted,
        clock_ms=clock_ms,
        idempotency_key=idempotency_key,
    )


def parse_event_document(
    document: Any,
    quota: QuotaConfig | None = None,
) -> list[Event]:
    if isinstance(document, list):
        raw_events = document
    elif isinstance(document, dict) and "events" in document:
        raw_events = _as_list(document.get("events"), "events", E_BAD_EVENT)
    else:
        raw_events = [document]

    events = [parse_event(item, quota) for item in raw_events]
    validate_event_order(events)
    return events


def validate_event_order(events: list[Event]) -> None:
    """Validate the ordering and idempotency uniqueness of parsed events."""

    previous_seq = 0
    seen_idempotency: set[str] = set()
    for event in events:
        if event.seq <= previous_seq:
            raise event_error(
                "event sequence numbers must be strictly increasing",
                previous_seq=previous_seq,
                seq=event.seq,
            )
        previous_seq = event.seq
        if event.idempotency_key in seen_idempotency:
            raise event_error(
                "duplicate idempotency key in one ordered event stream",
                idempotency_key=event.idempotency_key,
            )
        seen_idempotency.add(event.idempotency_key)

