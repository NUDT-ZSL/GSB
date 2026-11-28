"""Deterministic request routing, event replay, ledgers and rebalancing."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable

from .model import Expert, InferenceRequest, NormalizedProject, Topology
from .normalize import canonical_fingerprint, sequence_blocks

SELECTION_RULE = (
    "minimize projected (max replica load, squared replica load, "
    "billed cross-rack communication, target replica, expert id)"
)
GLOBAL_ORDER_RULE = (
    "descending minimum candidate compute work, then request id; "
    + SELECTION_RULE
)


@dataclass(frozen=True)
class RouteOption:
    request_id: str
    expert_id: str
    home_replica: str
    precision: str
    target_replica: str
    target_rack: str
    sequence_blocks: int
    sample_units: int
    compute_load_bytes: int
    cross_replica_raw_bytes: int
    same_rack_communication_avoided_bytes: int
    billed_communication_bytes: int
    weighted_load_bytes: int
    same_rack_dispatch: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "billed_communication_bytes": self.billed_communication_bytes,
            "compute_load_bytes": self.compute_load_bytes,
            "cross_replica_raw_bytes": self.cross_replica_raw_bytes,
            "expert_id": self.expert_id,
            "home_replica": self.home_replica,
            "precision": self.precision,
            "request_id": self.request_id,
            "same_rack_communication_avoided_bytes": (
                self.same_rack_communication_avoided_bytes
            ),
            "same_rack_dispatch": self.same_rack_dispatch,
            "sample_units": self.sample_units,
            "sequence_blocks": self.sequence_blocks,
            "target_replica": self.target_replica,
            "weighted_load_bytes": self.weighted_load_bytes,
        }

    def signature(self) -> tuple[str, str, str]:
        return (self.request_id, self.expert_id, self.target_replica)


def _initial_topology(project: NormalizedProject) -> Topology:
    return Topology(
        replicas={replica.replica_id: replica for replica in project.replicas},
        experts={expert.expert_id: expert for expert in project.experts},
    )


def _request_by_id(project: NormalizedProject) -> dict[str, InferenceRequest]:
    return {request.request_id: request for request in project.requests}


def _blocks(project: NormalizedProject, request: InferenceRequest) -> int:
    return sequence_blocks(
        request.sequence_length,
        int(project.normalization["reference_sequence_length"]),
    )


def _candidate_experts(
    request: InferenceRequest, topology: Topology
) -> list[Expert]:
    return sorted(
        (
            expert
            for expert in topology.experts.values()
            if request.domain in expert.domains
        ),
        key=lambda expert: expert.expert_id,
    )


def _route_options(
    project: NormalizedProject,
    request: InferenceRequest,
    topology: Topology,
) -> list[RouteOption]:
    blocks = _blocks(project, request)
    options: list[RouteOption] = []
    for expert in _candidate_experts(request, topology):
        home = topology.replicas[expert.home_replica]
        for target_id in sorted(topology.replicas):
            target = topology.replicas[target_id]
            if expert.precision not in target.supported_precisions:
                continue
            compute_load = (
                request.sample_units * blocks * expert.per_sample_cost_bytes
            )
            if target_id == expert.home_replica:
                raw_comm = 0
            else:
                raw_comm = expert.cross_rack_comm_bytes * blocks
            same_rack = target.rack == home.rack
            billed = 0 if same_rack else raw_comm
            avoided = raw_comm if same_rack else 0
            options.append(
                RouteOption(
                    request_id=request.request_id,
                    expert_id=expert.expert_id,
                    home_replica=expert.home_replica,
                    precision=expert.precision,
                    target_replica=target_id,
                    target_rack=target.rack,
                    sequence_blocks=blocks,
                    sample_units=request.sample_units,
                    compute_load_bytes=compute_load,
                    cross_replica_raw_bytes=raw_comm,
                    same_rack_communication_avoided_bytes=avoided,
                    billed_communication_bytes=billed,
                    weighted_load_bytes=compute_load + billed,
                    same_rack_dispatch=same_rack,
                )
            )
    return options


def _candidate_min_work(
    project: NormalizedProject,
    request: InferenceRequest,
    topology: Topology,
) -> int:
    blocks = _blocks(project, request)
    candidates = _candidate_experts(request, topology)
    if not candidates:
        raise ValueError(
            f"request {request.request_id}: no expert for domain {request.domain!r}"
        )
    return min(
        request.sample_units * blocks * expert.per_sample_cost_bytes
        for expert in candidates
    )


def _loads(
    assignments: dict[str, RouteOption], topology: Topology
) -> dict[str, int]:
    loads = {replica_id: 0 for replica_id in topology.replicas}
    for route in assignments.values():
        loads[route.target_replica] += route.weighted_load_bytes
    return loads


def _objective(
    option: RouteOption,
    loads: dict[str, int],
    topology: Topology,
) -> tuple[int, int, int, str, str]:
    projected = dict(loads)
    projected[option.target_replica] += option.weighted_load_bytes
    return (
        max(projected.values()),
        sum(value * value for value in projected.values()),
        option.billed_communication_bytes,
        option.target_replica,
        option.expert_id,
    )


def _overflow_objective(
    option: RouteOption,
    loads: dict[str, int],
    topology: Topology,
) -> tuple[int, int, int, int, str, str]:
    projected = dict(loads)
    projected[option.target_replica] += option.weighted_load_bytes
    overflow = sum(
        max(0, projected[replica_id] - topology.replicas[replica_id].capacity_bytes)
        for replica_id in projected
    )
    return (
        overflow,
        max(projected.values()),
        sum(value * value for value in projected.values()),
        option.billed_communication_bytes,
        option.target_replica,
        option.expert_id,
    )


def _ranked_options(
    project: NormalizedProject,
    request: InferenceRequest,
    topology: Topology,
    loads: dict[str, int],
    *,
    capacity_aware: bool = True,
) -> list[tuple[RouteOption, tuple[int, ...] | tuple[int, int, int, str, str]]]:
    ranked = []
    for option in _route_options(project, request, topology):
        capacity = topology.replicas[option.target_replica].capacity_bytes
        if not capacity_aware or loads[option.target_replica] + option.weighted_load_bytes <= capacity:
            ranked.append((option, _objective(option, loads, topology)))
    ranked.sort(key=lambda item: (item[1], item[0].expert_id, item[0].target_replica))
    return ranked


def _serialize_option_groups(
    project: NormalizedProject,
    request: InferenceRequest,
    topology: Topology,
    loads: dict[str, int] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for option in _route_options(project, request, topology):
        item = option.to_dict()
        if loads is not None:
            capacity = topology.replicas[option.target_replica].capacity_bytes
            item["remaining_capacity_bytes"] = (
                capacity - loads[option.target_replica]
            )
            item["feasible"] = (
                loads[option.target_replica] + option.weighted_load_bytes
                <= capacity
            )
        groups.setdefault(option.expert_id, []).append(item)
    return groups


def _greedy_allocation(
    project: NormalizedProject,
    requests: Iterable[InferenceRequest],
    topology: Topology,
    order_name: str,
) -> tuple[dict[str, RouteOption], bool, dict[str, dict[str, Any]]]:
    loads = {replica_id: 0 for replica_id in topology.replicas}
    assignments: dict[str, RouteOption] = {}
    details: dict[str, dict[str, Any]] = {}
    for request in requests:
        ranked = _ranked_options(project, request, topology, loads)
        if not ranked:
            return assignments, False, details
        choice, key = ranked[0]
        details[request.request_id] = {
            "loads_before": dict(loads),
            "objective": list(key),
            "feasible_option_count": len(ranked),
            "order": order_name,
        }
        assignments[request.request_id] = choice
        loads[choice.target_replica] += choice.weighted_load_bytes
    return assignments, True, details


def _overflow_allocation(
    project: NormalizedProject,
    requests: Iterable[InferenceRequest],
    topology: Topology,
) -> tuple[dict[str, RouteOption], dict[str, dict[str, Any]]]:
    loads = {replica_id: 0 for replica_id in topology.replicas}
    assignments: dict[str, RouteOption] = {}
    details: dict[str, dict[str, Any]] = {}
    for request in requests:
        options = _route_options(project, request, topology)
        if not options:
            raise ValueError(
                f"request {request.request_id}: no precision-compatible route"
            )
        best = min(
            options,
            key=lambda option: (
                _overflow_objective(option, loads, topology),
                option.expert_id,
                option.target_replica,
            ),
        )
        details[request.request_id] = {
            "loads_before": dict(loads),
            "objective": list(_overflow_objective(best, loads, topology)),
            "feasible_option_count": 0,
            "order": "capacity-overflow fallback",
        }
        assignments[request.request_id] = best
        loads[best.target_replica] += best.weighted_load_bytes
    return assignments, details


def _global_allocate(
    project: NormalizedProject,
    requests: Iterable[InferenceRequest],
    topology: Topology,
) -> tuple[
    dict[str, RouteOption],
    bool,
    dict[str, dict[str, Any]],
    str,
]:
    request_list = sorted(requests, key=lambda item: item.request_id)
    if not request_list:
        return {}, True, {}, "empty"
    min_work_desc = sorted(
        request_list,
        key=lambda request: (
            -_candidate_min_work(project, request, topology),
            request.request_id,
        ),
    )
    max_work_desc = sorted(
        request_list,
        key=lambda request: (
            -max(
                request.sample_units
                * _blocks(project, request)
                * expert.per_sample_cost_bytes
                for expert in _candidate_experts(request, topology)
            ),
            request.request_id,
        ),
    )
    attempts = (
        ("min-work-desc", min_work_desc),
        ("max-work-desc", max_work_desc),
        (
            "arrival",
            sorted(request_list, key=lambda request: (request.arrival_time, request.request_id)),
        ),
        (
            "sample-desc",
            sorted(request_list, key=lambda request: (-request.sample_units, request.request_id)),
        ),
    )
    feasible_results: list[
        tuple[
            tuple[int, int, int, tuple[tuple[str, str, str], ...]],
            str,
            dict[str, RouteOption],
            dict[str, dict[str, Any]],
        ]
    ] = []
    for order_name, order in attempts:
        assignments, feasible, details = _greedy_allocation(
            project, order, topology, order_name
        )
        if feasible:
            loads = _loads(assignments, topology)
            key = (
                max(loads.values()),
                sum(value * value for value in loads.values()),
                sum(
                    route.billed_communication_bytes
                    for route in assignments.values()
                ),
                tuple(
                    sorted(route.signature() for route in assignments.values())
                ),
            )
            feasible_results.append((key, order_name, assignments, details))
    if feasible_results:
        _, order_name, assignments, details = min(
            feasible_results, key=lambda item: item[0]
        )
        return assignments, True, details, order_name
    assignments, details = _overflow_allocation(project, request_list, topology)
    return assignments, False, details, "overflow-fallback"


def _ledger(
    project: NormalizedProject,
    assignments: dict[str, RouteOption],
    topology: Topology,
) -> dict[str, Any]:
    requests = _request_by_id(project)
    by_replica: dict[str, dict[str, dict[str, Any]]] = {
        replica_id: {} for replica_id in topology.replicas
    }
    for request_id in sorted(assignments, key=lambda rid: (requests[rid].arrival_time, rid)):
        route = assignments[request_id]
        expert_entry = by_replica[route.target_replica].setdefault(
            route.expert_id,
            {
                "billed_communication_bytes": 0,
                "compute_load_bytes": 0,
                "expert_id": route.expert_id,
                "request_count": 0,
                "request_ids": [],
                "same_rack_communication_avoided_bytes": 0,
                "sample_units": 0,
                "weighted_load_bytes": 0,
            },
        )
        expert_entry["billed_communication_bytes"] += (
            route.billed_communication_bytes
        )
        expert_entry["compute_load_bytes"] += route.compute_load_bytes
        expert_entry["request_count"] += 1
        expert_entry["request_ids"].append(request_id)
        expert_entry["same_rack_communication_avoided_bytes"] += (
            route.same_rack_communication_avoided_bytes
        )
        expert_entry["sample_units"] += route.sample_units
        expert_entry["weighted_load_bytes"] += route.weighted_load_bytes

    replica_rows: list[dict[str, Any]] = []
    total_compute = 0
    total_billed = 0
    total_avoided = 0
    total_weighted = 0
    for replica_id in sorted(topology.replicas):
        replica = topology.replicas[replica_id]
        experts = [
            by_replica[replica_id][expert_id]
            for expert_id in sorted(by_replica[replica_id])
        ]
        compute = sum(item["compute_load_bytes"] for item in experts)
        billed = sum(item["billed_communication_bytes"] for item in experts)
        avoided = sum(
            item["same_rack_communication_avoided_bytes"] for item in experts
        )
        weighted = sum(item["weighted_load_bytes"] for item in experts)
        total_compute += compute
        total_billed += billed
        total_avoided += avoided
        total_weighted += weighted
        replica_rows.append(
            {
                "billed_communication_bytes": billed,
                "capacity_bytes": replica.capacity_bytes,
                "compute_load_bytes": compute,
                "experts": experts,
                "rack": replica.rack,
                "remaining_capacity_bytes": replica.capacity_bytes - weighted,
                "replica_id": replica_id,
                "same_rack_communication_avoided_bytes": avoided,
                "supported_precisions": list(replica.supported_precisions),
                "utilization": (
                    weighted / replica.capacity_bytes
                    if replica.capacity_bytes
                    else 0.0
                ),
                "weighted_load_bytes": weighted,
            }
        )
    return {
        "replicas": replica_rows,
        "total_billed_communication_bytes": total_billed,
        "total_compute_load_bytes": total_compute,
        "total_same_rack_communication_avoided_bytes": total_avoided,
        "total_weighted_load_bytes": total_weighted,
        "unit": "bytes",
    }


def _coefficient_of_variation(values: list[int | float]) -> float:
    if not values:
        return 0.0
    mean = sum(values) / len(values)
    if mean == 0:
        return 0.0
    variance = sum((value - mean) ** 2 for value in values) / len(values)
    return math.sqrt(variance) / mean


def _metrics(ledger: dict[str, Any], threshold: float = 0.9) -> dict[str, Any]:
    rows = ledger["replicas"]
    weighted = [row["weighted_load_bytes"] for row in rows]
    compute = [row["compute_load_bytes"] for row in rows]
    communication = [row["billed_communication_bytes"] for row in rows]
    mean_weighted = sum(weighted) / len(weighted) if weighted else 0.0
    mean_compute = sum(compute) / len(compute) if compute else 0.0
    mean_comm = sum(communication) / len(communication) if communication else 0.0
    maximum = max(weighted) if weighted else 0
    minimum = min(weighted) if weighted else 0
    bottleneck = [
        {
            "capacity_bytes": row["capacity_bytes"],
            "rack": row["rack"],
            "replica_id": row["replica_id"],
            "utilization": row["utilization"],
            "weighted_load_bytes": row["weighted_load_bytes"],
        }
        for row in rows
        if row["utilization"] >= threshold
    ]
    if not bottleneck and rows:
        peak = max(rows, key=lambda row: (row["utilization"], row["replica_id"]))
        bottleneck = [
            {
                "capacity_bytes": peak["capacity_bytes"],
                "rack": peak["rack"],
                "replica_id": peak["replica_id"],
                "utilization": peak["utilization"],
                "weighted_load_bytes": peak["weighted_load_bytes"],
            }
        ]
    return {
        "bottleneck_replicas": bottleneck,
        "bottleneck_threshold": threshold,
        "coefficient_of_variation": _coefficient_of_variation(weighted),
        "communication_coefficient_of_variation": _coefficient_of_variation(
            communication
        ),
        "communication_imbalance_ratio_max_over_mean": (
            max(communication) / mean_comm if mean_comm else 0.0
        ),
        "compute_coefficient_of_variation": _coefficient_of_variation(compute),
        "compute_imbalance_ratio_max_over_mean": (
            max(compute) / mean_compute if mean_compute else 0.0
        ),
        "imbalance_ratio_max_over_mean": (
            maximum / mean_weighted if mean_weighted else 0.0
        ),
        "max_replica_communication_bytes": max(communication) if communication else 0,
        "max_replica_compute_bytes": max(compute) if compute else 0,
        "max_replica_load_bytes": maximum,
        "mean_replica_communication_bytes": mean_comm,
        "mean_replica_compute_bytes": mean_compute,
        "mean_replica_load_bytes": mean_weighted,
        "min_replica_load_bytes": minimum,
        "normalized_range": (
            (maximum - minimum) / mean_weighted if mean_weighted else 0.0
        ),
        "peak_utilization": max(
            (row["utilization"] for row in rows), default=0.0
        ),
    }


def _route_payload(route: RouteOption, rationale: str | None = None) -> dict[str, Any]:
    payload = route.to_dict()
    if rationale is not None:
        payload["rationale"] = rationale
    return payload


def _route_changed(left: dict[str, Any], right: dict[str, Any]) -> bool:
    fields = (
        "expert_id",
        "home_replica",
        "target_replica",
        "compute_load_bytes",
        "cross_replica_raw_bytes",
        "billed_communication_bytes",
        "weighted_load_bytes",
    )
    return any(left.get(field) != right.get(field) for field in fields)


def _assignment_signature(assignments: dict[str, RouteOption]) -> list[list[str]]:
    return [
        [request_id, route.expert_id, route.target_replica]
        for request_id, route in sorted(assignments.items())
    ]


def _allocation_key(
    assignments: dict[str, RouteOption],
    topology: Topology,
) -> tuple[int, int, int, tuple[tuple[str, str, str], ...]]:
    loads = _loads(assignments, topology)
    return (
        max(loads.values(), default=0),
        sum(value * value for value in loads.values()),
        sum(
            route.billed_communication_bytes
            for route in assignments.values()
        ),
        tuple(sorted(route.signature() for route in assignments.values())),
    )


def _affected_for_event(
    event_kind: str,
    assignments: dict[str, RouteOption],
    topology_before: Topology,
    event: Any,
) -> tuple[list[str], list[str]]:
    if event.kind == "migrate_expert":
        affected = [
            request_id
            for request_id, route in assignments.items()
            if route.expert_id == event.expert_id
        ]
        dependent = [event.expert_id]
    else:
        replica_id = event.replica_id
        affected = [
            request_id
            for request_id, route in assignments.items()
            if route.target_replica == replica_id
        ]
        dependent = sorted(
            {
                expert.expert_id
                for expert in topology_before.experts.values()
                if expert.home_replica == replica_id
            }
            | {
                assignments[request_id].expert_id
                for request_id in affected
            }
        )
    return sorted(affected), dependent


def _event_recompute(
    project: NormalizedProject,
    event: Any,
    topology_before: Topology,
    assignments: dict[str, RouteOption],
    arrived_requests: dict[str, InferenceRequest],
) -> tuple[
    Topology,
    dict[str, RouteOption],
    dict[str, Any],
]:
    topology_after = topology_before.apply_event(event)
    before_assignments = dict(assignments)
    affected, dependent = _affected_for_event(
        event.kind, assignments, topology_before, event
    )
    loads = _loads(assignments, topology_after)
    old_replica_load = None
    old_capacity = None
    new_capacity = None
    shortfall = 0
    if event.kind == "scale_replica":
        old_replica_load = _loads(assignments, topology_before)[event.replica_id]
        old_capacity = topology_before.replicas[event.replica_id].capacity_bytes
        new_capacity = topology_after.replicas[event.replica_id].capacity_bytes
        shortfall = max(0, old_replica_load - new_capacity)

    for request_id in affected:
        route = assignments.pop(request_id)
        loads[route.target_replica] -= route.weighted_load_bytes

    failed: list[dict[str, Any]] = []
    route_order = sorted(
        (arrived_requests[request_id] for request_id in affected),
        key=lambda request: (
            -_candidate_min_work(project, request, topology_after),
            request.request_id,
        ),
    )
    for request in route_order:
        ranked = _ranked_options(project, request, topology_after, loads)
        if ranked:
            choice, _ = ranked[0]
            assignments[request.request_id] = choice
            loads[choice.target_replica] += choice.weighted_load_bytes
            continue
        details = []
        for option in _route_options(project, request, topology_after):
            capacity = topology_after.replicas[option.target_replica].capacity_bytes
            remaining = capacity - loads[option.target_replica]
            details.append(
                {
                    "billed_communication_bytes": option.billed_communication_bytes,
                    "expert_id": option.expert_id,
                    "remaining_capacity_bytes": remaining,
                    "shortfall_bytes": max(
                        0, option.weighted_load_bytes - remaining
                    ),
                    "target_replica": option.target_replica,
                    "weighted_load_bytes": option.weighted_load_bytes,
                }
            )
        failed.append(
            {
                "request_id": request.request_id,
                "candidate_options": details,
                "reason": (
                    "no domain-matched, precision-compatible target replica has "
                    "remaining capacity under the post-event topology"
                ),
            }
        )

    fallback_used = False
    fallback_order = None
    if failed:
        fallback_used = True
        assigned_ids = set(assignments)
        all_arrived = [
            request
            for request in project.requests
            if request.request_id in assigned_ids or request.request_id in affected
        ]
        reassigned, feasible, _, fallback_order = _global_allocate(
            project, all_arrived, topology_after
        )
        fallback_reason = (
            "incremental post-event placement was infeasible; a deterministic "
            "capacity-aware global fallback re-allocated every already-arrived "
            "request under the post-event topology"
        )
        if not feasible:
            reassigned, _ = _overflow_allocation(project, all_arrived, topology_after)
            fallback_reason += " (even the global fallback overflowed)"
        assignments = reassigned
    else:
        fallback_reason = None

    changed_requests = sorted(
        request_id
        for request_id in set(before_assignments) | set(assignments)
        if before_assignments.get(request_id) != assignments.get(request_id)
    )
    unaffected_changed = sorted(set(changed_requests) - set(affected))
    after_ledger = _ledger(project, assignments, topology_after)
    result: dict[str, Any] = {
        "affected_request_ids": affected,
        "affected_request_count": len(affected),
        "after_ledger": after_ledger,
        "after_metrics": _metrics(after_ledger),
        "assignment_changes": {
            request_id: {
                "after": (
                    _route_payload(assignments[request_id])
                    if request_id in assignments
                    else None
                ),
                "before": (
                    _route_payload(before_assignments[request_id])
                    if request_id in before_assignments
                    else None
                ),
            }
            for request_id in changed_requests
        },
        "assignments_after": {
            request_id: _route_payload(assignments[request_id])
            for request_id in sorted(set(affected) & set(assignments))
        },
        "assignments_before": {
            request_id: _route_payload(before_assignments[request_id])
            for request_id in affected
            if request_id in before_assignments
        },
        "at": event.at,
        "changed_request_ids": changed_requests,
        "dependent_expert_ids": dependent,
        "event_id": event.event_id,
        "fallback_order": fallback_order,
        "fallback_reason": fallback_reason,
        "fallback_used": fallback_used,
        "feasible_after_event": all(
            row["weighted_load_bytes"] <= row["capacity_bytes"]
            for row in after_ledger["replicas"]
        ),
        "infeasible_candidates": failed,
        "kind": event.kind,
        "new_capacity_bytes": new_capacity,
        "old_capacity_bytes": old_capacity,
        "old_target_load_bytes": old_replica_load,
        "shortfall_bytes": shortfall,
        "unaffected_but_reassigned_request_ids": unaffected_changed,
    }
    if event.kind == "migrate_expert":
        result.update(
            {
                "expert_id": event.expert_id,
                "experts_requiring_attention": sorted(set(dependent + [event.expert_id])),
                "experts_requiring_migration": [event.expert_id],
                "from_replica": event.from_replica,
                "to_replica": event.to_replica,
            }
        )
    else:
        result.update(
            {
                "experts_requiring_attention": dependent,
                "experts_requiring_migration": [],
                "replica_id": event.replica_id,
                "resolution": (
                    "post-event rerouting restored every replica to or below "
                    "its capacity"
                    if result["feasible_after_event"]
                    else "capacity remains insufficient after deterministic fallback"
                ),
            }
        )
    return topology_after, assignments, result


def _candidate_filter_text(request: InferenceRequest) -> str:
    return (
        f"domain == {request.domain!r}; expert precision must be supported "
        "by at least one target replica"
    )


def _target_basis_text() -> str:
    return (
        "selected route has a domain-matched expert, a precision-compatible "
        "target replica, remaining weighted capacity, and the lowest balance "
        "objective among feasible options"
    )


def run_allocation(project: NormalizedProject) -> dict[str, Any]:
    """Run admission routing, event replay, ledger accounting and rebalancing."""

    requests = sorted(project.requests, key=lambda request: (request.arrival_time, request.request_id))
    request_map = {request.request_id: request for request in requests}
    events = sorted(project.events, key=lambda event: (event.at, event.event_id))
    topology = _initial_topology(project)
    assignments: dict[str, RouteOption] = {}
    loads = {replica_id: 0 for replica_id in topology.replicas}
    arrival_traces: dict[str, dict[str, Any]] = {}
    event_results: list[dict[str, Any]] = []
    arrived: dict[str, InferenceRequest] = {}

    event_index = 0

    def process_events_until(logical_time: int) -> None:
        nonlocal topology, assignments, loads, event_index
        while event_index < len(events) and events[event_index].at <= logical_time:
            event = events[event_index]
            topology, assignments, result = _event_recompute(
                project, event, topology, assignments, arrived
            )
            loads = _loads(assignments, topology)
            event_results.append(result)
            event_index += 1

    for request in requests:
        process_events_until(request.arrival_time)
        arrived[request.request_id] = request
        loads_before = dict(loads)
        candidate_experts = _candidate_experts(request, topology)
        option_groups = _serialize_option_groups(
            project, request, topology, loads
        )
        ranked = _ranked_options(project, request, topology, loads)
        admission_rebalanced = False
        if ranked:
            selected, objective = ranked[0]
            rationale = "incremental admission choice"
        else:
            seen = [request_map[request_id] for request_id in sorted(arrived)]
            proposed, feasible, _, order_name = _global_allocate(
                project, seen, topology
            )
            admission_rebalanced = True
            if feasible:
                assignments = proposed
                loads = _loads(assignments, topology)
                selected = assignments[request.request_id]
                objective = list(
                    _objective(selected, loads, topology)
                )
                rationale = (
                    "capacity-aware admission fallback rebalanced all requests "
                    f"seen so far using {order_name}"
                )
            else:
                selected = min(
                    _route_options(project, request, topology),
                    key=lambda option: (
                        _overflow_objective(option, loads, topology),
                        option.expert_id,
                        option.target_replica,
                    ),
                )
                objective = list(
                    _overflow_objective(selected, loads, topology)
                )
                rationale = (
                    "no feasible capacity-aware route; retained the least-overflow "
                    "route for explainability, to be repaired by the final global pass"
                )
                assignments[request.request_id] = selected
                loads[selected.target_replica] += selected.weighted_load_bytes
        if request.request_id not in assignments:
            assignments[request.request_id] = selected
            loads[selected.target_replica] += selected.weighted_load_bytes
        arrival_traces[request.request_id] = {
            "admission_rebalanced": admission_rebalanced,
            "at": request.arrival_time,
            "candidate_experts": [expert.to_dict() for expert in candidate_experts],
            "candidate_filter": _candidate_filter_text(request),
            "candidate_expert_ids": [expert.expert_id for expert in candidate_experts],
            "loads_before": {
                replica_id: value for replica_id, value in loads_before.items()
            },
            "objective": list(objective),
            "options_by_expert": option_groups,
            "request_id": request.request_id,
            "selection_rule": SELECTION_RULE,
            "selected": _route_payload(selected, rationale),
            "topology_events_seen": list(topology.events_seen),
        }

    while event_index < len(events):
        event = events[event_index]
        topology, assignments, result = _event_recompute(
            project, event, topology, assignments, arrived
        )
        loads = _loads(assignments, topology)
        event_results.append(result)
        event_index += 1

    before_assignments = dict(assignments)
    before_ledger = _ledger(project, before_assignments, topology)
    before_metrics = _metrics(before_ledger)

    after_assignments, final_feasible, final_details, final_order = _global_allocate(
        project, requests, topology
    )
    before_key = _allocation_key(before_assignments, topology)
    global_key = _allocation_key(after_assignments, topology)
    before_feasible = all(
        row["weighted_load_bytes"] <= row["capacity_bytes"]
        for row in before_ledger["replicas"]
    )
    if before_feasible and before_key < global_key:
        after_assignments = dict(before_assignments)
        final_order = "retained-event-state-minmax"
        after_loads = _loads(after_assignments, topology)
        final_details = {}
        for request_id, route in after_assignments.items():
            base_loads = dict(after_loads)
            base_loads[route.target_replica] -= route.weighted_load_bytes
            final_details[request_id] = {
                "feasible_option_count": sum(
                    1
                    for option in _route_options(
                        project, request_map[request_id], topology
                    )
                    if base_loads[option.target_replica]
                    + option.weighted_load_bytes
                    <= topology.replicas[option.target_replica].capacity_bytes
                ),
                "loads_before": base_loads,
                "objective": list(_objective(route, base_loads, topology)),
                "order": final_order,
            }
    after_ledger = _ledger(project, after_assignments, topology)
    after_metrics = _metrics(after_ledger)

    adjustments: list[dict[str, Any]] = []
    for request_id in sorted(request_map):
        previous = before_assignments.get(request_id)
        current = after_assignments.get(request_id)
        if previous == current:
            continue
        adjustments.append(
            {
                "after": (
                    _route_payload(
                        current,
                        "global min-max rebalance reconstruction",
                    )
                    if current
                    else None
                ),
                "before": (
                    _route_payload(
                        previous,
                        "state after event replay, before final global rebalance",
                    )
                    if previous
                    else None
                ),
                "reason": (
                    "final global min-max pass changed expert ownership and/or "
                    "target replica"
                ),
                "request_id": request_id,
                "step": len(adjustments) + 1,
            }
        )

    traces: list[dict[str, Any]] = []
    for request in requests:
        route = after_assignments[request.request_id]
        arrival = arrival_traces[request.request_id]
        final_options = _serialize_option_groups(
            project, request, topology, _loads(after_assignments, topology)
        )
        history = []
        for event in event_results:
            before_route = event["assignments_before"].get(request.request_id)
            after_route = event["assignments_after"].get(request.request_id)
            if before_route is not None or after_route is not None:
                history.append(
                    {
                        "after": after_route,
                        "at": event["at"],
                        "before": before_route,
                        "event_id": event["event_id"],
                    }
                )
        final_route_payload = _route_payload(route)
        if _route_changed(arrival["selected"], final_route_payload):
            history.append(
                {
                    "after": final_route_payload,
                    "at": None,
                    "before": arrival["selected"],
                    "event_id": "FINAL_REBALANCE",
                }
            )
        traces.append(
            {
                "arrival_selection": arrival["selected"],
                "arrival_time": request.arrival_time,
                "candidate_experts": arrival["candidate_experts"],
                "candidate_filter": arrival["candidate_filter"],
                "changed_since_arrival": _route_changed(
                    arrival["selected"], final_route_payload
                ),
                "domain": request.domain,
                "final_options_by_expert": final_options,
                "final_selection_basis": {
                    "objective": final_details[request.request_id]["objective"],
                    "objective_priority": [
                        "projected maximum replica load",
                        "projected sum of squared replica loads",
                        "billed cross-rack communication",
                        "target replica id",
                        "expert id",
                    ],
                    "order": final_details[request.request_id]["order"],
                },
                "request_id": request.request_id,
                "route_history": history,
                "sample_units": request.sample_units,
                "selected": _route_payload(route),
                "selection_rule": GLOBAL_ORDER_RULE,
                "sequence_blocks": _blocks(project, request),
                "sequence_length": request.sequence_length,
                "target_selection_basis": _target_basis_text(),
            }
        )

    assignments_payload = [
        _route_payload(
            after_assignments[request_id],
            (
                "descending-work global min-max allocation selected the smallest "
                "projected objective among capacity-feasible options"
            ),
        )
        for request_id in sorted(after_assignments)
    ]
    return {
        "after_rebalance": {
            "ledger": after_ledger,
            "metrics": after_metrics,
        },
        "arrival_traces": [
            arrival_traces[request.request_id] for request in requests
        ],
        "assignment_signature": _assignment_signature(after_assignments),
        "assignments": assignments_payload,
        "before_rebalance": {
            "assignment_signature": _assignment_signature(before_assignments),
            "ledger": before_ledger,
            "metrics": before_metrics,
        },
        "event_results": event_results,
        "final_allocation_feasible": final_feasible,
        "final_topology": topology.to_dict(),
        "global_rebalance_order": final_order,
        "ledger": after_ledger,
        "metrics": after_metrics,
        "model_fingerprint": canonical_fingerprint(project),
        "rebalance_adjustments": adjustments,
        "request_traces": traces,
        "source_schema": project.source_schema,
    }

