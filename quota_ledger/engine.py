"""Pure deterministic adjudication shared by writes and verification."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

from .errors import (
    E_CLOCK_ROLLBACK,
    E_EVENT_SEQ_CONFLICT,
    E_HARD_QUOTA,
    E_IDEMPOTENCY_CONFLICT,
    E_INTERNAL,
    E_OBJECT_NOT_FOUND,
    E_RATE_LIMITED,
    LedgerError,
)
from .models import (
    Event,
    LimiterConfig,
    ObjectRecord,
    QuotaConfig,
    QuotaLayer,
    State,
    TenantRateConfig,
    TokenState,
    Usage,
)
from .utils import TOKEN_SCALE, tokens_text


@dataclass(frozen=True)
class Adjudication:
    result: dict[str, Any]
    replayed: bool


def _quota_state(usage: Usage, layer: QuotaLayer) -> str:
    if (
        usage.used_bytes >= layer.hard_bytes
        or usage.used_objects >= layer.hard_objects
    ):
        return "HARD"
    if (
        usage.soft_active
        or usage.used_bytes > layer.soft_bytes
        or usage.used_objects > layer.soft_objects
    ):
        return "SOFT"
    return "NORMAL"


def _projected_quota_state(
    projected_bytes: int,
    projected_objects: int,
    layer: QuotaLayer,
) -> str:
    if projected_bytes >= layer.hard_bytes or projected_objects >= layer.hard_objects:
        return "HARD"
    if projected_bytes > layer.soft_bytes or projected_objects > layer.soft_objects:
        return "SOFT"
    return "NORMAL"


def _ceil_div(numerator: int, denominator: int) -> int:
    return (numerator + denominator - 1) // denominator


def calculate_rate_limit(
    token: TokenState,
    policy: TenantRateConfig,
    clock_ms: int,
) -> dict[str, Any]:
    elapsed_ms = max(0, clock_ms - token.last_refill_ms)
    numerator = (
        token.refill_remainder_num
        + elapsed_ms * policy.rate_numerator * 1000
    )
    earned_micro = numerator // policy.rate_denominator
    remainder_after = numerator % policy.rate_denominator
    uncapped_micro = token.tokens_micro + earned_micro
    after_refill_micro = min(token.capacity_micro, uncapped_micro)
    if after_refill_micro == token.capacity_micro:
        remainder_after = 0

    required_micro = TOKEN_SCALE
    passed = after_refill_micro >= required_micro
    if passed:
        wait_ms = 0
        retry_after_ms = 0
    else:
        deficit_micro = required_micro - after_refill_micro
        wait_ms = _ceil_div(
            deficit_micro * policy.rate_denominator,
            policy.rate_numerator * 1000,
        )
        retry_after_ms = max(wait_ms, policy.backoff_window_ms)

    return {
        "tenant": policy.tenant,
        "policy": policy.policy_dict(),
        "clock_ms": clock_ms,
        "tokens_before_micro": token.tokens_micro,
        "tokens_before": tokens_text(token.tokens_micro),
        "last_refill_ms": token.last_refill_ms,
        "elapsed_ms": elapsed_ms,
        "earned_micro": earned_micro,
        "remainder_before": token.refill_remainder_num,
        "remainder_after": remainder_after,
        "tokens_after_refill_micro": after_refill_micro,
        "tokens_after_refill": tokens_text(after_refill_micro),
        "required_micro": required_micro,
        "decision": "PASS" if passed else "DENY",
        "wait_ms": wait_ms,
        "backoff_window_ms": policy.backoff_window_ms,
        "retry_after_ms": retry_after_ms,
        "refill_applied": False,
    }


def _metric_check(
    metric: str,
    observed: int,
    delta: int,
    soft_threshold: int,
    hard_threshold: int,
) -> dict[str, Any]:
    projected = observed + delta
    soft_exceeded = projected > soft_threshold
    hard_exceeded = projected > hard_threshold
    if hard_exceeded:
        result = "HARD"
    elif soft_exceeded:
        result = "SOFT"
    else:
        result = "PASS"
    return {
        "metric": metric,
        "observed": observed,
        "delta": delta,
        "projected": projected,
        "soft_threshold": soft_threshold,
        "hard_threshold": hard_threshold,
        "soft_exceeded": soft_exceeded,
        "hard_exceeded": hard_exceeded,
        "result": result,
    }


def _layer_basis(
    layer: QuotaLayer,
    usage: Usage,
    delta_bytes: int,
    delta_objects: int,
) -> dict[str, Any]:
    checks = [
        _metric_check(
            "bytes",
            usage.used_bytes,
            delta_bytes,
            layer.soft_bytes,
            layer.hard_bytes,
        ),
        _metric_check(
            "objects",
            usage.used_objects,
            delta_objects,
            layer.soft_objects,
            layer.hard_objects,
        ),
    ]
    return {
        "level": layer.level,
        "path": layer.path,
        "tenant": layer.tenant,
        "bucket": layer.bucket or None,
        "prefix": layer.prefix or None,
        "used_before": {
            "bytes": usage.used_bytes,
            "objects": usage.used_objects,
        },
        "delta": {"bytes": delta_bytes, "objects": delta_objects},
        "used_projected": {
            "bytes": usage.used_bytes + delta_bytes,
            "objects": usage.used_objects + delta_objects,
        },
        "limits": {
            "bytes": layer.max_bytes,
            "objects": layer.max_objects,
            "soft_pct": layer.soft_pct,
            "hard_pct": layer.hard_pct,
        },
        "soft_thresholds": {
            "bytes": layer.soft_bytes,
            "objects": layer.soft_objects,
        },
        "hard_thresholds": {
            "bytes": layer.hard_bytes,
            "objects": layer.hard_objects,
        },
        "checks": checks,
        "quota_state_before": _quota_state(usage, layer),
        "quota_state_projected": _projected_quota_state(
            usage.used_bytes + delta_bytes,
            usage.used_objects + delta_objects,
            layer,
        ),
        "soft_active_before": usage.soft_active,
    }


def _first_hard_violation(layer_basis: list[dict[str, Any]]) -> dict[str, Any] | None:
    for layer in layer_basis:
        for check in layer["checks"]:
            if check["hard_exceeded"]:
                return {
                    "kind": "hard_quota",
                    "level": layer["level"],
                    "path": layer["path"],
                    "metric": check["metric"],
                    "observed": check["observed"],
                    "delta": check["delta"],
                    "projected": check["projected"],
                    "limit": check["hard_threshold"],
                    "limit_kind": "hard",
                }
    return None


def _rate_violation(rate_basis: dict[str, Any], tenant_path: str) -> dict[str, Any]:
    return {
        "kind": "rate_limit",
        "level": "tenant",
        "path": tenant_path,
        "metric": "tokens",
        "available_micro": rate_basis["tokens_after_refill_micro"],
        "required_micro": rate_basis["required_micro"],
        "wait_ms": rate_basis["wait_ms"],
        "retry_after_ms": rate_basis["retry_after_ms"],
    }


def _decision_order(layers: tuple[QuotaLayer, QuotaLayer, QuotaLayer]) -> list[str]:
    order = ["rate_limit"]
    for layer in layers:
        order.append(f"{layer.level}.bytes")
        order.append(f"{layer.level}.objects")
    return order


def _record_decision(state: State, event: Event, result: dict[str, Any]) -> None:
    state.last_decided_seq = event.seq
    state.idempotency[event.idempotency_key] = (
        event.fingerprint,
        copy.deepcopy(result),
    )
    state.decision_results.append(result)


def _decision_result(
    event: Event,
    decision: str,
    code: str,
    first_violation: dict[str, Any] | None,
    order: list[str],
    basis: dict[str, Any],
    applied: dict[str, Any] | None,
    soft_transitions: list[dict[str, Any]],
    state_effect: str,
) -> dict[str, Any]:
    return {
        "ok": decision == "ALLOW",
        "decision": decision,
        "code": code,
        "event": event.payload(),
        "fingerprint": event.fingerprint,
        "operation": event.operation,
        "state_effect": state_effect,
        "first_violation": first_violation,
        "decision_order": order,
        "basis": basis,
        "applied": applied,
        "soft_transitions": soft_transitions,
    }


def adjudicate(
    state: State,
    event: Event,
    quota: QuotaConfig,
    limiter: LimiterConfig,
) -> Adjudication:
    existing = state.idempotency.get(event.idempotency_key)
    if existing is not None:
        old_fingerprint, old_result = existing
        if old_fingerprint != event.fingerprint:
            raise LedgerError(
                E_IDEMPOTENCY_CONFLICT,
                "idempotency key was already used by a different payload",
                {
                    "idempotency_key": event.idempotency_key,
                    "first_seq": old_result.get("event", {}).get("seq"),
                    "incoming_seq": event.seq,
                },
            )
        return Adjudication(copy.deepcopy(old_result), True)

    if event.seq <= state.last_decided_seq:
        raise LedgerError(
            E_EVENT_SEQ_CONFLICT,
            "event sequence was already decided or is not increasing",
            {
                "seq": event.seq,
                "last_decided_seq": state.last_decided_seq,
            },
        )

    tenant_layer, bucket_layer, prefix_layer = quota.event_layers(event)
    layers = (tenant_layer, bucket_layer, prefix_layer)
    policy = limiter.tenant(event.tenant)
    token = state.tokens[event.tenant]
    rollback_ms = token.last_seen_ms - event.clock_ms
    if rollback_ms > limiter.clock_rollback_tolerance_ms:
        raise LedgerError(
            E_CLOCK_ROLLBACK,
            "logical clock moved backwards beyond tolerance",
            {
                "tenant": event.tenant,
                "last_seen_ms": token.last_seen_ms,
                "event_clock_ms": event.clock_ms,
                "rollback_ms": rollback_ms,
                "tolerance_ms": limiter.clock_rollback_tolerance_ms,
            },
        )

    order = _decision_order(layers)
    identity = (
        event.tenant,
        event.bucket,
        event.prefix,
        event.object_key,
    )
    existing_object = state.objects.get(identity)

    if event.deleted and existing_object is None:
        token.last_seen_ms = max(token.last_seen_ms, event.clock_ms)
        result = _decision_result(
            event=event,
            decision="DENY",
            code=E_OBJECT_NOT_FOUND,
            first_violation={
                "kind": "object_existence",
                "level": "prefix",
                "path": prefix_layer.path,
                "metric": "object",
                "object_key": event.object_key,
            },
            order=["object_existence"],
            basis={
                "object_check": {
                    "identity": {
                        "tenant": event.tenant,
                        "bucket": event.bucket,
                        "prefix": event.prefix,
                        "object_key": event.object_key,
                    },
                    "found": False,
                }
            },
            applied=None,
            soft_transitions=[],
            state_effect="DECISION_ONLY",
        )
        _record_decision(state, event, result)
        return Adjudication(result, False)

    if event.deleted:
        assert existing_object is not None
        delta_bytes = -existing_object.size_bytes
        delta_objects = -1
        object_action = "DELETE"
    elif existing_object is None:
        delta_bytes = event.bytes
        delta_objects = 1
        object_action = "CREATE"
    else:
        delta_bytes = event.bytes - existing_object.size_bytes
        delta_objects = 0
        object_action = "REPLACE"

    layer_basis = [
        _layer_basis(
            layer,
            state.usage[layer.path],
            delta_bytes,
            delta_objects,
        )
        for layer in layers
    ]
    hard_violation = _first_hard_violation(layer_basis)
    rate_basis = calculate_rate_limit(token, policy, event.clock_ms)
    rate_failed = rate_basis["decision"] == "DENY"

    basis = {
        "rate_limit": rate_basis,
        "layers": layer_basis,
    }

    if rate_failed or hard_violation is not None:
        token.last_seen_ms = max(token.last_seen_ms, event.clock_ms)
        if rate_failed:
            first_violation = _rate_violation(rate_basis, tenant_layer.path)
            code = E_RATE_LIMITED
        else:
            first_violation = hard_violation
            code = E_HARD_QUOTA
        result = _decision_result(
            event=event,
            decision="DENY",
            code=code,
            first_violation=first_violation,
            order=order,
            basis=basis,
            applied=None,
            soft_transitions=[],
            state_effect="DECISION_ONLY",
        )
        _record_decision(state, event, result)
        return Adjudication(result, False)

    token.tokens_micro = rate_basis["tokens_after_refill_micro"] - TOKEN_SCALE
    token.last_refill_ms = max(token.last_refill_ms, event.clock_ms)
    token.last_seen_ms = max(token.last_seen_ms, event.clock_ms)
    token.refill_remainder_num = rate_basis["remainder_after"]
    rate_basis["refill_applied"] = True

    soft_transitions: list[dict[str, Any]] = []
    for layer in layers:
        usage = state.usage[layer.path]
        usage.used_bytes += delta_bytes
        usage.used_objects += delta_objects
        if usage.used_bytes < 0 or usage.used_objects < 0:
            raise LedgerError(
                E_INTERNAL,
                "negative usage after accepted operation",
                {"path": layer.path},
            )
        soft_now = (
            usage.used_bytes > layer.soft_bytes
            or usage.used_objects > layer.soft_objects
        )
        if not usage.soft_active and soft_now:
            usage.soft_active = True
            usage.soft_entry_seq = event.seq
            usage.soft_entry_clock_ms = event.clock_ms
            usage.soft_entry_used_bytes = usage.used_bytes
            usage.soft_entry_used_objects = usage.used_objects
            usage.soft_entry_soft_bytes = layer.soft_bytes
            usage.soft_entry_soft_objects = layer.soft_objects
            usage.soft_exit_seq = None
            usage.soft_exit_clock_ms = None
            usage.soft_exit_used_bytes = None
            usage.soft_exit_used_objects = None
            usage.soft_exit_soft_bytes = None
            usage.soft_exit_soft_objects = None
            soft_transitions.append(
                {
                    "transition": "ENTER",
                    "level": layer.level,
                    "path": layer.path,
                    "event_seq": event.seq,
                    "clock_ms": event.clock_ms,
                    "used_bytes": usage.used_bytes,
                    "used_objects": usage.used_objects,
                    "soft_bytes_limit": layer.soft_bytes,
                    "soft_objects_limit": layer.soft_objects,
                }
            )
        elif usage.soft_active and not soft_now:
            usage.soft_active = False
            usage.soft_exit_seq = event.seq
            usage.soft_exit_clock_ms = event.clock_ms
            usage.soft_exit_used_bytes = usage.used_bytes
            usage.soft_exit_used_objects = usage.used_objects
            usage.soft_exit_soft_bytes = layer.soft_bytes
            usage.soft_exit_soft_objects = layer.soft_objects
            soft_transitions.append(
                {
                    "transition": "EXIT",
                    "level": layer.level,
                    "path": layer.path,
                    "event_seq": event.seq,
                    "clock_ms": event.clock_ms,
                    "used_bytes": usage.used_bytes,
                    "used_objects": usage.used_objects,
                    "soft_bytes_limit": layer.soft_bytes,
                    "soft_objects_limit": layer.soft_objects,
                }
            )

    if event.deleted:
        del state.objects[identity]
    elif existing_object is None:
        state.objects[identity] = ObjectRecord(
            tenant=event.tenant,
            bucket=event.bucket,
            prefix=event.prefix,
            object_key=event.object_key,
            size_bytes=event.bytes,
            created_seq=event.seq,
            updated_seq=event.seq,
        )
    else:
        existing_object.size_bytes = event.bytes
        existing_object.updated_seq = event.seq

    applied = {
        "bytes_delta": delta_bytes,
        "objects_delta": delta_objects,
        "object_action": object_action,
        "object_size_bytes": event.bytes,
        "tokens_after_micro": token.tokens_micro,
        "tokens_after": tokens_text(token.tokens_micro),
    }
    result = _decision_result(
        event=event,
        decision="ALLOW",
        code="OK",
        first_violation=None,
        order=order,
        basis=basis,
        applied=applied,
        soft_transitions=soft_transitions,
        state_effect="COMMITTED",
    )
    _record_decision(state, event, result)
    return Adjudication(result, False)

