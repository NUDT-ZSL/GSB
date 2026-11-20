from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Iterable

from .errors import fail
from .fslock import stable_json
from .precision import BatchEstimate, estimate_batch
from .validation import (
    ModelSpec,
    PrecisionConfig,
    ReplicaSpec,
    RequestSpec,
)


CONSTRAINT_ORDER = (
    "MAX_BATCH_SIZE",
    "SEQUENCE_LENGTH",
    "COMPUTE_BUDGET",
    "MEMORY_BUDGET",
)


def make_window_key(replica_id: str, version: int, start_ms: int) -> str:
    return f"{replica_id}:v{version}:w{start_ms:012d}"


def make_batch_key(window_key: str, ordinal: int) -> str:
    return f"{window_key}#b{ordinal:04d}"


def plan_hash(plan: dict[str, Any]) -> str:
    return hashlib.sha256(stable_json(plan).encode("utf-8")).hexdigest()


def _compact_quantity(quantity: Any) -> dict[str, Any]:
    value = quantity.to_dict()
    return {
        key: value[key]
        for key in (
            "raw",
            "fixed",
            "scale",
            "rounding_direction",
            "rounding_error",
            "accumulated_error_bound",
            "within_error_budget",
        )
    }


@dataclass
class ReplicaChoice:
    replica: ReplicaSpec
    window_start_ms: int
    window_end_ms: int
    probe: BatchEstimate
    flag: dict[str, Any] | None


@dataclass
class PlannedWindow:
    plan: dict[str, Any]
    flags: list[dict[str, Any]]


def _probe_flag(
    request: RequestSpec,
    estimates: list[tuple[ReplicaSpec, BatchEstimate]],
    chosen: ReplicaSpec,
    scores: dict[str, float],
    precision: PrecisionConfig,
    window_starts: dict[str, tuple[int, int]],
) -> dict[str, Any] | None:
    if len(estimates) < 2:
        return None
    ordered = sorted(
        estimates,
        key=lambda item: (scores[item[0].replica_id], item[0].replica_id),
    )
    first, second = ordered[0], ordered[1]
    difference = abs(
        scores[first[0].replica_id] - scores[second[0].replica_id]
    )
    if difference >= precision.float_tolerance:
        return None
    return {
        "scope": "replica_choice",
        "scope_key": f"v?::req:{request.request_seq}",
        "metric": "projected_queue_ms",
        "difference": difference,
        "tolerance": precision.float_tolerance,
        "chosen": chosen.replica_id,
        "replica_id": chosen.replica_id,
        "error_chain": {
            "reason": (
                "two candidate replica estimates are within float_tolerance; "
                "selection depends on the difference and uses stable replica_id "
                "as the final tie-break"
            ),
            "request_seq": request.request_seq,
            "request_id": request.request_id,
            "candidates": [
                {
                    "replica_id": replica.replica_id,
                    "projected_queue_ms": scores[replica.replica_id],
                    "window_start_ms": window_starts[replica.replica_id][0],
                    "window_end_ms": window_starts[replica.replica_id][1],
                    "estimate": estimate.comparison_dict(),
                }
                for replica, estimate in ordered
            ],
        },
    }


def choose_replica(
    request: RequestSpec,
    candidates: Iterable[str],
    replicas_by_id: dict[str, ReplicaSpec],
    model: ModelSpec,
    precision: PrecisionConfig,
    next_window_start: dict[str, int],
    projected_queue_ms: dict[str, float],
) -> ReplicaChoice:
    candidate_specs = [
        replicas_by_id[replica_id]
        for replica_id in sorted(set(candidates))
        if replica_id in replicas_by_id
    ]
    if not candidate_specs:
        fail(
            "E_REQ_NO_REPLICA",
            "request has no candidate in the active replica snapshot",
            request_id=request.request_id,
        )

    estimates: list[tuple[ReplicaSpec, BatchEstimate]] = []
    scores: dict[str, float] = {}
    window_starts: dict[str, tuple[int, int]] = {}
    for replica in candidate_specs:
        aligned = (request.arrival_ms // replica.batch_window_ms) * replica.batch_window_ms
        start = max(aligned, next_window_start.get(replica.replica_id, 0))
        end = start + replica.batch_window_ms
        probe = estimate_batch(
            [request],
            model,
            replica,
            precision,
            window_start_ms=start,
            window_end_ms=end,
            decision_clock_ms=request.arrival_ms,
        )
        estimates.append((replica, probe))
        scores[replica.replica_id] = (
            projected_queue_ms.get(replica.replica_id, 0.0) + probe.queue_ms.raw
        )
        window_starts[replica.replica_id] = (start, end)

    chosen_replica, chosen_probe = min(
        estimates,
        key=lambda item: (scores[item[0].replica_id], item[0].replica_id),
    )
    flag = _probe_flag(
        request,
        estimates,
        chosen_replica,
        scores,
        precision,
        window_starts,
    )
    if flag is not None:
        flag["scope_key"] = f"v?::req:{request.request_seq}"
    start, end = window_starts[chosen_replica.replica_id]
    return ReplicaChoice(
        replica=chosen_replica,
        window_start_ms=start,
        window_end_ms=end,
        probe=chosen_probe,
        flag=flag,
    )


def _constraint_for(
    candidate: list[RequestSpec],
    estimate: BatchEstimate,
    replica: ReplicaSpec,
) -> str | None:
    if len(candidate) > replica.max_batch_size:
        return "MAX_BATCH_SIZE"
    if any(item.total_tokens > replica.max_sequence_length for item in candidate):
        return "SEQUENCE_LENGTH"
    if estimate.compute.raw > replica.available_compute_units:
        return "COMPUTE_BUDGET"
    if estimate.memory.raw > float(replica.memory_limit_mib):
        return "MEMORY_BUDGET"
    return None


def _priority_insertions(requests: list[RequestSpec]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for index, item in enumerate(requests):
        jumped = [
            other.request_seq
            for other in requests[index + 1 :]
            if other.arrival_ms < item.arrival_ms and other.priority < item.priority
        ]
        if jumped:
            result.append(
                {
                    "request_seq": item.request_seq,
                    "request_id": item.request_id,
                    "priority": item.priority,
                    "arrival_ms": item.arrival_ms,
                    "inserted_ahead_of": jumped,
                }
            )
    return result


def _plan_batches(
    window_key: str,
    requests: list[RequestSpec],
    replica: ReplicaSpec,
    model: ModelSpec,
    precision: PrecisionConfig,
    window_start_ms: int,
    window_end_ms: int,
    decision_clock_ms: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    ordered = sorted(
        requests,
        key=lambda item: (-item.priority, item.arrival_ms, item.request_seq),
    )
    batches: list[dict[str, Any]] = []
    flags: list[dict[str, Any]] = []
    remaining = list(ordered)
    ordinal = 1
    while remaining:
        feasible: list[tuple[list[RequestSpec], BatchEstimate]] = []
        first_rejected: RequestSpec | None = None
        first_constraint: str | None = None
        for index, request in enumerate(remaining):
            candidate = remaining[: index + 1]
            estimate = estimate_batch(
                candidate,
                model,
                replica,
                precision,
                window_start_ms=window_start_ms,
                window_end_ms=window_end_ms,
                decision_clock_ms=decision_clock_ms,
            )
            constraint = _constraint_for(candidate, estimate, replica)
            if constraint is not None:
                first_rejected = request
                first_constraint = constraint
                break
            feasible.append((candidate, estimate))

        if not feasible:
            fail(
                "E_BATCH_COMPOSE",
                "no feasible request could start a batch",
                window_key=window_key,
                request_id=remaining[0].request_id,
            )

        best_index = max(
            range(len(feasible)),
            key=lambda idx: (
                feasible[idx][1].efficiency.raw,
                len(feasible[idx][0]),
            ),
        )
        near_tie = False
        compare_pair: tuple[int, int] | None = None
        if len(feasible) >= 2:
            ranked = sorted(
                range(len(feasible)),
                key=lambda idx: (
                    -feasible[idx][1].efficiency.raw,
                    -len(feasible[idx][0]),
                    tuple(item.request_seq for item in feasible[idx][0]),
                ),
            )
            first_idx, second_idx = ranked[0], ranked[1]
            difference = abs(
                feasible[first_idx][1].efficiency.raw
                - feasible[second_idx][1].efficiency.raw
            )
            if difference < precision.float_tolerance:
                near_tie = True
                compare_pair = (first_idx, second_idx)
                best_index = min(
                    compare_pair,
                    key=lambda idx: (
                        -feasible[idx][1].efficiency.fixed,
                        -len(feasible[idx][0]),
                        tuple(item.request_seq for item in feasible[idx][0]),
                    ),
                )

        selected, selected_estimate = feasible[best_index]
        selected_estimate_dict = selected_estimate.to_dict()
        cutoff_reason = "BUDGET_CONSTRAINT" if first_constraint else "NONE"
        if best_index < len(feasible) - 1:
            cutoff_reason = "ESTIMATE_CUTOFF"

        skipped: list[dict[str, Any]] = []
        deferred = remaining[best_index + 1 :]
        for position, request in enumerate(deferred):
            if (
                first_rejected is not None
                and request.request_seq == first_rejected.request_seq
            ):
                reason = first_constraint or "UNKNOWN"
            elif position == 0:
                reason = "ESTIMATE_CUTOFF"
            else:
                reason = "PRIORITY_HELD_FOR_NEXT_BATCH"
            skipped.append(
                {
                    "request_seq": request.request_seq,
                    "request_id": request.request_id,
                    "priority": request.priority,
                    "reason": reason,
                }
            )

        candidate_summaries = [
            {
                "request_seqs": [item.request_seq for item in candidate],
                "request_ids": [item.request_id for item in candidate],
                "compute": _compact_quantity(estimate.compute),
                "memory": _compact_quantity(estimate.memory),
                "queue_ms": _compact_quantity(estimate.queue_ms),
                "efficiency": _compact_quantity(estimate.efficiency),
            }
            for candidate, estimate in feasible
        ]
        batch_key = make_batch_key(window_key, ordinal)
        decision = {
            "aggregation_order": [item.request_seq for item in selected],
            "aggregation_rule": (
                "candidate prefixes built in priority_desc,arrival_ms_asc,"
                "request_seq_asc order; left-to-right float64 arithmetic; "
                "constraint order MAX_BATCH_SIZE,SEQUENCE_LENGTH,"
                "COMPUTE_BUDGET,MEMORY_BUDGET"
            ),
            "observations": selected_estimate.observations,
            "candidate_prefixes": candidate_summaries,
            "first_constraint": (
                first_constraint if first_rejected is not None else None
            ),
            "first_rejected_request_id": (
                first_rejected.request_id if first_rejected is not None else None
            ),
            "cutoff_reason": cutoff_reason,
            "deferred_requests": skipped,
            "priority_insertions": _priority_insertions(selected),
            "selection_rule": (
                "maximize priority_sum/queue_ms; if efficiencies differ by "
                "less than float_tolerance, use fixed efficiency, batch size, "
                "and request-seq signature as stable tie-breaks"
            ),
        }

        batch = {
            "batch_key": batch_key,
            "ordinal": ordinal,
            "request_seqs": [item.request_seq for item in selected],
            "request_ids": [item.request_id for item in selected],
            "estimate": selected_estimate_dict,
            "decision": decision,
            "precision_sensitive": near_tie,
            "compute_used": selected_estimate.compute.raw,
            "memory_used": selected_estimate.memory.raw,
            "estimated_completion_ms": selected_estimate.estimated_completion_ms,
        }
        batches.append(batch)

        if near_tie and compare_pair is not None:
            a_idx, b_idx = compare_pair
            a_candidate, a_estimate = feasible[a_idx]
            b_candidate, b_estimate = feasible[b_idx]
            difference = abs(
                a_estimate.efficiency.raw - b_estimate.efficiency.raw
            )
            flags.append(
                {
                    "scope": "batch_composition",
                    "scope_key": batch_key,
                    "metric": "priority_per_queue_ms",
                    "difference": difference,
                    "tolerance": precision.float_tolerance,
                    "chosen": ",".join(
                        str(item.request_seq) for item in selected
                    ),
                    "replica_id": replica.replica_id,
                    "error_chain": {
                        "reason": (
                            "candidate batch efficiency estimates are within "
                            "float_tolerance; batch composition depends on the "
                            "difference and uses fixed values plus a stable "
                            "signature tie-break"
                        ),
                        "window_key": window_key,
                        "priority_order": [
                            item.request_seq for item in ordered
                        ],
                        "candidate_a": {
                            "request_seqs": [
                                item.request_seq for item in a_candidate
                            ],
                            "request_ids": [
                                item.request_id for item in a_candidate
                            ],
                            "estimate": a_estimate.comparison_dict(),
                        },
                        "candidate_b": {
                            "request_seqs": [
                                item.request_seq for item in b_candidate
                            ],
                            "request_ids": [
                                item.request_id for item in b_candidate
                            ],
                            "estimate": b_estimate.comparison_dict(),
                        },
                        "selected": [
                            item.request_seq for item in selected
                        ],
                    },
                }
            )

        ordinal += 1
        remaining = remaining[best_index + 1 :] if best_index + 1 < len(remaining) else []

    return batches, flags


def plan_windows(
    pending_requests: list[RequestSpec],
    candidates: dict[int, tuple[str, ...]],
    replicas: list[ReplicaSpec],
    version: int,
    next_window_start: dict[str, int],
    model: ModelSpec,
    precision: PrecisionConfig,
) -> tuple[list[PlannedWindow], dict[str, int]]:
    replicas_by_id = {item.replica_id: item for item in replicas}
    projected_queue_ms = {item.replica_id: 0.0 for item in replicas}
    groups: dict[str, list[RequestSpec]] = {}
    replica_for_window: dict[str, ReplicaSpec] = {}
    flags_for_window: dict[str, list[dict[str, Any]]] = {}
    bounds: dict[str, tuple[int, int]] = {}
    decision_clocks: dict[str, int] = {}
    next_boundary = dict(next_window_start)

    for request in sorted(
        pending_requests, key=lambda item: (item.arrival_ms, item.request_seq)
    ):
        choice = choose_replica(
            request,
            candidates[request.request_seq],
            replicas_by_id,
            model,
            precision,
            next_boundary,
            projected_queue_ms,
        )
        key = make_window_key(
            choice.replica.replica_id, version, choice.window_start_ms
        )
        groups.setdefault(key, []).append(request)
        replica_for_window[key] = choice.replica
        bounds[key] = (choice.window_start_ms, choice.window_end_ms)
        decision_clocks[key] = max(
            decision_clocks.get(key, request.arrival_ms), request.arrival_ms
        )
        projected_queue_ms[choice.replica.replica_id] += choice.probe.queue_ms.raw
        if choice.flag is not None:
            choice.flag["scope_key"] = f"v{version}::req:{request.request_seq}"
            flags_for_window.setdefault(key, []).append(choice.flag)

    planned: list[PlannedWindow] = []
    for key in sorted(
        groups,
        key=lambda item: (
            bounds[item][0],
            replica_for_window[item].replica_id,
        ),
    ):
        replica = replica_for_window[key]
        start, end = bounds[key]
        requests = sorted(
            groups[key],
            key=lambda item: (
                -item.priority,
                item.arrival_ms,
                item.request_seq,
            ),
        )
        decision_clock = decision_clocks[key]
        batches, batch_flags = _plan_batches(
            key,
            requests,
            replica,
            model,
            precision,
            start,
            end,
            decision_clock,
        )
        flags = flags_for_window.get(key, []) + batch_flags
        plan = {
            "window_key": key,
            "replica_id": replica.replica_id,
            "replica_version": version,
            "window_start_ms": start,
            "window_end_ms": end,
            "decision_clock_ms": decision_clock,
            "boundary_source": (
                "floor(arrival_ms/batch_window_ms)*batch_window_ms, "
                "raised to the persisted replica window watermark"
            ),
            "replica_spec": replica.to_dict(),
            "requests": [
                {
                    **request.to_dict(),
                    "candidate_replicas": list(
                        candidates[request.request_seq]
                    ),
                }
                for request in requests
            ],
            "aggregation_order": [item.request_seq for item in requests],
            "batches": batches,
            "precision_flags": flags,
            "dropped_requests": [],
        }
        planned.append(PlannedWindow(plan=plan, flags=flags))
        next_boundary[replica.replica_id] = max(
            next_boundary.get(replica.replica_id, 0), end
        )
    return planned, next_boundary

