"""Pure deterministic routing, window cut and batch adjudication."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR
from typing import Any, Iterable

from .errors import BusinessError
from .fixture import (
    content_hash,
    request_compute_units,
    request_kv_memory_mib,
)
from .fixed import (
    FixedLedger,
    Quantity,
    decimal_string,
    fixed_normalized,
    fixed_to_ceiling_integer,
)
from .models import Fixture, ReplicaSpec, RequestSpec, Route

STATE_VERSION = 1
CONSTRAINT_ORDER = (
    "max_batch_size",
    "compute_budget",
    "memory_budget",
    "deadline",
)


@dataclass(frozen=True)
class BatchPlan:
    replica_id: str
    window_index: int
    version: int
    batch_index: int
    request_ids: tuple[int, ...]
    start_ms: int
    finish_ms: int
    metrics: dict[str, Any]
    decision: dict[str, Any]
    error_spectrum: tuple[dict[str, Any], ...]
    precision_events: tuple[dict[str, Any], ...]
    precision_sensitive: bool
    plan_hash: str

    @property
    def batch_key(self) -> str:
        return (
            f"{self.replica_id}:w{self.window_index:08d}:"
            f"v{self.version}:b{self.batch_index:08d}"
        )

    def to_dict(self, include_hash: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "batch_key": self.batch_key,
            "batch_index": self.batch_index,
            "replica_id": self.replica_id,
            "window_index": self.window_index,
            "version": self.version,
            "request_ids": list(self.request_ids),
            "start_ms": self.start_ms,
            "finish_ms": self.finish_ms,
            "metrics": self.metrics,
            "decision": self.decision,
            "error_spectrum": list(self.error_spectrum),
            "precision_events": list(self.precision_events),
            "precision_sensitive": self.precision_sensitive,
        }
        if include_hash:
            result["plan_hash"] = self.plan_hash
        return result


@dataclass(frozen=True)
class WindowPlan:
    replica_id: str
    window_index: int
    version: int
    start_ms: int
    end_ms: int
    request_ids: tuple[int, ...]
    batches: tuple[BatchPlan, ...]
    expired: tuple[dict[str, Any], ...]
    decision: dict[str, Any]
    precision_events: tuple[dict[str, Any], ...]
    precision_sensitive: bool
    plan_hash: str

    def to_dict(self, include_hash: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "replica_id": self.replica_id,
            "window_index": self.window_index,
            "version": self.version,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "request_ids": list(self.request_ids),
            "batches": [batch.to_dict() for batch in self.batches],
            "expired": list(self.expired),
            "decision": self.decision,
            "precision_events": list(self.precision_events),
            "precision_sensitive": self.precision_sensitive,
        }
        if include_hash:
            result["plan_hash"] = self.plan_hash
        return result


def _fixed_limit(limit: float, scale: float) -> int:
    return int(
        (Decimal(str(float(limit))) * Decimal(str(float(scale)))).to_integral_value(
            rounding=ROUND_FLOOR
        )
    )


def _request_observation(fixture: Fixture, request: RequestSpec) -> dict[str, Any]:
    return {
        "request_id": request.request_id,
        "arrival_ms": request.arrival_ms,
        "priority": request.priority,
        "input_tokens": request.input_tokens,
        "max_output_tokens": request.max_output_tokens,
        "sequence_tokens": request.sequence_tokens,
        "deadline_ms": request.deadline_ms,
        "compute_raw": request_compute_units(fixture.model, request),
        "memory_raw": request_kv_memory_mib(fixture.model, request),
    }


def _route_candidate(
    fixture: Fixture,
    request: RequestSpec,
    replica: ReplicaSpec,
    existing_compute: float,
) -> dict[str, Any]:
    window_index = request.arrival_ms // replica.window_ms
    window_start = window_index * replica.window_ms
    window_end = window_start + replica.window_ms
    request_compute = request_compute_units(fixture.model, request)
    projected_compute = existing_compute + request_compute
    duration_ms = (
        fixture.model.batch_overhead_ms
        + projected_compute / replica.compute_capacity * 1000.0
    )
    raw_score = float(window_end) + duration_ms
    ledger = FixedLedger(fixture.accuracy.rounding_mode)
    score_entry = ledger.quantize(
        f"route:req:{request.request_id:08d}:candidate:{replica.replica_id}:score",
        "candidate projected finish",
        raw_score,
        fixture.accuracy.latency_scale,
    )
    fixed_value = score_entry.fixed_value
    actual = Decimal(fixed_value) / Decimal(str(fixture.accuracy.latency_scale))
    if actual > Decimal(str(raw_score)):
        rounding = "up"
    elif actual < Decimal(str(raw_score)):
        rounding = "down"
    else:
        rounding = "none"
    return {
        "replica_id": replica.replica_id,
        "window_index": window_index,
        "window_start_ms": window_start,
        "window_end_ms": window_end,
        "window_ms": replica.window_ms,
        "sequence_tokens": request.sequence_tokens,
        "compute_capacity": replica.compute_capacity,
        "batch_overhead_ms": fixture.model.batch_overhead_ms,
        "existing_compute_raw": existing_compute,
        "request_compute_raw": request_compute,
        "projected_compute_raw": projected_compute,
        "projected_finish_ms_raw": raw_score,
        "score_fixed_value": fixed_value,
        "score_scale": decimal_string(Decimal(str(fixture.accuracy.latency_scale))),
        "score_rounding": rounding,
        "score_error_upper_bound": decimal_string(score_entry.error_upper_bound),
        "score_source_chain": [score_entry.entry_id],
    }


def route_requests(fixture: Fixture) -> tuple[Route, ...]:
    """Route every request exactly once using a deterministic projected-finish score."""

    window_compute: dict[tuple[str, int], float] = {}
    routes: list[Route] = []
    ordered = sorted(
        fixture.requests,
        key=lambda request: (request.arrival_ms, request.priority, request.request_id),
    )
    for request in ordered:
        candidates: list[dict[str, Any]] = []
        for replica in fixture.replicas:
            if request.sequence_tokens > replica.max_sequence_length:
                continue
            request_compute = request_compute_units(fixture.model, request)
            request_memory = (
                fixture.model.memory_base_mib
                + request_kv_memory_mib(fixture.model, request)
            )
            if request_compute > replica.compute_capacity:
                continue
            if request_memory > replica.memory_limit_mib:
                continue
            window_index = request.arrival_ms // replica.window_ms
            existing = window_compute.get((replica.replica_id, window_index), 0.0)
            candidates.append(_route_candidate(fixture, request, replica, existing))
        if not candidates:
            raise BusinessError(
                "E_ROUTE_INFEASIBLE",
                f"request {request.request_id} has no feasible replica",
            )
        candidates.sort(
            key=lambda item: (item["projected_finish_ms_raw"], item["replica_id"])
        )
        chosen = candidates[0]
        events: list[dict[str, Any]] = []
        if len(candidates) > 1:
            runner_up = candidates[1]
            margin = abs(
                runner_up["projected_finish_ms_raw"]
                - chosen["projected_finish_ms_raw"]
            )
            fixed_margin = abs(
                runner_up["score_fixed_value"] - chosen["score_fixed_value"]
            )
            fixed_margin_normalized = float(
                Decimal(fixed_margin)
                / Decimal(str(fixture.accuracy.latency_scale))
            )
            if (
                margin < fixture.accuracy.float_tolerance
                or fixed_margin_normalized < fixture.accuracy.float_tolerance
            ):
                chain: list[str] = []
                for item in (chosen, runner_up):
                    for entry_id in item["score_source_chain"]:
                        if entry_id not in chain:
                            chain.append(entry_id)
                events.append(
                    {
                        "event_id": f"route:req:{request.request_id:08d}:precision",
                        "kind": "replica_choice",
                        "decision": "minimum_projected_finish",
                        "chosen_replica_id": chosen["replica_id"],
                        "runner_up_replica_id": runner_up["replica_id"],
                        "raw_difference": margin,
                        "fixed_difference": fixed_margin,
                        "fixed_difference_normalized": fixed_margin_normalized,
                        "float_tolerance": fixture.accuracy.float_tolerance,
                        "scale": chosen["score_scale"],
                        "candidates": candidates,
                        "source_chain": chain,
                    }
                )
        key = (chosen["replica_id"], chosen["window_index"])
        window_compute[key] = window_compute.get(key, 0.0) + request_compute_units(
            fixture.model, request
        )
        routes.append(
            Route(
                request_id=request.request_id,
                replica_id=chosen["replica_id"],
                window_index=chosen["window_index"],
                window_start_ms=chosen["window_start_ms"],
                window_end_ms=chosen["window_end_ms"],
                version=STATE_VERSION,
                precision_events=tuple(events),
            )
        )
    return tuple(sorted(routes, key=lambda route: route.request_id))


def _batch_prefix(
    replica_id: str, window_index: int, version: int, batch_index: int
) -> str:
    return (
        f"{replica_id}:w{window_index:08d}:v{version}:b{batch_index:08d}"
    )


def _request_prefix(
    replica_id: str,
    window_index: int,
    version: int,
    batch_index: int,
    request_id: int,
) -> str:
    return (
        _batch_prefix(replica_id, window_index, version, batch_index)
        + f":r{request_id:08d}"
    )


def _rounding_direction(quantity: Quantity) -> str:
    actual = fixed_normalized(quantity)
    raw = Decimal(quantity.raw_text)
    if actual > raw:
        return "up"
    if actual < raw:
        return "down"
    return "none"


def _estimate_batch(
    fixture: Fixture,
    replica: ReplicaSpec,
    requests: Iterable[RequestSpec],
    *,
    window_index: int,
    batch_index: int,
    version: int,
    start_ms: int,
    window_end_ms: int,
) -> tuple[
    dict[str, Any],
    list[dict[str, Any]],
    dict[str, Quantity],
    tuple[dict[str, Any], ...],
]:
    request_list = tuple(requests)
    ledger = FixedLedger(fixture.accuracy.rounding_mode)
    compute_quantities: list[Quantity] = []
    memory_quantities: list[Quantity] = []
    observations: list[dict[str, Any]] = []
    for request in request_list:
        prefix = _request_prefix(
            replica.replica_id,
            window_index,
            version,
            batch_index,
            request.request_id,
        )
        compute_raw = request_compute_units(fixture.model, request)
        compute_quantity = ledger.quantize(
            prefix + ":compute:quantize",
            "request compute",
            compute_raw,
            fixture.accuracy.compute_scale,
        )
        memory_raw = request_kv_memory_mib(fixture.model, request)
        memory_quantity = ledger.quantize(
            prefix + ":memory:quantize",
            "request kv/activation memory",
            memory_raw,
            fixture.accuracy.memory_scale,
        )
        compute_quantities.append(compute_quantity)
        memory_quantities.append(memory_quantity)
        observations.append(
            {
                **_request_observation(fixture, request),
                "compute_fixed": compute_quantity.fixed_value,
                "compute_scale": compute_quantity.scale,
                "compute_rounding": _rounding_direction(compute_quantity),
                "compute_error_upper_bound": decimal_string(
                    compute_quantity.error_upper_bound
                ),
                "compute_source_chain": list(compute_quantity.source_chain),
                "memory_fixed": memory_quantity.fixed_value,
                "memory_scale": memory_quantity.scale,
                "memory_rounding": _rounding_direction(memory_quantity),
                "memory_error_upper_bound": decimal_string(
                    memory_quantity.error_upper_bound
                ),
                "memory_source_chain": list(memory_quantity.source_chain),
            }
        )

    prefix = _batch_prefix(replica.replica_id, window_index, version, batch_index)
    compute_total = ledger.add(
        prefix + ":compute:add",
        "batch compute total",
        compute_quantities,
        fixture.accuracy.compute_scale,
    )
    base_memory = ledger.quantize(
        prefix + ":memory:base:quantize",
        "model batch base memory",
        fixture.model.memory_base_mib,
        fixture.accuracy.memory_scale,
    )
    memory_total = ledger.add(
        prefix + ":memory:add",
        "batch memory total",
        (base_memory, *memory_quantities),
        fixture.accuracy.memory_scale,
    )
    duration_compute = ledger.multiply_constant(
        prefix + ":duration:compute:mul",
        "compute-to-duration",
        compute_total,
        1000.0 / replica.compute_capacity,
        fixture.accuracy.latency_scale,
    )
    duration_overhead = ledger.quantize(
        prefix + ":duration:overhead:quantize",
        "batch overhead",
        fixture.model.batch_overhead_ms,
        fixture.accuracy.latency_scale,
    )
    duration_total = ledger.add(
        prefix + ":duration:add",
        "batch duration total",
        (duration_compute, duration_overhead),
        fixture.accuracy.latency_scale,
    )
    queue_delay = ledger.quantize(
        prefix + ":queue:quantize",
        "estimated queue delay",
        float(max(0, start_ms - window_end_ms)),
        fixture.accuracy.latency_scale,
    )
    finish_ms = start_ms + fixed_to_ceiling_integer(duration_total)
    error_bound_total = (
        compute_total.error_upper_bound
        + memory_total.error_upper_bound
        + duration_total.error_upper_bound
        + queue_delay.error_upper_bound
    )
    metrics = {
        "compute": compute_total.public_dict(),
        "memory": memory_total.public_dict(),
        "queue_delay": queue_delay.public_dict(),
        "duration": duration_total.public_dict(),
        "start_ms": start_ms,
        "finish_ms": finish_ms,
        "error_bound_total": decimal_string(error_bound_total),
        "error_budget": fixture.accuracy.error_budget,
        "error_budget_exceeded": error_bound_total
        > Decimal(str(fixture.accuracy.error_budget)),
    }
    return (
        metrics,
        observations,
        {
            "compute": compute_total,
            "memory": memory_total,
            "duration": duration_total,
            "queue_delay": queue_delay,
        },
        tuple(ledger.as_dicts()),
    )


def _constraint(
    fixture: Fixture,
    replica: ReplicaSpec,
    requests: tuple[RequestSpec, ...],
    metrics: dict[str, Any],
) -> dict[str, Any] | None:
    if len(requests) > replica.max_batch_size:
        return {
            "constraint": "max_batch_size",
            "observed": len(requests),
            "limit": replica.max_batch_size,
        }
    compute = metrics["compute"]
    compute_limit_fixed = _fixed_limit(
        replica.compute_capacity, fixture.accuracy.compute_scale
    )
    if (
        compute["raw_float"] > replica.compute_capacity
        or compute["fixed_value"] > compute_limit_fixed
    ):
        return {
            "constraint": "compute_budget",
            "observed": compute["raw_float"],
            "observed_fixed": compute["fixed_value"],
            "limit": replica.compute_capacity,
            "limit_fixed": compute_limit_fixed,
            "scale": compute["scale"],
        }
    memory = metrics["memory"]
    memory_limit_fixed = _fixed_limit(
        replica.memory_limit_mib, fixture.accuracy.memory_scale
    )
    if (
        memory["raw_float"] > replica.memory_limit_mib
        or memory["fixed_value"] > memory_limit_fixed
    ):
        return {
            "constraint": "memory_budget",
            "observed": memory["raw_float"],
            "observed_fixed": memory["fixed_value"],
            "limit": replica.memory_limit_mib,
            "limit_fixed": memory_limit_fixed,
            "scale": memory["scale"],
        }
    finish_ms = metrics["finish_ms"]
    earliest_deadline = min(request.deadline_ms for request in requests)
    if finish_ms > earliest_deadline:
        deadline_request = min(
            requests, key=lambda request: (request.deadline_ms, request.request_id)
        )
        return {
            "constraint": "deadline",
            "request_id": deadline_request.request_id,
            "observed": finish_ms,
            "limit": earliest_deadline,
        }
    return None


def _boundary_event(
    fixture: Fixture,
    replica: ReplicaSpec,
    batch_prefix: str,
    metrics: dict[str, Any],
    quantities: dict[str, Quantity],
    accepted_requests: tuple[RequestSpec, ...],
    violation: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    tolerance = fixture.accuracy.float_tolerance
    compute_gap = abs(metrics["compute"]["raw_float"] - replica.compute_capacity)
    memory_gap = abs(metrics["memory"]["raw_float"] - replica.memory_limit_mib)
    compute_limit_fixed = _fixed_limit(
        replica.compute_capacity, fixture.accuracy.compute_scale
    )
    memory_limit_fixed = _fixed_limit(
        replica.memory_limit_mib, fixture.accuracy.memory_scale
    )
    compute_fixed_gap = float(
        Decimal(abs(metrics["compute"]["fixed_value"] - compute_limit_fixed))
        / Decimal(str(fixture.accuracy.compute_scale))
    )
    memory_fixed_gap = float(
        Decimal(abs(metrics["memory"]["fixed_value"] - memory_limit_fixed))
        / Decimal(str(fixture.accuracy.memory_scale))
    )
    if compute_gap < tolerance or compute_fixed_gap < tolerance:
        events.append(
            {
                "event_id": batch_prefix + ":precision:compute-boundary",
                "kind": "batch_budget_boundary",
                "metric": "compute",
                "raw_difference": compute_gap,
                "float_tolerance": tolerance,
                "observed": metrics["compute"]["raw_float"],
                "limit": replica.compute_capacity,
                "observed_fixed": metrics["compute"]["fixed_value"],
                "limit_fixed": compute_limit_fixed,
                "fixed_difference_normalized": compute_fixed_gap,
                "scale": metrics["compute"]["scale"],
                "source_chain": list(quantities["compute"].source_chain),
            }
        )
    if memory_gap < tolerance or memory_fixed_gap < tolerance:
        events.append(
            {
                "event_id": batch_prefix + ":precision:memory-boundary",
                "kind": "batch_budget_boundary",
                "metric": "memory",
                "raw_difference": memory_gap,
                "float_tolerance": tolerance,
                "observed": metrics["memory"]["raw_float"],
                "limit": replica.memory_limit_mib,
                "observed_fixed": metrics["memory"]["fixed_value"],
                "limit_fixed": memory_limit_fixed,
                "fixed_difference_normalized": memory_fixed_gap,
                "scale": metrics["memory"]["scale"],
                "source_chain": list(quantities["memory"].source_chain),
            }
        )
    if accepted_requests:
        earliest = min(accepted_requests, key=lambda req: (req.deadline_ms, req.request_id))
        deadline_gap = abs(metrics["finish_ms"] - earliest.deadline_ms)
        if deadline_gap < tolerance:
            events.append(
                {
                    "event_id": batch_prefix + ":precision:deadline-boundary",
                    "kind": "batch_deadline_boundary",
                    "metric": "deadline",
                    "raw_difference": float(deadline_gap),
                    "float_tolerance": tolerance,
                    "observed": metrics["finish_ms"],
                    "limit": earliest.deadline_ms,
                    "request_id": earliest.request_id,
                    "source_chain": list(quantities["duration"].source_chain),
                }
            )
    if violation is not None and violation["constraint"] in (
        "compute_budget",
        "memory_budget",
    ):
        observed = float(violation["observed"])
        limit = float(violation["limit"])
        gap = abs(observed - limit)
        if gap < tolerance:
            metric = (
                "compute"
                if violation["constraint"] == "compute_budget"
                else "memory"
            )
            quantity = quantities[metric]
            event_id = batch_prefix + ":precision:" + metric + "-violation-boundary"
            if not any(event["event_id"] == event_id for event in events):
                events.append(
                    {
                        "event_id": event_id,
                        "kind": "batch_budget_boundary",
                        "metric": metric,
                        "decision": "request_skipped",
                        "raw_difference": gap,
                        "float_tolerance": tolerance,
                        "observed": observed,
                        "limit": limit,
                        "observed_fixed": violation.get("observed_fixed"),
                        "limit_fixed": violation.get("limit_fixed"),
                        "scale": violation.get("scale"),
                        "source_chain": list(quantity.source_chain),
                    }
                )
    return events


def _make_batch(
    fixture: Fixture,
    replica: ReplicaSpec,
    request_list: tuple[RequestSpec, ...],
    *,
    window_index: int,
    batch_index: int,
    version: int,
    start_ms: int,
    window_end_ms: int,
    ordered_ids: list[int],
    skipped: list[dict[str, Any]],
    seed_request_id: int,
) -> BatchPlan:
    metrics, observations, quantities, ledger_entries = _estimate_batch(
        fixture,
        replica,
        request_list,
        window_index=window_index,
        batch_index=batch_index,
        version=version,
        start_ms=start_ms,
        window_end_ms=window_end_ms,
    )
    prefix = _batch_prefix(replica.replica_id, window_index, version, batch_index)
    events = _boundary_event(
        fixture,
        replica,
        prefix,
        metrics,
        quantities,
        request_list,
        None,
    )
    if metrics["error_budget_exceeded"]:
        chain: list[str] = []
        for key in ("compute", "memory", "duration", "queue_delay"):
            for entry_id in quantities[key].source_chain:
                if entry_id not in chain:
                    chain.append(entry_id)
        events.append(
            {
                "event_id": prefix + ":precision:error-budget-exceeded",
                "kind": "error_budget_exceeded",
                "metric": "total",
                "observed": metrics["error_bound_total"],
                "limit": fixture.accuracy.error_budget,
                "source_chain": chain,
            }
        )
    decision = {
        "constraint_order": list(CONSTRAINT_ORDER),
        "ordered_request_ids": ordered_ids,
        "accepted_request_ids": [request.request_id for request in request_list],
        "seed_request_id": seed_request_id,
        "skipped": skipped,
        "observations": observations,
        "scheduling_basis": "priority_then_arrival_then_request_id",
        "priority_direction": "ascending_number_is_higher_priority",
    }
    error_spectrum = ledger_entries
    skeleton = {
        "batch_key": (
            f"{replica.replica_id}:w{window_index:08d}:"
            f"v{version}:b{batch_index:08d}"
        ),
        "batch_index": batch_index,
        "replica_id": replica.replica_id,
        "window_index": window_index,
        "version": version,
        "request_ids": [request.request_id for request in request_list],
        "start_ms": start_ms,
        "finish_ms": metrics["finish_ms"],
        "metrics": metrics,
        "decision": decision,
        "error_spectrum": list(error_spectrum),
        "precision_events": events,
        "precision_sensitive": bool(events),
    }
    plan_hash = content_hash(skeleton)
    return BatchPlan(
        replica_id=replica.replica_id,
        window_index=window_index,
        version=version,
        batch_index=batch_index,
        request_ids=tuple(request.request_id for request in request_list),
        start_ms=start_ms,
        finish_ms=metrics["finish_ms"],
        metrics=metrics,
        decision=decision,
        error_spectrum=error_spectrum,
        precision_events=tuple(events),
        precision_sensitive=bool(events),
        plan_hash=plan_hash,
    )


def _trial_snapshot(
    fixture: Fixture,
    replica: ReplicaSpec,
    requests: tuple[RequestSpec, ...],
    metrics: dict[str, Any],
    observations: list[dict[str, Any]],
    error_spectrum: tuple[dict[str, Any], ...],
    violation: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "request_id": requests[-1].request_id,
        "request_ids": [request.request_id for request in requests],
        "violated_constraint": (
            violation["constraint"] if violation is not None else None
        ),
        "violation": violation,
        "trial_metrics": {
            "compute_raw": metrics["compute"]["raw_float"],
            "compute_fixed": metrics["compute"]["fixed_value"],
            "memory_raw": metrics["memory"]["raw_float"],
            "memory_fixed": metrics["memory"]["fixed_value"],
            "finish_ms": metrics["finish_ms"],
        },
        "observations": observations,
        "error_spectrum": list(error_spectrum),
    }


def plan_window(
    fixture: Fixture,
    replica: ReplicaSpec,
    window_index: int,
    requests: Iterable[RequestSpec],
    routes: dict[int, Route],
    initial_available_ms: int,
) -> WindowPlan:
    request_list = tuple(
        sorted(
            requests,
            key=lambda request: (
                request.priority,
                request.arrival_ms,
                request.request_id,
            ),
        )
    )
    window_start = window_index * replica.window_ms
    window_end = window_start + replica.window_ms
    cursor = max(window_end, initial_available_ms)
    remaining = list(request_list)
    batches: list[BatchPlan] = []
    expired: list[dict[str, Any]] = []
    batch_index = 0
    while remaining:
        before = len(remaining)
        still: list[RequestSpec] = []
        for request in remaining:
            single_metrics, _, _, _ = _estimate_batch(
                fixture,
                replica,
                (request,),
                window_index=window_index,
                batch_index=batch_index,
                version=STATE_VERSION,
                start_ms=cursor,
                window_end_ms=window_end,
            )
            violation = _constraint(fixture, replica, (request,), single_metrics)
            if request.deadline_ms < cursor:
                expired.append(
                    {
                        "request_id": request.request_id,
                        "reason": "deadline_before_start",
                        "cursor_ms": cursor,
                        "deadline_ms": request.deadline_ms,
                        "observation": _request_observation(fixture, request),
                    }
                )
            elif violation is not None and violation["constraint"] == "deadline":
                expired.append(
                    {
                        "request_id": request.request_id,
                        "reason": "deadline_unreachable_single_request",
                        "cursor_ms": cursor,
                        "deadline_ms": request.deadline_ms,
                        "single_request_finish_ms": single_metrics["finish_ms"],
                        "observation": _request_observation(fixture, request),
                    }
                )
            elif violation is not None:
                expired.append(
                    {
                        "request_id": request.request_id,
                        "reason": "single_request_resource_violation",
                        "cursor_ms": cursor,
                        "violation": violation,
                        "observation": _request_observation(fixture, request),
                    }
                )
            else:
                still.append(request)
        remaining = still
        if not remaining:
            break

        accepted: list[RequestSpec] = []
        skipped: list[dict[str, Any]] = []
        seed_id: int | None = None
        for request in remaining:
            trial = tuple(accepted + [request])
            if len(trial) > replica.max_batch_size:
                violation = {
                    "constraint": "max_batch_size",
                    "observed": len(trial),
                    "limit": replica.max_batch_size,
                }
                skipped.append(
                    {
                        "request_id": request.request_id,
                        "priority": request.priority,
                        "first_constraint": "max_batch_size",
                        "violation": violation,
                        "observation": _request_observation(fixture, request),
                    }
                )
                continue
            trial_metrics, trial_observations, _, trial_error_spectrum = _estimate_batch(
                fixture,
                replica,
                trial,
                window_index=window_index,
                batch_index=batch_index,
                version=STATE_VERSION,
                start_ms=cursor,
                window_end_ms=window_end,
            )
            violation = _constraint(fixture, replica, trial, trial_metrics)
            if violation is None:
                if seed_id is None:
                    seed_id = request.request_id
                accepted.append(request)
            else:
                skipped.append(
                    {
                        "request_id": request.request_id,
                        "priority": request.priority,
                        "first_constraint": violation["constraint"],
                        "violation": violation,
                        "trial": _trial_snapshot(
                            fixture,
                            replica,
                            trial,
                            trial_metrics,
                            trial_observations,
                            trial_error_spectrum,
                            violation,
                        ),
                        "observation": _request_observation(fixture, request),
                    }
                )
        if not accepted:
            # Defensive fallback: the pre-pass guarantees a single request fits,
            # but an explicit expiry is safer than an infinite scheduling loop.
            request = remaining[0]
            expired.append(
                {
                    "request_id": request.request_id,
                    "reason": "no_admissible_batch",
                    "cursor_ms": cursor,
                    "observation": _request_observation(fixture, request),
                }
            )
            remaining = remaining[1:]
            continue
        assert seed_id is not None
        ordered_ids = [request.request_id for request in remaining]
        batch = _make_batch(
            fixture,
            replica,
            tuple(accepted),
            window_index=window_index,
            batch_index=batch_index,
            version=STATE_VERSION,
            start_ms=cursor,
            window_end_ms=window_end,
            ordered_ids=ordered_ids,
            skipped=skipped,
            seed_request_id=seed_id,
        )
        # Enrich skipped records containing trial metrics with boundary events from
        # the exact trial ledger. This preserves fixed-point precision evidence for
        # decisions that did not become committed batches.
        enriched_skipped: list[dict[str, Any]] = []
        for item in skipped:
            if "trial" not in item or item["first_constraint"] not in (
                "compute_budget",
                "memory_budget",
                "deadline",
            ):
                enriched_skipped.append(item)
                continue
            # Recover request objects by id from the full window input.
            by_id = {request.request_id: request for request in request_list}
            trial_requests = tuple(
                by_id[request_id] for request_id in item["trial"]["request_ids"]
            )
            trial_metrics, _, trial_quantities, _ = _estimate_batch(
                fixture,
                replica,
                trial_requests,
                window_index=window_index,
                batch_index=batch_index,
                version=STATE_VERSION,
                start_ms=cursor,
                window_end_ms=window_end,
            )
            trial_prefix = _batch_prefix(
                replica.replica_id, window_index, STATE_VERSION, batch_index
            ) + f":skip:{request_id_text(item['request_id'])}"
            item = dict(item)
            item["precision_events"] = _boundary_event(
                fixture,
                replica,
                trial_prefix,
                trial_metrics,
                trial_quantities,
                trial_requests,
                item["violation"],
            )
            enriched_skipped.append(item)
        skipped_precision_events: list[dict[str, Any]] = []
        for item in enriched_skipped:
            skipped_precision_events.extend(item.get("precision_events", []))
        merged_events: list[dict[str, Any]] = []
        seen_event_ids: set[str] = set()
        for event in [*batch.precision_events, *skipped_precision_events]:
            event_id = str(event.get("event_id"))
            if event_id in seen_event_ids:
                continue
            seen_event_ids.add(event_id)
            merged_events.append(event)
        if enriched_skipped != skipped or merged_events != list(batch.precision_events):
            provisional = BatchPlan(
                replica_id=batch.replica_id,
                window_index=batch.window_index,
                version=batch.version,
                batch_index=batch.batch_index,
                request_ids=batch.request_ids,
                start_ms=batch.start_ms,
                finish_ms=batch.finish_ms,
                metrics=batch.metrics,
                decision={**batch.decision, "skipped": enriched_skipped},
                error_spectrum=batch.error_spectrum,
                precision_events=tuple(merged_events),
                precision_sensitive=bool(merged_events),
                plan_hash="",
            )
            skeleton = provisional.to_dict(include_hash=False)
            batch = BatchPlan(
                replica_id=provisional.replica_id,
                window_index=provisional.window_index,
                version=provisional.version,
                batch_index=provisional.batch_index,
                request_ids=provisional.request_ids,
                start_ms=provisional.start_ms,
                finish_ms=provisional.finish_ms,
                metrics=provisional.metrics,
                decision=provisional.decision,
                error_spectrum=provisional.error_spectrum,
                precision_events=provisional.precision_events,
                precision_sensitive=provisional.precision_sensitive,
                plan_hash=content_hash(skeleton),
            )
        accepted_ids = {request.request_id for request in accepted}
        remaining = [
            request
            for request in remaining
            if request.request_id not in accepted_ids
        ]
        batches.append(batch)
        cursor = batch.finish_ms
        batch_index += 1

    routing_events: list[dict[str, Any]] = []
    for request in request_list:
        route = routes[request.request_id]
        routing_events.extend(dict(event) for event in route.precision_events)
    all_batch_events: list[dict[str, Any]] = []
    seen_event_ids: set[str] = set()
    for event in [
        *routing_events,
        *(event for batch in batches for event in batch.precision_events),
    ]:
        event_id = str(event.get("event_id"))
        if event_id in seen_event_ids:
            continue
        seen_event_ids.add(event_id)
        all_batch_events.append(event)
    precision_events = tuple(all_batch_events)
    decision = {
        "constraint_order": list(CONSTRAINT_ORDER),
        "scheduling_basis": "priority_then_arrival_then_request_id",
        "priority_direction": "ascending_number_is_higher_priority",
        "batch_count": len(batches),
        "expired": list(expired),
        "routing_precision": routing_events,
        "window_closed_by": "logical_window_boundary",
        "initial_available_ms": initial_available_ms,
        "final_available_ms": cursor,
    }
    skeleton = {
        "replica_id": replica.replica_id,
        "window_index": window_index,
        "version": STATE_VERSION,
        "start_ms": window_start,
        "end_ms": window_end,
        "request_ids": sorted(request.request_id for request in request_list),
        "batches": [batch.to_dict() for batch in batches],
        "expired": list(expired),
        "decision": decision,
        "precision_events": list(precision_events),
        "precision_sensitive": bool(precision_events),
    }
    return WindowPlan(
        replica_id=replica.replica_id,
        window_index=window_index,
        version=STATE_VERSION,
        start_ms=window_start,
        end_ms=window_end,
        request_ids=tuple(sorted(request.request_id for request in request_list)),
        batches=tuple(batches),
        expired=tuple(expired),
        decision=decision,
        precision_events=precision_events,
        precision_sensitive=bool(precision_events),
        plan_hash=content_hash(skeleton),
    )


def request_id_text(request_id: int) -> str:
    return f"{request_id:08d}"
