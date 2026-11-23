from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .errors import EngineError, fail
from .models import (
    Configuration,
    EventSpec,
    PartitionConfig,
    QuotaConfig,
    StreamSpec,
    TenantQuotaSpec,
    TokenConfig,
)

_TENANT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_METRIC_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")
_TAG_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")
_TAG_VALUE_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,256}$")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def stable_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _read_json(path: str | Path) -> Any:
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        fail("E_IO", f"configuration file not found: {path}", path=str(path))
    except json.JSONDecodeError as exc:
        fail("E_BAD_JSON", f"invalid JSON in {path}: {exc.msg}", path=str(path), line=exc.lineno)
    except OSError as exc:
        fail("E_IO", f"cannot read {path}: {exc}", path=str(path))


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _positive_int(value: Any, code: str, field: str, *, allow_zero: bool = False) -> int:
    if not _is_int(value) or value < (0 if allow_zero else 1):
        comparator = "non-negative" if allow_zero else "positive"
        fail(code, f"{field} must be a {comparator} integer", field=field, value=value)
    return value


def _text(value: Any, code: str, field: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        fail(code, f"{field} has an invalid format", field=field, value=value)
    return value


def _decimal(value: Any, code: str, field: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        fail(code, f"{field} must be a decimal number", field=field, value=value)
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        fail(code, f"{field} must be a decimal number", field=field, value=value)
    if not result.is_finite() or result < 0:
        fail(code, f"{field} must be finite and non-negative", field=field, value=value)
    return result


def _unwrap(document: Any, key: str) -> Any:
    if isinstance(document, dict) and key in document:
        return document[key]
    return document


def parse_streams(document: Any) -> tuple[StreamSpec, ...]:
    rows = _unwrap(document, "streams")
    if not isinstance(rows, list) or not rows:
        fail("E_BAD_STREAM", "streams must be a non-empty array")

    seen: set[tuple[str, str]] = set()
    streams: list[StreamSpec] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            fail("E_BAD_STREAM", "each stream must be an object", index=index)
        tenant = _text(row.get("tenant"), "E_BAD_STREAM", "tenant", _TENANT_RE)
        metric = _text(row.get("metric"), "E_BAD_STREAM", "metric", _METRIC_RE)
        key = (tenant, metric)
        if key in seen:
            fail(
                "E_BAD_STREAM",
                "tenant and metric pair must be unique",
                index=index,
                tenant=tenant,
                metric=metric,
            )
        seen.add(key)

        tags = row.get("tags")
        if not isinstance(tags, dict):
            fail("E_BAD_STREAM", "tags must be an object", index=index, tenant=tenant, metric=metric)
        normalized_tags: list[tuple[str, str]] = []
        for tag_key, tag_value in tags.items():
            if not isinstance(tag_key, str) or not _TAG_KEY_RE.fullmatch(tag_key):
                fail("E_BAD_TAG", "invalid tag key", index=index, key=tag_key)
            if not isinstance(tag_value, str) or not _TAG_VALUE_RE.fullmatch(tag_value):
                fail("E_BAD_TAG", "invalid tag value", index=index, key=tag_key, value=tag_value)
            normalized_tags.append((tag_key, tag_value))
        normalized_tags.sort()

        period = _positive_int(
            row.get("sample_period_ms"), "E_BAD_STREAM", "sample_period_ms"
        )
        sample_bytes = _positive_int(row.get("sample_bytes"), "E_BAD_STREAM", "sample_bytes")
        streams.append(
            StreamSpec(
                tenant=tenant,
                metric=metric,
                tags=tuple(normalized_tags),
                sample_period_ms=period,
                sample_bytes=sample_bytes,
                partition=-1,
                route_hash="",
            )
        )
    return tuple(streams)


def parse_partitions(document: Any) -> PartitionConfig:
    row = _unwrap(document, "partitions")
    if not isinstance(row, dict):
        fail("E_BAD_PARTITION_CONFIG", "partitions config must be an object")
    partition_count = _positive_int(
        row.get("partition_count"), "E_BAD_PARTITION_CONFIG", "partition_count"
    )
    buffer_limit = _positive_int(
        row.get("buffer_per_partition"), "E_BAD_PARTITION_CONFIG", "buffer_per_partition"
    )
    round_limit = _positive_int(
        row.get("flush_round_limit"), "E_BAD_PARTITION_CONFIG", "flush_round_limit"
    )
    budget = _positive_int(
        row.get("round_time_budget_ms"),
        "E_BAD_PARTITION_CONFIG",
        "round_time_budget_ms",
        allow_zero=True,
    )
    hash_seed = row.get("hash_seed", "metric-engine-v1")
    if not isinstance(hash_seed, str) or not hash_seed:
        fail(
            "E_BAD_PARTITION_CONFIG",
            "hash_seed must be a non-empty string",
            field="hash_seed",
        )
    token_cost = _positive_int(
        row.get("token_cost_per_sample", 1),
        "E_BAD_PARTITION_CONFIG",
        "token_cost_per_sample",
    )
    return PartitionConfig(
        partition_count=partition_count,
        buffer_per_partition=buffer_limit,
        flush_round_limit=round_limit,
        round_time_budget_ms=budget,
        hash_seed=hash_seed,
        token_cost_per_sample=token_cost,
    )


def parse_quotas(document: Any) -> QuotaConfig:
    row = _unwrap(document, "quotas")
    if not isinstance(row, dict):
        fail("E_BAD_QUOTA", "quota config must be an object")
    window_ms = _positive_int(row.get("window_ms"), "E_BAD_QUOTA", "window_ms")
    scale = _positive_int(row.get("fixed_scale", 1), "E_BAD_QUOTA", "fixed_scale")
    tolerance = _decimal(
        row.get("comparison_tolerance_bytes", "0"), "E_BAD_QUOTA", "comparison_tolerance_bytes"
    )
    tenant_rows = row.get("tenants")
    if not isinstance(tenant_rows, dict) or not tenant_rows:
        fail("E_BAD_QUOTA", "tenants must be a non-empty object")
    tenants: dict[str, TenantQuotaSpec] = {}
    for tenant, declaration in tenant_rows.items():
        _text(tenant, "E_BAD_QUOTA", "tenant", _TENANT_RE)
        if not isinstance(declaration, dict):
            fail("E_BAD_QUOTA", "tenant quota must be an object", tenant=tenant)
        limit = _decimal(
            declaration.get("window_limit_bytes"),
            "E_BAD_QUOTA",
            "window_limit_bytes",
        )
        borrow = _decimal(
            declaration.get("borrow_limit_bytes", "0"),
            "E_BAD_QUOTA",
            "borrow_limit_bytes",
        )
        allow_borrow = declaration.get("allow_borrow", False)
        if not isinstance(allow_borrow, bool):
            fail("E_BAD_QUOTA", "allow_borrow must be boolean", tenant=tenant)
        if limit == 0:
            fail("E_BAD_QUOTA", "window_limit_bytes must be positive", tenant=tenant)
        tenants[tenant] = TenantQuotaSpec(limit, borrow, allow_borrow)
    return QuotaConfig(
        window_ms=window_ms,
        fixed_scale=scale,
        comparison_tolerance_bytes=tolerance,
        tenants=tenants,
    )


def parse_tokens(document: Any) -> TokenConfig:
    row = _unwrap(document, "tokens")
    if not isinstance(row, dict):
        fail("E_BAD_TOKEN", "token config must be an object")
    capacity = _positive_int(row.get("capacity_tokens"), "E_BAD_TOKEN", "capacity_tokens")
    rate = _positive_int(
        row.get("refill_rate_per_second"), "E_BAD_TOKEN", "refill_rate_per_second"
    )
    step = _positive_int(row.get("clock_step_ms"), "E_BAD_TOKEN", "clock_step_ms")
    scale = _positive_int(row.get("fixed_scale", 1), "E_BAD_TOKEN", "fixed_scale")
    return TokenConfig(
        capacity_tokens=capacity,
        refill_rate_per_second=rate,
        clock_step_ms=step,
        fixed_scale=scale,
    )


def route_partition(
    tenant: str,
    metric: str,
    tags: tuple[tuple[str, str], ...],
    partition_count: int,
    hash_seed: str,
) -> tuple[int, str]:
    material = {
        "seed": hash_seed,
        "tenant": tenant,
        "metric": metric,
        "tags": [list(item) for item in tags],
    }
    digest = hashlib.sha256(canonical_json(material).encode("utf-8")).hexdigest()
    partition = int(digest[:16], 16) % partition_count
    return partition, digest


def parse_configuration(
    streams_document: Any,
    partitions_document: Any,
    quotas_document: Any,
    tokens_document: Any,
) -> Configuration:
    streams = parse_streams(streams_document)
    partitions = parse_partitions(partitions_document)
    quotas = parse_quotas(quotas_document)
    tokens = parse_tokens(tokens_document)

    for stream in streams:
        if stream.tenant not in quotas.tenants:
            fail(
                "E_BAD_QUOTA",
                "stream tenant has no quota declaration",
                tenant=stream.tenant,
                metric=stream.metric,
            )
    for tenant in quotas.tenants:
        if not any(stream.tenant == tenant for stream in streams):
            fail("E_BAD_QUOTA", "quota declared for unknown tenant", tenant=tenant)

    routed: list[StreamSpec] = []
    for stream in streams:
        partition, digest = route_partition(
            stream.tenant,
            stream.metric,
            stream.tags,
            partitions.partition_count,
            partitions.hash_seed,
        )
        routed.append(
            StreamSpec(
                tenant=stream.tenant,
                metric=stream.metric,
                tags=stream.tags,
                sample_period_ms=stream.sample_period_ms,
                sample_bytes=stream.sample_bytes,
                partition=partition,
                route_hash=digest,
            )
        )

    raw = {
        "streams": streams_document,
        "partitions": partitions_document,
        "quotas": quotas_document,
        "tokens": tokens_document,
    }
    return Configuration(
        streams=tuple(routed),
        partitions=partitions,
        quotas=quotas,
        tokens=tokens,
        config_hash=stable_hash(raw),
        raw=raw,
    )


def load_configuration(
    streams_path: str | Path,
    partitions_path: str | Path,
    quotas_path: str | Path,
    tokens_path: str | Path,
) -> Configuration:
    return parse_configuration(
        _read_json(streams_path),
        _read_json(partitions_path),
        _read_json(quotas_path),
        _read_json(tokens_path),
    )


def _normalize_event_tags(tags: Any, *, event_id: str | None = None) -> tuple[tuple[str, str], ...]:
    if not isinstance(tags, dict):
        fail("E_BAD_EVENT", "event tags must be an object", event_id=event_id)
    normalized: list[tuple[str, str]] = []
    for key, value in tags.items():
        if not isinstance(key, str) or not _TAG_KEY_RE.fullmatch(key):
            fail("E_BAD_TAG", "invalid tag key", event_id=event_id, key=key)
        if not isinstance(value, str) or not _TAG_VALUE_RE.fullmatch(value):
            fail("E_BAD_TAG", "invalid tag value", event_id=event_id, key=key, value=value)
        normalized.append((key, value))
    normalized.sort()
    return tuple(normalized)


def parse_events(document: Any, config: Configuration) -> tuple[list[EventSpec], list[dict[str, Any]]]:
    rows = _unwrap(document, "events")
    if not isinstance(rows, list):
        fail("E_BAD_EVENT", "events must be an array")
    stream_map = config.stream_map
    events: list[EventSpec] = []
    duplicates: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            fail("E_BAD_EVENT", "each event must be an object", index=index)
        tenant = row.get("tenant")
        metric = row.get("metric")
        if not isinstance(tenant, str) or not isinstance(metric, str):
            fail("E_BAD_EVENT", "event tenant and metric must be strings", index=index)
        stream = stream_map.get((tenant, metric))
        if stream is None:
            fail(
                "E_BAD_STREAM",
                "event references an unknown stream",
                index=index,
                tenant=tenant,
                metric=metric,
            )
        tags = _normalize_event_tags(row.get("tags", stream.tags_dict), event_id=row.get("event_id"))
        if tags != stream.tags:
            fail(
                "E_TAG_MISMATCH",
                "event tags do not match the declared stream",
                index=index,
                tenant=tenant,
                metric=metric,
            )
        logical_time = _positive_int(
            row.get("logical_time_ms"),
            "E_BAD_EVENT",
            "logical_time_ms",
            allow_zero=True,
        )
        sample_seq = _positive_int(
            row.get("sample_seq"),
            "E_BAD_EVENT",
            "sample_seq",
            allow_zero=True,
        )
        identity = {
            "tenant": tenant,
            "metric": metric,
            "tags": [list(item) for item in tags],
            "logical_time_ms": logical_time,
            "sample_seq": sample_seq,
            "sample_bytes": stream.sample_bytes,
        }
        event_id = row.get("event_id")
        if event_id is None:
            event_id = hashlib.sha256(canonical_json(identity).encode("utf-8")).hexdigest()
        elif not isinstance(event_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", event_id):
            fail("E_BAD_EVENT", "event_id has an invalid format", index=index)
        trace_id = row.get("trace_id")
        if trace_id is not None and not isinstance(trace_id, str):
            fail("E_BAD_EVENT", "trace_id must be a string", index=index)
        if event_id in seen_ids:
            duplicates.append({"index": index, "event_id": event_id, "kind": "batch_duplicate"})
            continue
        seen_ids.add(event_id)
        events.append(
            EventSpec(
                event_id=event_id,
                tenant=tenant,
                metric=metric,
                tags=tags,
                logical_time_ms=logical_time,
                sample_seq=sample_seq,
                sample_bytes=stream.sample_bytes,
                sample_period_ms=stream.sample_period_ms,
                trace_id=trace_id,
            )
        )
    return events, duplicates


def json_error(exc: EngineError) -> dict[str, Any]:
    return exc.as_dict()

