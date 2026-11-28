"""Independent reference allocator.

This module deliberately does not import :mod:`moe_capacity.router`.  It
implements its own option construction, capacity check, deterministic
weighted min-max choice, ledger aggregation and imbalance calculation.
"""

from __future__ import annotations

import math
from dataclasses import replace
from typing import Any

from .model import InferenceRequest, NormalizedProject, Replica


def _blocks(sequence_length: int, reference_length: int) -> int:
    return max(1, (sequence_length + reference_length - 1) // reference_length)


def _topology_at_end(project: NormalizedProject) -> tuple[
    dict[str, Replica], dict[str, Any]
]:
    replicas = {replica.replica_id: replica for replica in project.replicas}
    experts = {expert.expert_id: expert for expert in project.experts}
    for event in sorted(project.events, key=lambda item: (item.at, item.event_id)):
        if event.kind == "migrate_expert":
            expert = experts[event.expert_id]
            experts[event.expert_id] = replace(
                expert, home_replica=event.to_replica
            )
        else:
            replicas[event.replica_id] = replace(
                replicas[event.replica_id],
                capacity_bytes=event.new_capacity_bytes,
            )
    return replicas, experts


def _all_options(
    project: NormalizedProject,
    request: InferenceRequest,
    replicas: dict[str, Replica],
    experts: dict[str, Any],
) -> list[dict[str, Any]]:
    reference_length = int(project.normalization["reference_sequence_length"])
    blocks = _blocks(request.sequence_length, reference_length)
    rows: list[dict[str, Any]] = []
    for expert_id in sorted(experts):
        expert = experts[expert_id]
        if request.domain not in expert.domains:
            continue
        home = replicas[expert.home_replica]
        for target_id in sorted(replicas):
            target = replicas[target_id]
            if expert.precision not in target.supported_precisions:
                continue
            compute = (
                request.sample_units
                * blocks
                * expert.per_sample_cost_bytes
            )
            raw = (
                0
                if target_id == expert.home_replica
                else expert.cross_rack_comm_bytes * blocks
            )
            same_rack = home.rack == target.rack
            billed = 0 if same_rack else raw
            rows.append(
                {
                    "billed_communication_bytes": billed,
                    "compute_load_bytes": compute,
                    "cross_replica_raw_bytes": raw,
                    "expert_id": expert_id,
                    "home_replica": expert.home_replica,
                    "precision": expert.precision,
                    "request_id": request.request_id,
                    "same_rack_communication_avoided_bytes": (
                        raw if same_rack else 0
                    ),
                    "same_rack_dispatch": same_rack,
                    "sample_units": request.sample_units,
                    "sequence_blocks": blocks,
                    "target_replica": target_id,
                    "weighted_load_bytes": compute + billed,
                }
            )
    return rows


def _choice_key(
    row: dict[str, Any],
    loads: dict[str, int],
) -> tuple[int, int, int, str, str]:
    projected = dict(loads)
    projected[row["target_replica"]] += row["weighted_load_bytes"]
    return (
        max(projected.values()),
        sum(value * value for value in projected.values()),
        row["billed_communication_bytes"],
        row["target_replica"],
        row["expert_id"],
    )


def _minimum_work(
    project: NormalizedProject,
    request: InferenceRequest,
    replicas: dict[str, Replica],
    experts: dict[str, Any],
) -> int:
    reference_length = int(project.normalization["reference_sequence_length"])
    blocks = _blocks(request.sequence_length, reference_length)
    return min(
        request.sample_units * blocks * expert.per_sample_cost_bytes
        for expert in experts.values()
        if request.domain in expert.domains
    )


def _greedy(
    project: NormalizedProject,
    order: list[InferenceRequest],
    replicas: dict[str, Replica],
    experts: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]] | None, str]:
    loads = {replica_id: 0 for replica_id in replicas}
    result: dict[str, dict[str, Any]] = {}
    for request in order:
        options = _all_options(project, request, replicas, experts)
        usable = [
            row
            for row in options
            if loads[row["target_replica"]] + row["weighted_load_bytes"]
            <= replicas[row["target_replica"]].capacity_bytes
        ]
        if not usable:
            return None, "infeasible"
        chosen = min(
            usable,
            key=lambda row: (
                _choice_key(row, loads),
                row["expert_id"],
                row["target_replica"],
            ),
        )
        result[request.request_id] = dict(chosen)
        loads[chosen["target_replica"]] += chosen["weighted_load_bytes"]
    return result, "feasible"


def _best_global(
    project: NormalizedProject,
    request_list: list[InferenceRequest],
    replicas: dict[str, Replica],
    experts: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    if not request_list:
        return {}
    by_min_work = sorted(
        request_list,
        key=lambda request: (
            -_minimum_work(project, request, replicas, experts),
            request.request_id,
        ),
    )
    by_max_work = sorted(
        request_list,
        key=lambda request: (
            -max(
                row["compute_load_bytes"]
                for row in _all_options(project, request, replicas, experts)
            ),
            request.request_id,
        ),
    )
    by_arrival = sorted(
        request_list,
        key=lambda request: (request.arrival_time, request.request_id),
    )
    by_samples = sorted(
        request_list,
        key=lambda request: (-request.sample_units, request.request_id),
    )
    feasible_results: list[
        tuple[
            tuple[int, int, int, tuple[tuple[str, str, str], ...]],
            dict[str, dict[str, Any]],
        ]
    ] = []
    for order in (by_min_work, by_max_work, by_arrival, by_samples):
        result, status = _greedy(project, order, replicas, experts)
        if status == "feasible" and result is not None:
            loads = {replica_id: 0 for replica_id in replicas}
            for row in result.values():
                loads[row["target_replica"]] += row["weighted_load_bytes"]
            key = (
                max(loads.values()),
                sum(value * value for value in loads.values()),
                sum(
                    row["billed_communication_bytes"]
                    for row in result.values()
                ),
                tuple(
                    sorted(
                        (
                            request_id,
                            row["expert_id"],
                            row["target_replica"],
                        )
                        for request_id, row in result.items()
                    )
                ),
            )
            feasible_results.append((key, result))
    if feasible_results:
        _, result = min(feasible_results, key=lambda item: item[0])
        return result
    return _overflow(
        project,
        sorted(request_list, key=lambda item: item.request_id),
        replicas,
        experts,
    )


def _overflow(
    project: NormalizedProject,
    order: list[InferenceRequest],
    replicas: dict[str, Replica],
    experts: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    loads = {replica_id: 0 for replica_id in replicas}
    result: dict[str, dict[str, Any]] = {}
    for request in order:
        options = _all_options(project, request, replicas, experts)
        if not options:
            raise ValueError(
                f"request {request.request_id}: no reference route available"
            )

        def key(row: dict[str, Any]) -> tuple[Any, ...]:
            projected = dict(loads)
            projected[row["target_replica"]] += row["weighted_load_bytes"]
            overflow = 0
            for replica_id, value in projected.items():
                overflow += max(
                    0, value - replicas[replica_id].capacity_bytes
                )
            return (
                overflow,
                max(projected.values()),
                sum(value * value for value in projected.values()),
                row["billed_communication_bytes"],
                row["target_replica"],
                row["expert_id"],
            )

        chosen = min(options, key=key)
        result[request.request_id] = dict(chosen)
        loads[chosen["target_replica"]] += chosen["weighted_load_bytes"]
    return result


def _allocate(
    project: NormalizedProject,
) -> tuple[dict[str, dict[str, Any]], dict[str, Replica], dict[str, Any]]:
    replicas, experts = _topology_at_end(project)
    return (
        _best_global(project, list(project.requests), replicas, experts),
        replicas,
        experts,
    )


def _apply_ref_event(
    event: Any,
    replicas: dict[str, Replica],
    experts: dict[str, Any],
) -> tuple[dict[str, Replica], dict[str, Any]]:
    next_replicas = dict(replicas)
    next_experts = dict(experts)
    if event.kind == "migrate_expert":
        next_experts[event.expert_id] = replace(
            next_experts[event.expert_id], home_replica=event.to_replica
        )
    else:
        next_replicas[event.replica_id] = replace(
            next_replicas[event.replica_id],
            capacity_bytes=event.new_capacity_bytes,
        )
    return next_replicas, next_experts


def _reroute_event(
    project: NormalizedProject,
    event: Any,
    assignments: dict[str, dict[str, Any]],
    replicas: dict[str, Replica],
    experts: dict[str, Any],
    arrived: dict[str, InferenceRequest],
) -> tuple[dict[str, Replica], dict[str, Any], dict[str, dict[str, Any]]]:
    next_replicas, next_experts = _apply_ref_event(event, replicas, experts)
    if event.kind == "migrate_expert":
        affected = sorted(
            request_id
            for request_id, row in assignments.items()
            if row["expert_id"] == event.expert_id
        )
    else:
        affected = sorted(
            request_id
            for request_id, row in assignments.items()
            if row["target_replica"] == event.replica_id
        )
    loads = {replica_id: 0 for replica_id in next_replicas}
    for row in assignments.values():
        loads[row["target_replica"]] += row["weighted_load_bytes"]
    for request_id in affected:
        row = assignments.pop(request_id)
        loads[row["target_replica"]] -= row["weighted_load_bytes"]
    failed = False
    for request in sorted(
        (arrived[request_id] for request_id in affected),
        key=lambda item: (
            -_minimum_work(project, item, next_replicas, next_experts),
            item.request_id,
        ),
    ):
        options = _all_options(project, request, next_replicas, next_experts)
        usable = [
            row
            for row in options
            if loads[row["target_replica"]] + row["weighted_load_bytes"]
            <= next_replicas[row["target_replica"]].capacity_bytes
        ]
        if not usable:
            failed = True
            break
        chosen = min(
            usable,
            key=lambda row: (
                _choice_key(row, loads),
                row["expert_id"],
                row["target_replica"],
            ),
        )
        assignments[request.request_id] = dict(chosen)
        loads[chosen["target_replica"]] += chosen["weighted_load_bytes"]
    if failed:
        assigned_ids = set(assignments)
        requested = [
            request
            for request in project.requests
            if request.request_id in assigned_ids
            or request.request_id in affected
        ]
        assignments = _best_global(
            project, requested, next_replicas, next_experts
        )
    return next_replicas, next_experts, assignments


def _before_rebalance_state(
    project: NormalizedProject,
) -> tuple[
    dict[str, dict[str, Any]], dict[str, Replica], dict[str, Any]
]:
    replicas = {replica.replica_id: replica for replica in project.replicas}
    experts = {expert.expert_id: expert for expert in project.experts}
    events = sorted(project.events, key=lambda item: (item.at, item.event_id))
    requests = sorted(
        project.requests,
        key=lambda item: (item.arrival_time, item.request_id),
    )
    assignments: dict[str, dict[str, Any]] = {}
    arrived: dict[str, InferenceRequest] = {}
    event_index = 0

    def consume_events(until: int) -> None:
        nonlocal replicas, experts, assignments, event_index
        while event_index < len(events) and events[event_index].at <= until:
            replicas, experts, assignments = _reroute_event(
                project,
                events[event_index],
                assignments,
                replicas,
                experts,
                arrived,
            )
            event_index += 1

    for request in requests:
        consume_events(request.arrival_time)
        arrived[request.request_id] = request
        loads = {replica_id: 0 for replica_id in replicas}
        for row in assignments.values():
            loads[row["target_replica"]] += row["weighted_load_bytes"]
        options = _all_options(project, request, replicas, experts)
        usable = [
            row
            for row in options
            if loads[row["target_replica"]] + row["weighted_load_bytes"]
            <= replicas[row["target_replica"]].capacity_bytes
        ]
        if not usable:
            assignments = _best_global(
                project, list(arrived.values()), replicas, experts
            )
            if request.request_id not in assignments:
                raise AssertionError(
                    "reference event replay failed to place an arrived request"
                )
            continue
        chosen = min(
            usable,
            key=lambda row: (
                _choice_key(row, loads),
                row["expert_id"],
                row["target_replica"],
            ),
        )
        assignments[request.request_id] = dict(chosen)
    while event_index < len(events):
        replicas, experts, assignments = _reroute_event(
            project,
            events[event_index],
            assignments,
            replicas,
            experts,
            arrived,
        )
        event_index += 1
    return assignments, replicas, experts


def _assignment_key(
    assignments: dict[str, dict[str, Any]],
    replicas: dict[str, Replica],
) -> tuple[int, int, int, tuple[tuple[str, str, str], ...]]:
    loads = {replica_id: 0 for replica_id in replicas}
    for row in assignments.values():
        loads[row["target_replica"]] += row["weighted_load_bytes"]
    return (
        max(loads.values(), default=0),
        sum(value * value for value in loads.values()),
        sum(row["billed_communication_bytes"] for row in assignments.values()),
        tuple(
            sorted(
                (
                    request_id,
                    row["expert_id"],
                    row["target_replica"],
                )
                for request_id, row in assignments.items()
            )
        ),
    )


def _reference_ledger(
    project: NormalizedProject,
    assignments: dict[str, dict[str, Any]],
    replicas: dict[str, Replica],
) -> dict[str, Any]:
    requests = {request.request_id: request for request in project.requests}
    grouped: dict[str, dict[str, dict[str, Any]]] = {
        replica_id: {} for replica_id in replicas
    }
    for request_id in sorted(
        assignments,
        key=lambda rid: (requests[rid].arrival_time, rid),
    ):
        row = assignments[request_id]
        expert = grouped[row["target_replica"]].setdefault(
            row["expert_id"],
            {
                "billed_communication_bytes": 0,
                "compute_load_bytes": 0,
                "expert_id": row["expert_id"],
                "request_count": 0,
                "request_ids": [],
                "same_rack_communication_avoided_bytes": 0,
                "sample_units": 0,
                "weighted_load_bytes": 0,
            },
        )
        expert["billed_communication_bytes"] += row[
            "billed_communication_bytes"
        ]
        expert["compute_load_bytes"] += row["compute_load_bytes"]
        expert["request_count"] += 1
        expert["request_ids"].append(request_id)
        expert["same_rack_communication_avoided_bytes"] += row[
            "same_rack_communication_avoided_bytes"
        ]
        expert["sample_units"] += row["sample_units"]
        expert["weighted_load_bytes"] += row["weighted_load_bytes"]

    ledger_rows = []
    sum_compute = 0
    sum_billed = 0
    sum_avoided = 0
    sum_weighted = 0
    for replica_id in sorted(replicas):
        replica = replicas[replica_id]
        expert_rows = [
            grouped[replica_id][expert_id]
            for expert_id in sorted(grouped[replica_id])
        ]
        compute = sum(item["compute_load_bytes"] for item in expert_rows)
        billed = sum(
            item["billed_communication_bytes"] for item in expert_rows
        )
        avoided = sum(
            item["same_rack_communication_avoided_bytes"]
            for item in expert_rows
        )
        weighted = sum(item["weighted_load_bytes"] for item in expert_rows)
        sum_compute += compute
        sum_billed += billed
        sum_avoided += avoided
        sum_weighted += weighted
        ledger_rows.append(
            {
                "billed_communication_bytes": billed,
                "capacity_bytes": replica.capacity_bytes,
                "compute_load_bytes": compute,
                "experts": expert_rows,
                "rack": replica.rack,
                "remaining_capacity_bytes": replica.capacity_bytes - weighted,
                "replica_id": replica_id,
                "same_rack_communication_avoided_bytes": avoided,
                "supported_precisions": list(replica.supported_precisions),
                "utilization": weighted / replica.capacity_bytes,
                "weighted_load_bytes": weighted,
            }
        )
    return {
        "replicas": ledger_rows,
        "total_billed_communication_bytes": sum_billed,
        "total_compute_load_bytes": sum_compute,
        "total_same_rack_communication_avoided_bytes": sum_avoided,
        "total_weighted_load_bytes": sum_weighted,
        "unit": "bytes",
    }


def _reference_metrics(ledger: dict[str, Any]) -> dict[str, Any]:
    rows = ledger["replicas"]
    weighted = [row["weighted_load_bytes"] for row in rows]
    compute = [row["compute_load_bytes"] for row in rows]
    communication = [row["billed_communication_bytes"] for row in rows]

    def cv(values: list[int]) -> float:
        if not values:
            return 0.0
        mean = sum(values) / len(values)
        if mean == 0:
            return 0.0
        return math.sqrt(
            sum((value - mean) ** 2 for value in values) / len(values)
        ) / mean

    wmean = sum(weighted) / len(weighted)
    cmean = sum(compute) / len(compute)
    comm_mean = sum(communication) / len(communication)
    peak_rows = [
        {
            "capacity_bytes": row["capacity_bytes"],
            "rack": row["rack"],
            "replica_id": row["replica_id"],
            "utilization": row["utilization"],
            "weighted_load_bytes": row["weighted_load_bytes"],
        }
        for row in rows
        if row["utilization"] >= 0.9
    ]
    if not peak_rows:
        row = max(rows, key=lambda item: (item["utilization"], item["replica_id"]))
        peak_rows = [
            {
                "capacity_bytes": row["capacity_bytes"],
                "rack": row["rack"],
                "replica_id": row["replica_id"],
                "utilization": row["utilization"],
                "weighted_load_bytes": row["weighted_load_bytes"],
            }
        ]
    maximum = max(weighted)
    minimum = min(weighted)
    return {
        "bottleneck_replicas": peak_rows,
        "bottleneck_threshold": 0.9,
        "coefficient_of_variation": cv(weighted),
        "communication_coefficient_of_variation": cv(communication),
        "communication_imbalance_ratio_max_over_mean": (
            max(communication) / comm_mean if comm_mean else 0.0
        ),
        "compute_coefficient_of_variation": cv(compute),
        "compute_imbalance_ratio_max_over_mean": (
            max(compute) / cmean if cmean else 0.0
        ),
        "imbalance_ratio_max_over_mean": maximum / wmean if wmean else 0.0,
        "max_replica_communication_bytes": max(communication),
        "max_replica_compute_bytes": max(compute),
        "max_replica_load_bytes": maximum,
        "mean_replica_communication_bytes": comm_mean,
        "mean_replica_compute_bytes": cmean,
        "mean_replica_load_bytes": wmean,
        "min_replica_load_bytes": minimum,
        "normalized_range": (maximum - minimum) / wmean if wmean else 0.0,
        "peak_utilization": max(row["utilization"] for row in rows),
    }


def run_reference(project: NormalizedProject) -> dict[str, Any]:
    """Allocate with an independent implementation of the reference policy."""

    assignments, replicas, _ = _allocate(project)
    before_assignments, before_replicas, _ = _before_rebalance_state(project)
    before_feasible = all(
        sum(
            row["weighted_load_bytes"]
            for row in before_assignments.values()
            if row["target_replica"] == replica_id
        )
        <= replica.capacity_bytes
        for replica_id, replica in before_replicas.items()
    )
    final_policy = "global-minmax"
    if before_feasible and _assignment_key(
        before_assignments, before_replicas
    ) < _assignment_key(assignments, replicas):
        assignments = before_assignments
        final_policy = "retained-event-state-minmax"
    requests = {request.request_id: request for request in project.requests}
    ledger = _reference_ledger(project, assignments, replicas)
    metrics = _reference_metrics(ledger)
    signature = [
        [request_id, assignments[request_id]["expert_id"], assignments[request_id]["target_replica"]]
        for request_id in sorted(assignments)
    ]
    request_basis = []
    for request_id in sorted(assignments):
        request = requests[request_id]
        request_basis.append(
            {
                "basis": (
                    "weighted least-loaded admission, then independent "
                    "best-improvement min-max relocation"
                ),
                "domain_candidates": sorted(
                    expert.expert_id
                    for expert in project.experts
                    if request.domain in expert.domains
                ),
                "request_id": request_id,
                "selected": {
                    **assignments[request_id],
                    "rationale": "reference descending-work weighted choice",
                },
            }
        )
    return {
        "allocator": "independent-reference",
        "assignment_signature": signature,
        "assignments": [
            {
                **assignments[request_id],
                "rationale": "reference descending-work weighted choice",
            }
            for request_id in sorted(assignments)
        ],
        "final_topology": {
            "experts": [
                {
                    "cross_rack_comm_bytes": expert.cross_rack_comm_bytes,
                    "domains": list(expert.domains),
                    "expert_id": expert.expert_id,
                    "home_replica": expert.home_replica,
                    "per_sample_cost_bytes": expert.per_sample_cost_bytes,
                    "precision": expert.precision,
                }
                for _, expert in sorted(_topology_at_end(project)[1].items())
            ],
            "replicas": [
                {
                    "capacity_bytes": replica.capacity_bytes,
                    "rack": replica.rack,
                    "replica_id": replica.replica_id,
                    "supported_precisions": list(replica.supported_precisions),
                }
                for _, replica in sorted(replicas.items())
            ],
        },
        "final_policy": final_policy,
        "ledger": ledger,
        "metrics": metrics,
        "request_basis": request_basis,
    }


def _numeric_equal(left: Any, right: Any, tolerance: float = 1e-12) -> bool:
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return abs(float(left) - float(right)) <= tolerance
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(
            _numeric_equal(left[key], right[key], tolerance) for key in left
        )
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            _numeric_equal(a, b, tolerance) for a, b in zip(left, right)
        )
    return left == right


def compare_with_reference(
    main_result: dict[str, Any],
    reference_result: dict[str, Any],
) -> dict[str, Any]:
    """Compare main and reference results request by request."""

    main_sig = {
        row[0]: (row[1], row[2]) for row in main_result["assignment_signature"]
    }
    ref_sig = {
        row[0]: (row[1], row[2])
        for row in reference_result["assignment_signature"]
    }
    request_ids = sorted(set(main_sig) | set(ref_sig))
    first_mismatch = None
    mismatch_count = 0
    for request_id in request_ids:
        if main_sig.get(request_id) == ref_sig.get(request_id):
            continue
        mismatch_count += 1
        if first_mismatch is not None:
            continue
        main_trace = next(
            (
                trace
                for trace in main_result.get("request_traces", [])
                if trace["request_id"] == request_id
            ),
            None,
        )
        reference_basis = next(
            (
                row
                for row in reference_result.get("request_basis", [])
                if row["request_id"] == request_id
            ),
            None,
        )
        main_selected = main_trace["selected"] if main_trace else None
        ref_selected = (
            reference_basis["selected"] if reference_basis else None
        )
        differences = []
        if main_selected and ref_selected:
            for field in (
                "expert_id",
                "target_replica",
                "weighted_load_bytes",
                "billed_communication_bytes",
                "rationale",
            ):
                if main_selected.get(field) != ref_selected.get(field):
                    differences.append(
                        {
                            "field": field,
                            "main": main_selected.get(field),
                            "reference": ref_selected.get(field),
                        }
                    )
        first_mismatch = {
            "basis_differences": differences,
            "main_basis": {
                "candidate_experts": (
                    main_trace.get("candidate_experts") if main_trace else None
                ),
                "selected": main_selected,
                "selection_rule": (
                    main_trace.get("selection_rule") if main_trace else None
                ),
            },
            "reference_basis": reference_basis,
            "request_id": request_id,
        }
    ledger_equal = _numeric_equal(
        main_result["after_rebalance"]["ledger"], reference_result["ledger"]
    )
    metrics_equal = _numeric_equal(
        main_result["after_rebalance"]["metrics"], reference_result["metrics"]
    )
    requests_equal = mismatch_count == 0
    return {
        "agrees": requests_equal and ledger_equal and metrics_equal,
        "first_mismatch": first_mismatch,
        "first_mismatch_request": (
            first_mismatch["request_id"] if first_mismatch else None
        ),
        "ledger_equal": ledger_equal,
        "metrics_equal": metrics_equal,
        "mismatch_count": mismatch_count,
        "requests_checked": len(request_ids),
        "requests_equal": requests_equal,
    }

