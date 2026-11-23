"""Pure deterministic event engine.

The engine never reads a wall clock.  Every mutation is derived from the
injected logical clock and the persisted state snapshot passed by the caller.
"""

from __future__ import annotations

import copy
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from .errors import (
    E_CLOCK_ROLLBACK,
    E_DEGRADATION_ILLEGAL_REGRESSION,
    E_HALF_OPEN_PROBE_OVERFLOW,
    E_POLICY_VERSION_CONFLICT,
    E_WINDOW_DIGEST_MISMATCH,
    PipelineError,
)
from .model import (
    Anomaly,
    BucketPolicy,
    Event,
    Policy,
    sha256_json,
)


MAX_WINDOW_HISTORY = 64
TOKEN_MILLI = 1000

DECISION_ALLOW = "ALLOW"
DECISION_REJECT = "REJECT"

ERROR_OK = "E_OK"
ERROR_RATE_LIMITED = "E_RATE_LIMITED"
ERROR_CIRCUIT_OPEN = "E_CIRCUIT_OPEN"
ERROR_HALF_OPEN_EXHAUSTED = "E_HALF_OPEN_PROBES_EXHAUSTED"
ERROR_DEGRADED = "E_DEGRADATION_LIMITED"

CrashHook = Callable[[str, int], None]


def failure_rate_bps(failures: int, total: int) -> int:
    if total <= 0:
        return 0
    return (failures * 10_000 + total // 2) // total


def format_milli(value: int) -> str:
    return f"{value // TOKEN_MILLI}.{value % TOKEN_MILLI:03d}"


def format_bps(value: int) -> str:
    return f"{value // 10_000}.{value % 10_000:04d}"


def _bucket_state(policy: BucketPolicy) -> Dict[str, Any]:
    return {
        "tokens_milli": policy.capacity_milli,
        "last_refill_ms": 0,
    }


def initial_state(policy: Policy) -> Dict[str, Any]:
    services: Dict[str, Any] = {}
    normal = policy.degradation.levels[policy.degradation.initial_index]
    for name in policy.service_order:
        node = policy.services[name]
        services[name] = {
            "name": name,
            "instance_id": node.instance_id,
            "priority": node.priority,
            "fault_domain": node.fault_domain,
            "bucket": _bucket_state(policy.service_buckets[name]),
            "circuit": {
                "state": "closed",
                "opened_at_ms": None,
                "open_until_ms": None,
                "half_open_successes": 0,
                "half_open_failures": 0,
                "probe_count": 0,
                "last_transition": "initial",
                "last_transition_ms": 0,
                "last_evaluated_window_id": None,
                "last_window_failure_rate_bps": 0,
                "trigger_window_id": None,
            },
            "windows": [],
            "last_outcome": None,
            "degradation": {
                "index": policy.degradation.initial_index,
                "name": normal.name,
                "ratio_bps": normal.ratio_bps,
                "stable_index": policy.degradation.initial_index,
                "healthy_streak": 0,
                "last_transition": "initial",
                "last_basis": {"reason": "initial"},
            },
        }
    return {
        "schema_version": policy.schema_version,
        "policy_version": policy.policy_version,
        "state_version": 0,
        "last_committed_event_seq": 0,
        "logical_clock_ms": 0,
        "global": {
            "bucket": _bucket_state(policy.global_bucket),
            "error_budget": {
                "entries": [],
                "failure_count": 0,
                "exhausted": False,
            },
        },
        "services": services,
    }


def _refill(
    policy: BucketPolicy,
    bucket: Dict[str, Any],
    clock_ms: int,
) -> int:
    last = int(bucket["last_refill_ms"])
    elapsed = max(0, clock_ms - last)
    added = elapsed * policy.refill_per_sec
    before = int(bucket["tokens_milli"])
    after = min(policy.capacity_milli, before + added)
    bucket["tokens_milli"] = after
    if clock_ms >= last:
        bucket["last_refill_ms"] = clock_ms
    return after - before


def _wait_ms(tokens_milli: int, refill_per_sec: int) -> int:
    if tokens_milli >= TOKEN_MILLI:
        return 0
    return (TOKEN_MILLI - tokens_milli + refill_per_sec - 1) // refill_per_sec


def _bucket_trace(
    policy: BucketPolicy,
    bucket: Dict[str, Any],
    added_milli: int,
    *,
    consumed_milli: int = 0,
) -> Dict[str, Any]:
    tokens = int(bucket["tokens_milli"])
    return {
        "capacity": policy.capacity,
        "capacity_milli": policy.capacity_milli,
        "refill_per_sec": policy.refill_per_sec,
        "burst_depth": policy.burst_depth,
        "tokens_milli": tokens,
        "tokens": format_milli(tokens),
        "last_refill_ms": int(bucket["last_refill_ms"]),
        "refill_added_milli": added_milli,
        "consumed_milli": consumed_milli,
        "wait_ms_if_deficient": _wait_ms(tokens, policy.refill_per_sec),
    }


def _window_material(window: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "window_id": int(window["window_id"]),
        "start_ms": int(window["start_ms"]),
        "end_ms": int(window["end_ms"]),
        "success": int(window["success"]),
        "failure": int(window["failure"]),
        "finalized": bool(window["finalized"]),
    }


def window_digest(window: Dict[str, Any]) -> str:
    return sha256_json(_window_material(window))


def _window_trace(window: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if window is None:
        return None
    total = int(window["success"]) + int(window["failure"])
    return {
        **_window_material(window),
        "failure_rate_bps": failure_rate_bps(int(window["failure"]), total),
        "failure_rate": format_bps(
            failure_rate_bps(int(window["failure"]), total)
        ),
        "total": total,
        "digest": window_digest(window),
        "evaluation_clock_ms": window.get("evaluation_clock_ms"),
        "evaluated": bool(window.get("evaluated", False)),
    }


def _ensure_current_window(
    policy: Policy,
    service_state: Dict[str, Any],
    clock_ms: int,
) -> Dict[str, Any]:
    window_id = clock_ms // policy.circuit.window_length_ms
    windows: List[Dict[str, Any]] = service_state["windows"]
    for window in reversed(windows):
        if int(window["window_id"]) == window_id:
            return window
    start = window_id * policy.circuit.window_length_ms
    window = {
        "window_id": window_id,
        "start_ms": start,
        "end_ms": start + policy.circuit.window_length_ms,
        "success": 0,
        "failure": 0,
        "finalized": False,
        "evaluated": False,
        "digest": None,
        "evaluation_clock_ms": None,
    }
    windows.append(window)
    return window


def _trim_windows(service_state: Dict[str, Any]) -> None:
    windows: List[Dict[str, Any]] = service_state["windows"]
    if len(windows) <= MAX_WINDOW_HISTORY:
        return
    unfinalized = [item for item in windows if not item["finalized"]]
    finalized = [item for item in windows if item["finalized"]]
    keep_finalized = finalized[-(MAX_WINDOW_HISTORY - len(unfinalized)) :]
    service_state["windows"] = sorted(
        keep_finalized + unfinalized,
        key=lambda item: int(item["window_id"]),
    )


def _advance_circuit_time(
    policy: Policy,
    service_state: Dict[str, Any],
    clock_ms: int,
) -> None:
    circuit = service_state["circuit"]
    if (
        circuit["state"] == "open"
        and circuit["open_until_ms"] is not None
        and clock_ms >= int(circuit["open_until_ms"])
    ):
        circuit["state"] = "half_open"
        circuit["half_open_successes"] = 0
        circuit["half_open_failures"] = 0
        circuit["probe_count"] = 0
        circuit["last_transition"] = "open_to_half_open"
        circuit["last_transition_ms"] = clock_ms


def _open_circuit(
    policy: Policy,
    service_state: Dict[str, Any],
    clock_ms: int,
    *,
    reason: str,
    window_id: Optional[int],
) -> None:
    circuit = service_state["circuit"]
    circuit["state"] = "open"
    circuit["opened_at_ms"] = clock_ms
    circuit["open_until_ms"] = clock_ms + policy.circuit.cooldown_ms
    circuit["half_open_successes"] = 0
    circuit["half_open_failures"] = 0
    circuit["probe_count"] = 0
    circuit["last_transition"] = reason
    circuit["last_transition_ms"] = clock_ms
    circuit["trigger_window_id"] = window_id


def _finalize_windows(
    policy: Policy,
    service_state: Dict[str, Any],
    clock_ms: int,
) -> List[Dict[str, Any]]:
    step = policy.circuit.step_ms
    evaluation_clock = (clock_ms // step) * step
    finalized: List[Dict[str, Any]] = []
    circuit = service_state["circuit"]
    for window in sorted(service_state["windows"], key=lambda item: item["window_id"]):
        if window["finalized"] or int(window["end_ms"]) > evaluation_clock:
            continue
        window["finalized"] = True
        window["digest"] = window_digest(window)
        window["evaluation_clock_ms"] = evaluation_clock
        total = int(window["success"]) + int(window["failure"])
        rate_bps = failure_rate_bps(int(window["failure"]), total)
        circuit["last_window_failure_rate_bps"] = rate_bps
        if circuit["state"] == "closed" and total > 0:
            threshold_numerator = (
                int(window["failure"]) * 1_000_000
            )
            threshold_denominator = policy.circuit.failure_rate_threshold_micro * total
            if threshold_numerator >= threshold_denominator:
                _open_circuit(
                    policy,
                    service_state,
                    clock_ms,
                    reason="window_failure_rate",
                    window_id=int(window["window_id"]),
                )
        window["evaluated"] = True
        circuit["last_evaluated_window_id"] = int(window["window_id"])
        finalized.append(window)
    if circuit["state"] == "open" and policy.circuit.cooldown_ms == 0:
        _advance_circuit_time(policy, service_state, clock_ms)
    _trim_windows(service_state)
    return finalized


def _prune_error_budget(policy: Policy, state: Dict[str, Any], clock_ms: int) -> None:
    if policy.global_error_budget is None:
        state["global"]["error_budget"] = {
            "entries": [],
            "failure_count": 0,
            "exhausted": False,
        }
        return
    budget = state["global"]["error_budget"]
    cutoff = clock_ms - policy.global_error_budget.window_ms
    entries = [
        item for item in budget["entries"] if int(item["clock_ms"]) > cutoff
    ]
    budget["entries"] = entries
    budget["failure_count"] = sum(1 for item in entries if item["failure"])
    budget["exhausted"] = (
        budget["failure_count"] >= policy.global_error_budget.max_failures
    )


def _record_error_budget_outcome(
    policy: Policy,
    state: Dict[str, Any],
    clock_ms: int,
    *,
    failure: bool,
) -> None:
    if policy.global_error_budget is None:
        return
    budget = state["global"]["error_budget"]
    budget["entries"].append(
        {
            "clock_ms": clock_ms,
            "failure": bool(failure),
        }
    )
    _prune_error_budget(policy, state, clock_ms)


def _budget_trace(policy: Policy, state: Dict[str, Any]) -> Dict[str, Any]:
    if policy.global_error_budget is None:
        return {
            "enabled": False,
            "failure_count": 0,
            "exhausted": False,
        }
    budget = state["global"]["error_budget"]
    return {
        "enabled": True,
        "max_failures": policy.global_error_budget.max_failures,
        "window_ms": policy.global_error_budget.window_ms,
        "failure_count": int(budget["failure_count"]),
        "exhausted": bool(budget["exhausted"]),
    }


def _degrade_one(
    policy: Policy,
    service_state: Dict[str, Any],
    *,
    clock_ms: int,
    event_seq: int,
    reason: str,
) -> Dict[str, Any]:
    degradation = service_state["degradation"]
    before_index = int(degradation["index"])
    target_index = max(0, before_index - 1)
    if target_index != before_index:
        level = policy.degradation.levels[target_index]
        degradation["index"] = target_index
        degradation["name"] = level.name
        degradation["ratio_bps"] = level.ratio_bps
        degradation["stable_index"] = target_index
        degradation["healthy_streak"] = 0
        degradation["last_transition"] = "degrade"
        degradation["last_basis"] = {
            "reason": reason,
            "event_seq": event_seq,
            "clock_ms": clock_ms,
            "from": policy.degradation.levels[before_index].name,
            "to": level.name,
        }
        return {
            "changed": True,
            "direction": "down",
            "reason": reason,
            "from": policy.degradation.levels[before_index].name,
            "to": level.name,
        }
    degradation["healthy_streak"] = 0
    degradation["last_transition"] = "degrade_floor"
    degradation["last_basis"] = {
        "reason": reason,
        "event_seq": event_seq,
        "clock_ms": clock_ms,
        "level": degradation["name"],
    }
    return {
        "changed": False,
        "direction": "floor",
        "reason": reason,
        "from": degradation["name"],
        "to": degradation["name"],
    }


def _recovery_target(policy: Policy, current_index: int) -> Optional[int]:
    name_to_index = policy.degradation.name_to_index
    for rung in policy.degradation.recovery_ladder:
        target = name_to_index[rung]
        if target > current_index:
            return target
    return None


def _record_healthy_success(
    policy: Policy,
    state: Dict[str, Any],
    service_state: Dict[str, Any],
    *,
    clock_ms: int,
    event_seq: int,
    basis: Dict[str, Any],
) -> Dict[str, Any]:
    degradation = service_state["degradation"]
    circuit = service_state["circuit"]
    normal_index = len(policy.degradation.levels) - 1
    if int(degradation["index"]) >= normal_index:
        degradation["healthy_streak"] = 0
        return {"changed": False, "direction": "normal", "reason": "already_normal"}
    if circuit["state"] != "closed":
        degradation["healthy_streak"] = 0
        return {
            "changed": False,
            "direction": "blocked",
            "reason": "breaker_not_closed",
            "breaker_state": circuit["state"],
        }
    if state["global"]["error_budget"]["exhausted"]:
        degradation["healthy_streak"] = 0
        return {
            "changed": False,
            "direction": "blocked",
            "reason": "global_error_budget_exhausted",
        }

    degradation["healthy_streak"] = int(degradation["healthy_streak"]) + 1
    required = policy.degradation.recovery_successes_per_step
    if int(degradation["healthy_streak"]) < required:
        return {
            "changed": False,
            "direction": "wait",
            "reason": "recovery_streak",
            "healthy_streak": int(degradation["healthy_streak"]),
            "required": required,
        }
    current_index = int(degradation["index"])
    target_index = _recovery_target(policy, current_index)
    if target_index is None:
        degradation["healthy_streak"] = 0
        return {"changed": False, "direction": "normal", "reason": "already_normal"}
    target_level = policy.degradation.levels[target_index]
    degradation["index"] = target_index
    degradation["name"] = target_level.name
    degradation["ratio_bps"] = target_level.ratio_bps
    degradation["stable_index"] = target_index
    degradation["healthy_streak"] = 0
    degradation["last_transition"] = "recover"
    recovery_basis = {
        **basis,
        "reason": "recovery_step",
        "event_seq": event_seq,
        "clock_ms": clock_ms,
        "from": policy.degradation.levels[current_index].name,
        "to": target_level.name,
        "service_health": "success",
        "persisted_breaker_state": circuit["state"],
        "persisted_state_version": int(state["state_version"]),
    }
    degradation["last_basis"] = recovery_basis
    return {
        "changed": True,
        "direction": "up",
        "reason": "recovery_step",
        "from": policy.degradation.levels[current_index].name,
        "to": target_level.name,
        "basis": recovery_basis,
    }


def _interrupt_recovery(
    policy: Policy,
    service_state: Dict[str, Any],
    *,
    clock_ms: int,
    event_seq: int,
    reason: str,
) -> Dict[str, Any]:
    degradation = service_state["degradation"]
    previous_streak = int(degradation["healthy_streak"])
    degradation["healthy_streak"] = 0
    degradation["last_transition"] = "recovery_interrupted"
    degradation["last_basis"] = {
        "reason": reason,
        "event_seq": event_seq,
        "clock_ms": clock_ms,
        "stable_level": degradation["name"],
        "previous_healthy_streak": previous_streak,
    }
    return {
        "changed": False,
        "direction": "hold",
        "reason": reason,
        "stable_level": degradation["name"],
        "previous_healthy_streak": previous_streak,
    }


def _anomaly_error(
    policy: Policy,
    state: Dict[str, Any],
    event: Event,
    anomaly: Anomaly,
) -> PipelineError:
    window_id = event.clock_ms // policy.circuit.window_length_ms
    details = {
        "event_seq": event.seq,
        "service": event.service,
        "stage": anomaly.stage,
        "window_id": window_id,
        "anomaly_id": anomaly.anomaly_id,
        "kind": anomaly.kind,
        "policy_version": policy.policy_version,
        "state_version": int(state["state_version"]),
    }
    details.update(anomaly.details)
    message = f"injected {anomaly.kind} at {anomaly.stage}"
    if anomaly.error_code == E_POLICY_VERSION_CONFLICT:
        details.setdefault("expected_policy_version", "injected-conflict")
        details["actual_policy_version"] = policy.policy_version
    elif anomaly.error_code == E_CLOCK_ROLLBACK:
        details.setdefault(
            "rollback_ms",
            policy.clock_rollback_tolerance_ms + 1,
        )
        details["tolerance_ms"] = policy.clock_rollback_tolerance_ms
    elif anomaly.error_code == E_WINDOW_DIGEST_MISMATCH:
        details.setdefault("provided_digest", "injected-mismatch")
        windows = state["services"][event.service]["windows"]
        if windows:
            latest = max(windows, key=lambda item: int(item["window_id"]))
            details.setdefault("window_id", int(latest["window_id"]))
            details["actual_digest"] = window_digest(latest)
        else:
            details["actual_digest"] = None
    elif anomaly.error_code == E_HALF_OPEN_PROBE_OVERFLOW:
        details.setdefault(
            "probe_count",
            policy.circuit.half_open_probe_count + 1,
        )
        details["probe_limit"] = policy.circuit.half_open_probe_count
        details["persisted_probe_count"] = int(
            state["services"][event.service]["circuit"]["probe_count"]
        )
    elif anomaly.error_code == E_DEGRADATION_ILLEGAL_REGRESSION:
        current = state["services"][event.service]["degradation"]
        details.setdefault("current_level", current["name"])
        details.setdefault("target_level", current["name"])
    return PipelineError(
        anomaly.error_code,
        message,
        stage=anomaly.stage,
        details=details,
    )


def _anomalies_at(
    policy: Policy,
    state: Dict[str, Any],
    event: Event,
    anomalies: Sequence[Anomaly],
    stage: str,
) -> None:
    for anomaly in anomalies:
        if anomaly.event_seq == event.seq and anomaly.stage == stage:
            raise _anomaly_error(policy, state, event, anomaly)


def _resolve_clock(
    policy: Policy,
    state: Dict[str, Any],
    event: Event,
) -> Tuple[int, int, bool]:
    previous = int(state["logical_clock_ms"])
    raw = event.clock_ms
    if raw >= previous:
        return raw, 0, False
    rollback = previous - raw
    if rollback > policy.clock_rollback_tolerance_ms:
        raise PipelineError(
            E_CLOCK_ROLLBACK,
            "logical clock rollback exceeds tolerance",
            stage="rate_limit",
            details={
                "event_seq": event.seq,
                "service": event.service,
                "stage": "rate_limit",
                "window_id": raw // policy.circuit.window_length_ms,
                "previous_clock_ms": previous,
                "event_clock_ms": raw,
                "rollback_ms": rollback,
                "tolerance_ms": policy.clock_rollback_tolerance_ms,
            },
        )
    return previous, rollback, True


def _rate_decision(
    policy: Policy,
    state: Dict[str, Any],
    service_state: Dict[str, Any],
    clock_ms: int,
) -> Tuple[bool, Dict[str, Any], int, int]:
    service_policy = policy.service_buckets[service_state["name"]]
    global_policy = policy.global_bucket
    service_added = _refill(service_policy, service_state["bucket"], clock_ms)
    global_added = _refill(global_policy, state["global"]["bucket"], clock_ms)
    service_tokens = int(service_state["bucket"]["tokens_milli"])
    global_tokens = int(state["global"]["bucket"]["tokens_milli"])
    service_available = min(service_tokens, service_policy.burst_milli)
    global_available = min(global_tokens, global_policy.burst_milli)
    single_event_cap = min(service_policy.burst_depth, global_policy.burst_depth)
    deficiencies: List[str] = []
    if service_available < TOKEN_MILLI:
        deficiencies.append("service")
    if global_available < TOKEN_MILLI:
        deficiencies.append("global")
    allowed = not deficiencies and single_event_cap >= 1
    wait_ms = 0
    if not allowed:
        wait_ms = max(
            _wait_ms(service_tokens, service_policy.refill_per_sec),
            _wait_ms(global_tokens, global_policy.refill_per_sec),
        )
    service_consumed = 0
    global_consumed = 0
    if allowed:
        service_state["bucket"]["tokens_milli"] = service_tokens - TOKEN_MILLI
        state["global"]["bucket"]["tokens_milli"] = global_tokens - TOKEN_MILLI
        service_consumed = TOKEN_MILLI
        global_consumed = TOKEN_MILLI
    trace = {
        "cost_milli": TOKEN_MILLI,
        "single_event_cap": single_event_cap,
        "allowed": allowed,
        "deficiencies": deficiencies,
        "wait_ms": wait_ms,
        "service": _bucket_trace(
            service_policy,
            service_state["bucket"],
            service_added,
            consumed_milli=service_consumed,
        ),
        "global": _bucket_trace(
            global_policy,
            state["global"]["bucket"],
            global_added,
            consumed_milli=global_consumed,
        ),
    }
    return allowed, trace, service_consumed, global_consumed


def _degradation_gate(
    policy: Policy,
    event: Event,
    service_state: Dict[str, Any],
) -> Tuple[bool, Dict[str, Any]]:
    degradation = service_state["degradation"]
    ratio_bps = int(degradation["ratio_bps"])
    traffic_slot = (event.seq * 2_654_435_761) % 10_000
    admitted = traffic_slot < ratio_bps
    return admitted, {
        "ratio_bps": ratio_bps,
        "ratio": format_bps(ratio_bps),
        "traffic_slot": traffic_slot,
        "slot_modulus": 10_000,
        "admitted": admitted,
        "level": degradation["name"],
    }


def _circuit_state_trace(circuit: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "state": circuit["state"],
        "opened_at_ms": circuit["opened_at_ms"],
        "open_until_ms": circuit["open_until_ms"],
        "half_open_successes": int(circuit["half_open_successes"]),
        "half_open_failures": int(circuit["half_open_failures"]),
        "probe_count": int(circuit["probe_count"]),
        "last_transition": circuit["last_transition"],
        "trigger_window_id": circuit.get("trigger_window_id"),
        "last_window_failure_rate_bps": int(
            circuit.get("last_window_failure_rate_bps", 0)
        ),
    }


def _degradation_trace(service_state: Dict[str, Any]) -> Dict[str, Any]:
    degradation = service_state["degradation"]
    return {
        "index": int(degradation["index"]),
        "name": degradation["name"],
        "ratio_bps": int(degradation["ratio_bps"]),
        "ratio": format_bps(int(degradation["ratio_bps"])),
        "stable_index": int(degradation["stable_index"]),
        "healthy_streak": int(degradation["healthy_streak"]),
        "last_transition": degradation["last_transition"],
        "last_basis": degradation["last_basis"],
    }


def apply_event(
    policy: Policy,
    state: Dict[str, Any],
    event: Event,
    anomalies: Sequence[Anomaly] = (),
    *,
    crash_hook: Optional[CrashHook] = None,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Apply one event to a copied state and return the committed decision.

    Any PipelineError leaves the caller's state untouched.  The caller must
    discard this copy after an error.
    """

    new_state = copy.deepcopy(state)
    _anomalies_at(policy, new_state, event, anomalies, "parse")
    if crash_hook is not None:
        crash_hook("parse", event.seq)

    clock_ms, rollback_ms, clamped = _resolve_clock(policy, new_state, event)
    new_state["logical_clock_ms"] = clock_ms
    service_state = new_state["services"][event.service]

    finalized_windows = _finalize_windows(policy, service_state, clock_ms)
    _advance_circuit_time(policy, service_state, clock_ms)
    _prune_error_budget(policy, new_state, clock_ms)
    _anomalies_at(policy, new_state, event, anomalies, "circuit")

    circuit_before = service_state["circuit"]["state"]
    degradation_before = service_state["degradation"]["name"]
    budget_before = bool(new_state["global"]["error_budget"]["exhausted"])

    circuit_anomaly_window_id: Optional[int] = None
    if finalized_windows:
        circuit_anomaly_window_id = int(finalized_windows[-1]["window_id"])
    current_window = _ensure_current_window(policy, service_state, clock_ms)

    _anomalies_at(policy, new_state, event, anomalies, "rate_limit")
    rate_allowed, rate_trace, service_consumed, global_consumed = _rate_decision(
        policy,
        new_state,
        service_state,
        clock_ms,
    )
    if crash_hook is not None:
        crash_hook("rate_limit", event.seq)

    decision = DECISION_ALLOW
    error_code = ERROR_OK
    reasons: List[str] = []
    admitted_for_outcome = False
    probe = False
    degradation_gate_trace: Optional[Dict[str, Any]] = None
    circuit_reject_reason: Optional[str] = None

    if not rate_allowed:
        decision = DECISION_REJECT
        error_code = ERROR_RATE_LIMITED
        reasons.append("rate_limit_insufficient_tokens")
    else:
        circuit = service_state["circuit"]
        if circuit["state"] == "open":
            decision = DECISION_REJECT
            error_code = ERROR_CIRCUIT_OPEN
            circuit_reject_reason = "breaker_open"
            reasons.append("circuit_open")
        elif circuit["state"] == "half_open":
            if int(circuit["probe_count"]) >= policy.circuit.half_open_probe_count:
                decision = DECISION_REJECT
                error_code = ERROR_HALF_OPEN_EXHAUSTED
                circuit_reject_reason = "half_open_probes_exhausted"
                reasons.append("half_open_probe_limit")
            else:
                probe = True
                admitted_for_outcome = True
                reasons.append("half_open_probe")
        elif circuit["state"] == "closed":
            degradation_admitted, degradation_gate_trace = _degradation_gate(
                policy,
                event,
                service_state,
            )
            if not degradation_admitted:
                decision = DECISION_REJECT
                error_code = ERROR_DEGRADED
                reasons.append("degradation_ratio")
            else:
                admitted_for_outcome = True
                reasons.append("degradation_ratio_admitted")
        else:
            raise PipelineError(
                "E_STORAGE_STATE",
                f"unknown circuit state: {circuit['state']}",
                details={"event_seq": event.seq, "service": event.service},
            )

    circuit = service_state["circuit"]
    result_recorded = False
    if admitted_for_outcome:
        if probe:
            circuit["probe_count"] = int(circuit["probe_count"]) + 1
            if event.success:
                circuit["half_open_successes"] = int(circuit["half_open_successes"]) + 1
            else:
                circuit["half_open_failures"] = int(circuit["half_open_failures"]) + 1
                _open_circuit(
                    policy,
                    service_state,
                    clock_ms,
                    reason="half_open_probe_failure",
                    window_id=None,
                )
            if (
                circuit["state"] == "half_open"
                and int(circuit["probe_count"]) >= policy.circuit.half_open_probe_count
                and int(circuit["half_open_successes"])
                >= policy.circuit.half_open_probe_count
                and int(circuit["half_open_failures"]) == 0
            ):
                circuit["state"] = "closed"
                circuit["opened_at_ms"] = None
                circuit["open_until_ms"] = None
                circuit["half_open_successes"] = 0
                circuit["half_open_failures"] = 0
                circuit["probe_count"] = 0
                circuit["last_transition"] = "half_open_to_closed"
                circuit["last_transition_ms"] = clock_ms
            result_recorded = True
        else:
            result_key = "success" if event.success else "failure"
            current_window[result_key] = int(current_window[result_key]) + 1
            result_recorded = True
        service_state["last_outcome"] = "success" if event.success else "failure"
        _record_error_budget_outcome(
            policy,
            new_state,
            clock_ms,
            failure=not event.success,
        )
    elif rate_allowed:
        service_state["last_outcome"] = None

    if crash_hook is not None:
        crash_hook("window_commit", event.seq)

    current_window = _ensure_current_window(policy, service_state, clock_ms)
    degradation_action: Dict[str, Any]
    budget_now = bool(new_state["global"]["error_budget"]["exhausted"])
    if circuit["state"] == "open":
        degradation_action = _degrade_one(
            policy,
            service_state,
            clock_ms=clock_ms,
            event_seq=event.seq,
            reason="circuit_open",
        )
    elif budget_now:
        degradation_action = _degrade_one(
            policy,
            service_state,
            clock_ms=clock_ms,
            event_seq=event.seq,
            reason="global_error_budget_exhausted",
        )
    elif admitted_for_outcome and event.success and circuit["state"] == "closed":
        degradation_action = _record_healthy_success(
            policy,
            new_state,
            service_state,
            clock_ms=clock_ms,
            event_seq=event.seq,
            basis={"budget_exhausted": budget_now},
        )
    elif (
        admitted_for_outcome
        and not event.success
        and (
            int(service_state["degradation"]["healthy_streak"]) > 0
            or int(service_state["degradation"]["index"])
            < len(policy.degradation.levels) - 1
        )
    ):
        degradation_action = _interrupt_recovery(
            policy,
            service_state,
            clock_ms=clock_ms,
            event_seq=event.seq,
            reason="failure_during_recovery",
        )
    else:
        degradation_action = {
            "changed": False,
            "direction": "hold",
            "reason": "no_recovery_signal",
        }

    _anomalies_at(policy, new_state, event, anomalies, "degradation")
    if crash_hook is not None:
        crash_hook("degradation", event.seq)

    if "degradation_ratio" in reasons:
        degradation_action = {
            **degradation_action,
            "rejected_by_ratio": True,
        }
    if not admitted_for_outcome and decision == DECISION_ALLOW:
        raise PipelineError(
            "E_INTERNAL",
            "decision policy produced an unclassified event",
            details={"event_seq": event.seq},
        )
    if decision == DECISION_ALLOW:
        error_code = ERROR_OK
        reasons.append("admitted")

    next_version = int(new_state["state_version"]) + 1
    new_state["state_version"] = next_version
    new_state["last_committed_event_seq"] = event.seq
    window_trace = _window_trace(current_window)
    trace: Dict[str, Any] = {
        "event_seq": event.seq,
        "service": event.service,
        "clock_ms": event.clock_ms,
        "effective_clock_ms": clock_ms,
        "clock_clamped": clamped,
        "clock_rollback_ms": rollback_ms,
        "result": "success" if event.success else "failure",
        "latency_us": event.latency_us,
        "decision": decision,
        "error_code": error_code,
        "reasons": reasons,
        "state_version": next_version,
        "rate_limit": rate_trace,
        "window": window_trace,
        "completed_windows": [_window_trace(item) for item in finalized_windows],
        "circuit": {
            "before": circuit_before,
            "after": circuit["state"],
            "transition": circuit["last_transition"],
            "probe": probe,
            "probe_count": int(circuit["probe_count"]),
            "reject_reason": circuit_reject_reason,
            "state_detail": _circuit_state_trace(circuit),
            "basis_window_id": circuit_anomaly_window_id
            if circuit_anomaly_window_id is not None
            else (
                int(current_window["window_id"])
                if circuit["last_transition"] != "window_failure_rate"
                else circuit.get("trigger_window_id")
            ),
        },
        "degradation": {
            "before": degradation_before,
            "after": service_state["degradation"]["name"],
            "gate": degradation_gate_trace,
            "action": degradation_action,
            "state": _degradation_trace(service_state),
        },
        "global_error_budget": {
            "before_exhausted": budget_before,
            "after_exhausted": bool(
                new_state["global"]["error_budget"]["exhausted"]
            ),
            **_budget_trace(policy, new_state),
        },
        "outcome_recorded": result_recorded,
        "tokens_consumed": {
            "service_milli": service_consumed,
            "global_milli": global_consumed,
        },
        "state_fingerprint": sha256_json(new_state),
    }
    return new_state, trace


def build_error_record(
    policy: Policy,
    state: Dict[str, Any],
    event: Event,
    error: PipelineError,
) -> Dict[str, Any]:
    """Build a deterministic non-mutating audit record for a rejected event."""

    details = dict(error.details)
    details.setdefault("event_seq", event.seq)
    details.setdefault("service", event.service)
    details.setdefault("stage", error.stage or "unknown")
    details.setdefault(
        "window_id",
        event.clock_ms // policy.circuit.window_length_ms,
    )
    return {
        "event_seq": event.seq,
        "service": event.service,
        "clock_ms": event.clock_ms,
        "result": "success" if event.success else "failure",
        "latency_us": event.latency_us,
        "status": "error",
        "error_code": error.code,
        "stage": error.stage,
        "message": error.message,
        "details": details,
        "state_version": int(state["state_version"]),
        "state_fingerprint": sha256_json(state),
    }


def replay_all(
    policy: Policy,
    events: Iterable[Event],
    anomalies: Sequence[Anomaly] = (),
    *,
    crash_hook: Optional[CrashHook] = None,
) -> Dict[str, Any]:
    state = initial_state(policy)
    records: List[Dict[str, Any]] = []
    anomalies_by_seq: Dict[int, List[Anomaly]] = {}
    for anomaly in anomalies:
        anomalies_by_seq.setdefault(anomaly.event_seq, []).append(anomaly)
    for event in events:
        event_anomalies = anomalies_by_seq.get(event.seq, [])
        try:
            state, trace = apply_event(
                policy,
                state,
                event,
                event_anomalies,
                crash_hook=crash_hook,
            )
            records.append({"status": "committed", **trace})
        except PipelineError as exc:
            record = build_error_record(policy, state, event, exc)
            records.append(record)
    return {
        "records": records,
        "final_state": state,
        "state_sha256": sha256_json(state),
    }

