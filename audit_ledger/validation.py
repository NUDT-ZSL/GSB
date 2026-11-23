from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .errors import AuditError, validation_error
from .models import (
    AuditEvent,
    InputBundle,
    Policy,
    QUOTA_ACTIONS,
    QuotaConfig,
    QuotaDeclaration,
    canonical_json,
    logical_window,
    sha256_digest,
)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _positive_int(value: Any, field: str, code: str) -> int:
    if not _is_int(value) or value <= 0:
        raise validation_error(code, f"{field} must be a positive integer", field=field)
    return value


def _non_negative_int(value: Any, field: str, code: str) -> int:
    if not _is_int(value) or value < 0:
        raise validation_error(
            code, f"{field} must be a non-negative integer", field=field
        )
    return value


def _read_json(path: str | Path) -> Any:
    source = Path(path)
    if not source.exists():
        raise validation_error(
            "AUD-INPUT-NOT-FOUND", f"input file not found: {source}", path=str(source)
        )
    try:
        with source.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise validation_error(
            "AUD-INPUT-INVALID-JSON",
            f"input file is not valid UTF-8 JSON: {source}",
            path=str(source),
            reason=str(exc),
        ) from exc


def parse_policy(raw: Any) -> Policy:
    if not isinstance(raw, dict):
        raise validation_error("AUD-POLICY-INVALID", "policy must be a JSON object")
    version = _positive_int(
        raw.get("policy_version"), "policy_version", "AUD-POLICY-INVALID"
    )
    categories = raw.get("allowed_categories")
    if (
        not isinstance(categories, list)
        or not categories
        or any(not isinstance(item, str) or not item for item in categories)
        or len(set(categories)) != len(categories)
    ):
        raise validation_error(
            "AUD-EVENT-CATEGORY-UNKNOWN",
            "allowed_categories must be a non-empty unique string array",
        )
    max_events = _positive_int(
        raw.get("max_events_per_round"),
        "max_events_per_round",
        "AUD-POLICY-INVALID",
    )
    budget = _positive_int(
        raw.get("logical_time_budget_ms"),
        "logical_time_budget_ms",
        "AUD-POLICY-INVALID",
    )
    clock_step = _positive_int(
        raw.get("clock_step_ms"), "clock_step_ms", "AUD-POLICY-INVALID"
    )
    retry_base = _positive_int(
        raw.get("retry_base_ms"), "retry_base_ms", "AUD-POLICY-INVALID"
    )
    retry_cap = _positive_int(
        raw.get("retry_cap_ms"), "retry_cap_ms", "AUD-POLICY-INVALID"
    )
    max_attempts = _positive_int(
        raw.get("max_attempts"), "max_attempts", "AUD-POLICY-INVALID"
    )
    regression = _non_negative_int(
        raw.get("max_clock_regression_ms"),
        "max_clock_regression_ms",
        "AUD-POLICY-INVALID",
    )
    window_ms = _positive_int(
        raw.get("window_ms", 86_400_000), "window_ms", "AUD-POLICY-INVALID"
    )
    if retry_cap < retry_base:
        raise validation_error(
            "AUD-POLICY-INVALID",
            "retry_cap_ms must be greater than or equal to retry_base_ms",
        )
    return Policy(
        policy_version=version,
        allowed_categories=tuple(categories),
        max_events_per_round=max_events,
        logical_time_budget_ms=budget,
        clock_step_ms=clock_step,
        retry_base_ms=retry_base,
        retry_cap_ms=retry_cap,
        max_attempts=max_attempts,
        max_clock_regression_ms=regression,
        window_ms=window_ms,
    )


def parse_quotas(raw: Any) -> QuotaConfig:
    if not isinstance(raw, dict):
        raise validation_error("AUD-QUOTA-INVALID", "quotas must be a JSON object")
    version = _positive_int(
        raw.get("quota_version"), "quota_version", "AUD-QUOTA-INVALID"
    )
    declarations_raw = raw.get("declarations")
    if not isinstance(declarations_raw, list) or not declarations_raw:
        raise validation_error(
            "AUD-QUOTA-INVALID", "declarations must be a non-empty array"
        )
    declarations: list[QuotaDeclaration] = []
    seen: set[str] = set()
    for index, item in enumerate(declarations_raw):
        if not isinstance(item, dict):
            raise validation_error(
                "AUD-QUOTA-INVALID",
                "each quota declaration must be an object",
                index=index,
            )
        tenant = item.get("tenant")
        if not isinstance(tenant, str) or not tenant:
            raise validation_error(
                "AUD-EVENT-TENANT-UNKNOWN",
                "quota tenant must be a non-empty string",
                index=index,
            )
        if tenant in seen:
            raise validation_error(
                "AUD-QUOTA-INVALID",
                "duplicate tenant in quota declarations",
                tenant=tenant,
            )
        seen.add(tenant)
        quota = _positive_int(
            item.get("daily_byte_quota"),
            "daily_byte_quota",
            "AUD-QUOTA-INVALID",
        )
        max_event = _positive_int(
            item.get("max_event_bytes"), "max_event_bytes", "AUD-QUOTA-INVALID"
        )
        action = item.get("quota_action")
        if action not in QUOTA_ACTIONS:
            raise validation_error(
                "AUD-QUOTA-INVALID",
                "quota_action must be one of reject, archive, allow_overflow",
                tenant=tenant,
            )
        declarations.append(
            QuotaDeclaration(
                tenant=tenant,
                daily_byte_quota=quota,
                max_event_bytes=max_event,
                quota_action=action,
            )
        )
    return QuotaConfig(
        quota_version=version,
        declarations=tuple(
            sorted(declarations, key=lambda declaration: declaration.tenant)
        ),
    )


def parse_events(
    raw: Any,
    quotas: QuotaConfig,
    policy: Policy,
) -> tuple[AuditEvent, ...]:
    if isinstance(raw, dict):
        stream_version = raw.get("stream_version", 1)
        events_raw = raw.get("events")
    else:
        stream_version = 1
        events_raw = raw
    stream_version = _positive_int(
        stream_version, "stream_version", "AUD-EVENT-SEQ-INVALID"
    )
    if not isinstance(events_raw, list):
        raise validation_error("AUD-EVENT-SEQ-INVALID", "events must be an array")

    tenant_map = quotas.by_tenant()
    categories = set(policy.allowed_categories)
    seen_sequences: set[int] = set()
    seen_idempotency: dict[str, AuditEvent] = {}
    events: list[AuditEvent] = []
    previous_seq: int | None = None

    for index, item in enumerate(events_raw):
        if not isinstance(item, dict):
            raise validation_error(
                "AUD-EVENT-INVALID", "event must be an object", index=index
            )
        seq = item.get("event_seq")
        if not _is_int(seq) or seq < 0:
            raise validation_error(
                "AUD-EVENT-SEQ-INVALID",
                "event_seq must be a non-negative integer",
                index=index,
            )
        if seq in seen_sequences:
            raise validation_error(
                "AUD-EVENT-SEQ-DUPLICATE",
                "event_seq must be unique",
                event_seq=seq,
            )
        if previous_seq is not None and seq <= previous_seq:
            raise validation_error(
                "AUD-EVENT-SEQ-NOT-INCREASING",
                "event_seq must be strictly increasing",
                event_seq=seq,
                previous_event_seq=previous_seq,
            )
        seen_sequences.add(seq)
        previous_seq = seq

        tenant = item.get("tenant")
        if not isinstance(tenant, str) or tenant not in tenant_map:
            raise validation_error(
                "AUD-EVENT-TENANT-UNKNOWN",
                "tenant is not declared in quota configuration",
                tenant=tenant,
                event_seq=seq,
            )
        category = item.get("category")
        if not isinstance(category, str) or category not in categories:
            raise validation_error(
                "AUD-EVENT-CATEGORY-UNKNOWN",
                "event category is outside the closed category enum",
                tenant=tenant,
                event_seq=seq,
            )
        if "payload" not in item:
            raise validation_error(
                "AUD-EVENT-PAYLOAD-MISSING",
                "event payload is required",
                tenant=tenant,
                event_seq=seq,
            )
        payload = item["payload"]
        digest = item.get("payload_digest")
        actual_digest = sha256_digest(payload)
        if not isinstance(digest, str) or digest != actual_digest:
            raise validation_error(
                "AUD-EVENT-DIGEST-MISMATCH",
                "payload_digest does not match canonical payload bytes",
                tenant=tenant,
                event_seq=seq,
                expected=actual_digest,
                actual=digest,
            )
        logical_ts = item.get("logical_ts_ms")
        if not _is_int(logical_ts) or logical_ts < 0:
            raise validation_error(
                "AUD-EVENT-CLOCK-INVALID",
                "logical_ts_ms must be a non-negative integer",
                tenant=tenant,
                event_seq=seq,
            )
        idempotency_key = item.get("idempotency_key")
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise validation_error(
                "AUD-IDEMPOTENCY-INVALID",
                "idempotency_key must be a non-empty string",
                tenant=tenant,
                event_seq=seq,
            )
        event_version = item.get("event_version", 1)
        event_version = _positive_int(
            event_version, "event_version", "AUD-EVENT-VERSION-INVALID"
        )
        event = AuditEvent(
            event_seq=seq,
            tenant=tenant,
            category=category,
            payload=payload,
            payload_digest=digest,
            logical_ts_ms=logical_ts,
            idempotency_key=idempotency_key,
            event_version=event_version,
            stream_version=stream_version,
        )
        prior = seen_idempotency.get(idempotency_key)
        if prior is not None:
            raise validation_error(
                "AUD-IDEMPOTENCY-CONFLICT",
                "duplicate idempotency_key in event stream",
                tenant=tenant,
                window_id=logical_window(logical_ts, policy.window_ms)[0],
                event_seq=seq,
                prior_event_seq=prior.event_seq,
            )
        seen_idempotency[idempotency_key] = event
        events.append(event)
    return tuple(events)


def load_bundle(
    events_path: str | Path,
    quotas_path: str | Path,
    policy_path: str | Path,
) -> InputBundle:
    quota_config = parse_quotas(_read_json(quotas_path))
    policy = parse_policy(_read_json(policy_path))
    events = parse_events(_read_json(events_path), quota_config, policy)
    return InputBundle(events=events, quotas=quota_config, policy=policy)


def normalized_event_json(event: AuditEvent) -> str:
    return canonical_json(event.to_dict())
