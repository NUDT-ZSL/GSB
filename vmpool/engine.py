"""Pure deterministic capacity arbitration and dependency invalidation."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

from .errors import (
    E_BUDGET,
    E_DEPENDENCY_CAPACITY,
    E_POOL_QUOTA,
    E_SAFETY_MARGIN,
    E_WITHDRAW_NOT_ACCEPTED,
)
from .fixed import apply_safety_margin, canonical_json, ceil_div, digest_json
from .graph import paths_to
from .model import Request, Scenario


ACCEPTED = "ACCEPTED"


@dataclass
class EngineResult:
    state: dict[str, Any]
    decision: dict[str, Any]
    invalidations: list[dict[str, Any]]
    affected_services: list[str]
    reused_services: list[str]

    def output(
        self,
        scenario: Scenario,
        request: Request,
        *,
        round_id: str,
        base_version: int,
    ) -> dict[str, Any]:
        state = self.state
        return {
            "ok": True,
            "command": "apply",
            "request": request.as_dict(),
            "decision": self.decision,
            "effects": {
                "affected_services": self.affected_services,
                "reused_services": self.reused_services,
                "invalidations": self.invalidations,
                "pool": state["pool"],
                "budget": state["budget"],
                "target_derived": state["derived"].get(request.service),
            },
            "round": {
                "id": round_id,
                "base_version": base_version,
                "version": state["version"],
            },
            "state_digest": digest_json(state),
        }


def _conversion_index(scenario: Scenario) -> dict[str, str]:
    return {
        conversion.field_path: conversion.conversion_id
        for conversion in scenario.conversions
    }


def build_derived(
    scenario: Scenario,
    state: dict[str, Any],
    *,
    topo_subset: set[str] | None = None,
    effective_seed: dict[str, int] | None = None,
    bottleneck_seed: dict[str, list[str]] | None = None,
) -> tuple[dict[str, Any], dict[str, int], dict[str, list[str]]]:
    """Build derived records in fixed topological order.

    When topo_subset is supplied, only those services are recomputed. Other
    records are reused byte-for-byte from the prior state.
    """
    old_derived = state["derived"]
    derived = copy.deepcopy(old_derived)
    effective = dict(effective_seed or {})
    bottlenecks = copy.deepcopy(bottleneck_seed or {})
    conversion_ids = _conversion_index(scenario)
    policy = scenario.policy
    denominator = policy.ratio_denominator
    subset = set(scenario.topo_order) if topo_subset is None else topo_subset

    effective_services: dict[str, int] = {}
    effective_path: dict[str, list[str]] = {}
    for name in scenario.topo_order:
        service = scenario.services[name]
        service_state = state["services"][name]
        accepted = service_state["replicas"]
        if name not in subset and name in derived:
            old = derived[name]
            effective_services[name] = old["effective_capacity"]
            effective_path[name] = list(old["bottleneck_path"])
            continue

        upstream_names = list(scenario.upstream[name])
        if upstream_names:
            upstream_values = [
                effective.get(upstream, effective_services[upstream])
                for upstream in upstream_names
            ]
            minimum = min(upstream_values)
            candidates = [
                upstream
                for upstream in upstream_names
                if effective.get(upstream, effective_services[upstream]) == minimum
            ]
            upstream_limit = minimum if minimum < accepted else None
            bottleneck_upstream = sorted(candidates)[0]
            bottleneck_path = [name] + copy.deepcopy(
                bottlenecks.get(
                    bottleneck_upstream,
                    effective_path.get(bottleneck_upstream, [bottleneck_upstream]),
                )
            )
        else:
            upstream_limit = None
            bottleneck_path = [name]
        effective_capacity = (
            accepted if upstream_limit is None else min(accepted, upstream_limit)
        )
        if effective_capacity < accepted and upstream_limit is not None:
            bottleneck_path = [name] + copy.deepcopy(
                bottlenecks.get(
                    bottleneck_upstream,
                    effective_path.get(bottleneck_upstream, [bottleneck_upstream]),
                )
            )
        else:
            bottleneck_path = [name]

        cost = accepted * service.unit_price_fixed
        initial_cost = service.current_replicas * service.unit_price_fixed
        cost_error = accepted * service.unit_price_error
        cpu_usage = accepted * service.cpu_fixed
        memory_usage = accepted * service.memory_fixed
        cpu_error = accepted * service.cpu_error_fixed
        memory_error = accepted * service.memory_error_fixed

        direct_edges: list[dict[str, Any]] = []
        for child in scenario.downstream[name]:
            direct_edges.append(
                {
                    "downstream": child,
                    "edge_support_limit": effective_capacity,
                    "path": [name, child],
                }
            )
        closure_edges: list[dict[str, Any]] = []
        for target in scenario.transitive_downstream[name]:
            path_candidates = paths_to(name, target, scenario.downstream)
            closure_edges.append(
                {
                    "downstream": target,
                    "paths": path_candidates,
                    "support_limit": effective_capacity,
                }
            )
        shortage = accepted - effective_capacity
        dependency_shortage = None
        if shortage > 0:
            dependency_shortage = {
                "reason_code": E_DEPENDENCY_CAPACITY,
                "accepted_replicas": accepted,
                "effective_capacity": effective_capacity,
                "shortage": shortage,
                "bottleneck_path": bottleneck_path,
            }

        derived[name] = {
            "service": name,
            "topological_index": scenario.topo_order.index(name),
            "accepted_replicas": accepted,
            "effective_capacity": effective_capacity,
            "upstream_limit": upstream_limit,
            "capacity_shortage": shortage,
            "bottleneck_path": bottleneck_path,
            "dependency_shortage": dependency_shortage,
            "cost_fixed": cost,
            "cost_delta_vs_initial_fixed": cost - initial_cost,
            "cpu_usage_fixed": cpu_usage,
            "memory_usage_fixed": memory_usage,
            "cost_error_bound_fixed": cost_error,
            "cpu_error_bound_fixed": cpu_error,
            "memory_error_bound_fixed": memory_error,
            "downstream_edges": direct_edges,
            "downstream_closures": closure_edges,
            "downstream_support_sum_fixed": sum(
                edge["edge_support_limit"] for edge in direct_edges
            ),
            "budget_remaining_reference": "budget.remaining",
            "basis": {
                "service_source": f"services.json:services.{name}",
                "policy_source": "policy.json",
                "observation_parameters": {
                    "accepted_replicas": accepted,
                    "vcpu_per_replica_fixed": service.cpu_fixed,
                    "memory_mib_per_replica_fixed": service.memory_fixed,
                    "unit_price_per_replica_fixed": service.unit_price_fixed,
                    "safety_margin_ratio_fixed": policy.safety_ratio_fixed,
                    "safety_ratio_denominator": denominator,
                },
                "conversion_ids": [
                    conversion_ids[f"services.{name}.vcpu_per_replica"],
                    conversion_ids[f"services.{name}.memory_mib_per_replica"],
                    conversion_ids[f"services.{name}.unit_price_per_replica"],
                ],
                "intermediate_chain": [
                    "accepted_replicas*cpu_per_replica_fixed=cpu_usage_fixed",
                    "accepted_replicas*memory_mib_per_replica_fixed=memory_usage_fixed",
                    "accepted_replicas*unit_price_per_replica_fixed=cost_fixed",
                    (
                        "effective_capacity=min(accepted_replicas,"
                        "all_upstream_effective_capacity)"
                    ),
                ],
            },
        }
        effective_services[name] = effective_capacity
        effective_path[name] = bottleneck_path
        effective[name] = effective_capacity
        bottlenecks[name] = bottleneck_path
    return derived, effective_services, effective_path


def _initial_services_state(scenario: Scenario) -> dict[str, Any]:
    services: dict[str, Any] = {}
    for name, spec in scenario.services.items():
        cpu = spec.current_replicas * spec.cpu_fixed
        memory = spec.current_replicas * spec.memory_fixed
        cost = spec.current_replicas * spec.unit_price_fixed
        services[name] = {
            "initial_replicas": spec.current_replicas,
            "replicas": spec.current_replicas,
            "accepted_history": [],
            "last_request_seq": None,
            "last_change_seq": None,
            "last_change_direction": 0,
            "cpu_usage_fixed": cpu,
            "memory_usage_fixed": memory,
            "cost_fixed": cost,
            "cost_error_bound_fixed": spec.current_replicas
            * spec.unit_price_error,
            "cpu_error_bound_fixed": spec.current_replicas
            * spec.cpu_error_fixed,
            "memory_error_bound_fixed": spec.current_replicas
            * spec.memory_error_fixed,
        }
    return services


def initial_state(scenario: Scenario) -> dict[str, Any]:
    services = _initial_services_state(scenario)
    initial_service_cpu = sum(
        item["cpu_usage_fixed"] for item in services.values()
    )
    initial_service_memory = sum(
        item["memory_usage_fixed"] for item in services.values()
    )
    initial_cost = sum(item["cost_fixed"] for item in services.values())
    state: dict[str, Any] = {
        "version": 0,
        "scenario_digest": scenario.scenario_digest,
        "pool": {
            "name": scenario.pool.name,
            "available_cpu_fixed": scenario.pool.available_cpu,
            "available_memory_fixed": scenario.pool.available_memory,
            "occupied_cpu_fixed": scenario.pool.occupied_cpu,
            "occupied_memory_fixed": scenario.pool.occupied_memory,
            "free_cpu_fixed": scenario.pool.available_cpu
            - scenario.pool.occupied_cpu,
            "free_memory_fixed": scenario.pool.available_memory
            - scenario.pool.occupied_memory,
            "baseline_other_cpu_fixed": scenario.pool.occupied_cpu
            - initial_service_cpu,
            "baseline_other_memory_fixed": scenario.pool.occupied_memory
            - initial_service_memory,
        },
        "budget": {
            "limit_fixed": scenario.policy.budget_fixed,
            "used_fixed": initial_cost,
            "remaining_fixed": scenario.policy.budget_fixed - initial_cost,
            "error_bound_fixed": scenario.policy.budget_error
            + sum(item["cost_error_bound_fixed"] for item in services.values()),
        },
        "services": services,
        "derived": {},
        "processed_sequences": [],
        "last_clock_ms": None,
    }
    derived, _, _ = build_derived(
        scenario, {**state, "derived": {}}, topo_subset=set(scenario.topo_order)
    )
    state["derived"] = derived
    return state


def _decision_check(
    name: str,
    available: int,
    required: int,
    tolerance: int,
    *,
    reason_code: str,
    conversion_ids: list[str],
    error_bound: int,
) -> dict[str, Any]:
    difference = available - required
    return {
        "constraint": name,
        "available_fixed": available,
        "required_fixed": required,
        "difference_fixed": difference,
        "tolerance_fixed": tolerance,
        "precision_sensitive": abs(difference) < tolerance,
        "satisfied": required <= available,
        "reason_code": reason_code,
        "source_chain": conversion_ids,
        "error_upper_bound_fixed": error_bound,
    }


def _first_failure(checks: list[dict[str, Any]]) -> dict[str, Any] | None:
    for check in checks:
        if not check["satisfied"]:
            return check
    return None


def _mark_processed(state: dict[str, Any], request: Request) -> None:
    state["processed_sequences"] = sorted(
        set(state["processed_sequences"]) | {request.seq}
    )
    state["last_clock_ms"] = request.clock_ms


def _recompute_closure(
    scenario: Scenario,
    state: dict[str, Any],
    changed_service: str,
    old_effective_map: dict[str, int],
    request: Request,
    *,
    reason: str,
    force_target_invalidation: bool,
) -> tuple[list[str], list[dict[str, Any]]]:
    accepted = state["services"][changed_service]["replicas"]
    upstream_names = list(scenario.upstream[changed_service])
    upstream_limit = (
        min(old_effective_map[name] for name in upstream_names)
        if upstream_names
        else None
    )
    new_effective = (
        accepted if upstream_limit is None else min(accepted, upstream_limit)
    )
    old_effective = old_effective_map[changed_service]
    affected = [changed_service]
    invalidations: list[dict[str, Any]] = []

    changed_effective: dict[str, int] = {}
    if new_effective != old_effective:
        changed_effective[changed_service] = new_effective
        closure = set(scenario.transitive_downstream[changed_service])
        for name in scenario.topo_order:
            if name not in closure:
                continue
            inbound = list(scenario.upstream[name])
            if not any(upstream in changed_effective for upstream in inbound):
                continue
            accepted_child = state["services"][name]["replicas"]
            candidate_effective = min(
                accepted_child,
                min(
                    changed_effective.get(
                        upstream, old_effective_map[upstream]
                    )
                    for upstream in inbound
                ),
            )
            if candidate_effective != old_effective_map[name]:
                changed_effective[name] = candidate_effective
                affected.append(name)

    invalidated_targets = (
        [changed_service] + [name for name in affected if name != changed_service]
        if changed_effective
        else ([changed_service] if force_target_invalidation else [])
    )
    for target in invalidated_targets:
        if target == changed_service:
            path = [changed_service]
        else:
            candidates = paths_to(changed_service, target, scenario.downstream)
            path = candidates[0] if candidates else [changed_service, target]
        invalidations.append(
            {
                "source_request_seq": request.seq,
                "source_service": changed_service,
                "target_service": target,
                "propagation_path": path,
                "reason": reason,
                "fields": (
                    [
                        "accepted_replicas",
                        "cost_fixed",
                        "effective_capacity",
                        "downstream_support_sum_fixed",
                    ]
                    if target == changed_service and changed_effective
                    else [
                        "accepted_replicas",
                        "cost_fixed",
                    ]
                    if target == changed_service
                    else [
                        "effective_capacity",
                        "dependency_shortage",
                        "downstream_support_sum_fixed",
                    ]
                ),
                "recomputed": True,
                "reusable": False,
                "cost_recomputed": target == changed_service,
                "cost_reusable": target != changed_service,
                "plan_recompute_required": True,
            }
        )
    return affected, invalidations


def apply_request(
    scenario: Scenario,
    current_state: dict[str, Any],
    request: Request,
) -> EngineResult:
    state = copy.deepcopy(current_state)
    base_version = state["version"]
    service = scenario.services[request.service]
    service_state = state["services"][request.service]
    previous_replicas = service_state["replicas"]
    history = copy.deepcopy(service_state["accepted_history"])
    old_derived = copy.deepcopy(state["derived"])
    old_effective_map = {
        name: old_derived[name]["effective_capacity"]
        for name in scenario.topo_order
    }
    policy = scenario.policy
    conversion_ids = _conversion_index(scenario)

    target = previous_replicas
    is_reversal = False
    reversal_reason = "adjust"
    withdraw_rejected = False
    if request.op == "withdraw":
        reverse_seq = request.reverses_seq
        if reverse_seq is None:
            if not history:
                withdraw_rejected = True
            else:
                reverse_seq = history[-1]["seq"]
        if not withdraw_rejected:
            indexes = [
                index
                for index, frame in enumerate(history)
                if frame["seq"] == reverse_seq
            ]
            if not indexes:
                withdraw_rejected = True
            else:
                index = indexes[0]
                target = (
                    service.current_replicas
                    if index == 0
                    else history[index - 1]["target"]
                )
                history = history[:index]
                is_reversal = target != previous_replicas
                reversal_reason = "withdraw"
    else:
        target = int(request.target_replicas or previous_replicas)
        previous_direction = service_state["last_change_direction"]
        if previous_direction:
            requested_direction = (
                1
                if target > previous_replicas
                else (-1 if target < previous_replicas else 0)
            )
            if requested_direction:
                is_reversal = previous_direction != requested_direction
                if is_reversal:
                    reversal_reason = "target_reversal"

    current_cpu = previous_replicas * service.cpu_fixed
    current_memory = previous_replicas * service.memory_fixed
    current_cost = previous_replicas * service.unit_price_fixed
    target_cpu = target * service.cpu_fixed
    target_memory = target * service.memory_fixed
    target_cost = target * service.unit_price_fixed
    target_safe_cpu = apply_safety_margin(
        target_cpu, policy.safety_ratio_fixed, policy.ratio_denominator
    )
    target_safe_memory = apply_safety_margin(
        target_memory, policy.safety_ratio_fixed, policy.ratio_denominator
    )
    pool = state["pool"]
    budget = state["budget"]
    other_occupied_cpu = pool["occupied_cpu_fixed"] - current_cpu
    other_occupied_memory = pool["occupied_memory_fixed"] - current_memory
    other_used_cost = budget["used_fixed"] - current_cost
    cpu_headroom = pool["available_cpu_fixed"] - other_occupied_cpu
    memory_headroom = (
        pool["available_memory_fixed"] - other_occupied_memory
    )
    budget_headroom = budget["limit_fixed"] - other_used_cost

    cpu_conversions = [
        conversion_ids["pool.available_vcpu"],
        conversion_ids["pool.occupied_vcpu"],
        conversion_ids[f"services.{service.name}.vcpu_per_replica"],
    ]
    memory_conversions = [
        conversion_ids["pool.available_memory_mib"],
        conversion_ids["pool.occupied_memory_mib"],
        conversion_ids[f"services.{service.name}.memory_mib_per_replica"],
    ]
    budget_conversions = [
        conversion_ids["policy.budget"],
        conversion_ids[f"services.{service.name}.unit_price_per_replica"],
    ]
    checks = [
        _decision_check(
            "pool_cpu",
            cpu_headroom,
            target_cpu,
            policy.tolerance_cpu_fixed,
            reason_code=E_POOL_QUOTA,
            conversion_ids=cpu_conversions,
            error_bound=(
                scenario.pool.available_cpu_error
                + scenario.pool.occupied_cpu_error
                + target * service.cpu_error_fixed
            ),
        ),
        _decision_check(
            "pool_memory",
            memory_headroom,
            target_memory,
            policy.tolerance_memory_fixed,
            reason_code=E_POOL_QUOTA,
            conversion_ids=memory_conversions,
            error_bound=(
                scenario.pool.available_memory_error
                + scenario.pool.occupied_memory_error
                + target * service.memory_error_fixed
            ),
        ),
        _decision_check(
            "budget",
            budget_headroom,
            target_cost,
            policy.tolerance_cost_fixed,
            reason_code=E_BUDGET,
            conversion_ids=budget_conversions,
            error_bound=scenario.policy.budget_error
            + target * service.unit_price_error,
        ),
        _decision_check(
            "service_cpu_safety",
            cpu_headroom,
            target_safe_cpu,
            policy.tolerance_cpu_fixed,
            reason_code=E_SAFETY_MARGIN,
            conversion_ids=cpu_conversions
            + [conversion_ids["policy.safety_margin_percent"]],
            error_bound=scenario.pool.available_cpu_error
            + scenario.pool.occupied_cpu_error
            + target * service.cpu_error_fixed
            + ceil_div(
                target_cpu * policy.safety_error_fixed,
                policy.ratio_denominator,
            ),
        ),
        _decision_check(
            "service_memory_safety",
            memory_headroom,
            target_safe_memory,
            policy.tolerance_memory_fixed,
            reason_code=E_SAFETY_MARGIN,
            conversion_ids=memory_conversions
            + [conversion_ids["policy.safety_margin_percent"]],
            error_bound=scenario.pool.available_memory_error
            + scenario.pool.occupied_memory_error
            + target * service.memory_error_fixed
            + ceil_div(
                target_memory * policy.safety_error_fixed,
                policy.ratio_denominator,
            ),
        ),
    ]
    first_failure = _first_failure(checks)
    accepted = first_failure is None and not withdraw_rejected
    decision_status = "accepted" if accepted else "rejected"
    reason_code = (
        ACCEPTED
        if accepted
        else (
            E_WITHDRAW_NOT_ACCEPTED
            if withdraw_rejected
            else first_failure["reason_code"]
        )
    )

    affected: list[str] = []
    reused: list[str] = []
    invalidations: list[dict[str, Any]] = []
    service_state["last_request_seq"] = request.seq
    _mark_processed(state, request)
    state["version"] = base_version + 1

    if accepted:
        service_state["replicas"] = target
        service_state["accepted_history"] = history
        service_state["last_change_seq"] = (
            history[-1]["seq"] if history else request.seq
        )
        service_state["last_change_direction"] = (
            1
            if target > previous_replicas
            else (-1 if target < previous_replicas else 0)
        )
        service_state["cpu_usage_fixed"] = target_cpu
        service_state["memory_usage_fixed"] = target_memory
        service_state["cost_fixed"] = target_cost
        service_state["cost_error_bound_fixed"] = (
            target * service.unit_price_error
        )
        service_state["cpu_error_bound_fixed"] = (
            target * service.cpu_error_fixed
        )
        service_state["memory_error_bound_fixed"] = (
            target * service.memory_error_fixed
        )
        if request.op == "adjust":
            service_state["accepted_history"].append(
                {"seq": request.seq, "target": target}
            )
            service_state["last_change_seq"] = request.seq
            service_state["last_change_direction"] = (
                1
                if target > previous_replicas
                else (-1 if target < previous_replicas else 0)
            )
        pool["occupied_cpu_fixed"] += target_cpu - current_cpu
        pool["occupied_memory_fixed"] += target_memory - current_memory
        pool["free_cpu_fixed"] = (
            pool["available_cpu_fixed"] - pool["occupied_cpu_fixed"]
        )
        pool["free_memory_fixed"] = (
            pool["available_memory_fixed"] - pool["occupied_memory_fixed"]
        )
        budget["used_fixed"] += target_cost - current_cost
        budget["remaining_fixed"] = budget["limit_fixed"] - budget["used_fixed"]

        closure_affected, invalidations = _recompute_closure(
            scenario,
            state,
            request.service,
            old_effective_map,
            request,
            reason=(
                reversal_reason
                if (is_reversal or request.op == "withdraw")
                else "capacity_change"
            ),
            force_target_invalidation=(
                is_reversal or request.op == "withdraw"
            ),
        )
        affected = closure_affected
        affected_set = set(affected)
        reused = [
            name
            for name in scenario.topo_order
            if name not in affected_set
        ]
        recomputed, _, _ = build_derived(
            scenario,
            state,
            topo_subset=affected_set,
        )
        for name in affected:
            state["derived"][name] = recomputed[name]
    else:
        reused = list(scenario.topo_order)

    target_derived = state["derived"][request.service]
    target_shortage = target_derived["dependency_shortage"]
    precision_checks = [check for check in checks if check["precision_sensitive"]]
    if target_shortage is not None:
        shortage_difference = (
            target_derived["accepted_replicas"]
            - target_derived["effective_capacity"]
        )
        if shortage_difference < policy.tolerance_cpu_fixed:
            precision_checks.append(
                {
                    "constraint": "dependency_capacity",
                    "available_fixed": target_derived["upstream_limit"],
                    "required_fixed": target_derived["accepted_replicas"],
                    "difference_fixed": -shortage_difference,
                    "tolerance_fixed": policy.tolerance_cpu_fixed,
                    "precision_sensitive": True,
                    "satisfied": False,
                    "reason_code": E_DEPENDENCY_CAPACITY,
                    "source_chain": [
                        conversion_ids[
                            f"services.{service.name}.vcpu_per_replica"
                        ]
                    ],
                    "error_upper_bound_fixed": 0,
                }
            )

    decision = {
        "status": decision_status,
        "reason_code": reason_code,
        "first_constraint": (
            "withdraw_history"
            if withdraw_rejected
            else (first_failure["constraint"] if first_failure else None)
        ),
        "previous_replicas": previous_replicas,
        "target_replicas": target,
        "delta_replicas": target - previous_replicas,
        "is_reversal": is_reversal,
        "reversal_kind": reversal_reason if is_reversal else None,
        "reverses_seq": request.reverses_seq,
        "constraints": checks,
        "precision_sensitive": bool(precision_checks),
        "precision_sources": precision_checks,
        "cost_delta_fixed": (
            target_cost - current_cost if accepted else 0
        ),
        "budget_remaining_after_fixed": state["budget"]["remaining_fixed"],
        "effective_capacity": target_derived["effective_capacity"],
        "capacity_shortage": target_derived["capacity_shortage"],
        "shortage_reason_code": (
            E_DEPENDENCY_CAPACITY if target_shortage is not None else None
        ),
        "shortage_bottleneck_path": (
            target_derived["bottleneck_path"]
            if target_shortage is not None
            else None
        ),
        "invalidated": bool(invalidations),
        "affected_services": affected,
        "reused_services": reused,
    }
    return EngineResult(
        state=state,
        decision=decision,
        invalidations=invalidations,
        affected_services=affected,
        reused_services=reused,
    )


def verify_state(scenario: Scenario, state: dict[str, Any]) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    def check(name: str, condition: bool, details: dict[str, Any]) -> None:
        row = {"name": name, "ok": bool(condition), "details": details}
        checks.append(row)
        if not condition:
            errors.append({"check": name, **details})

    services = state["services"]
    total_cpu = sum(
        state["services"][name]["replicas"] * scenario.services[name].cpu_fixed
        for name in scenario.services
    )
    total_memory = sum(
        state["services"][name]["replicas"]
        * scenario.services[name].memory_fixed
        for name in scenario.services
    )
    total_cost = sum(
        state["services"][name]["replicas"]
        * scenario.services[name].unit_price_fixed
        for name in scenario.services
    )
    expected_cpu = state["pool"]["baseline_other_cpu_fixed"] + total_cpu
    expected_memory = (
        state["pool"]["baseline_other_memory_fixed"] + total_memory
    )
    check(
        "pool_cpu_reconciles",
        state["pool"]["occupied_cpu_fixed"] == expected_cpu,
        {
            "expected": expected_cpu,
            "actual": state["pool"]["occupied_cpu_fixed"],
        },
    )
    check(
        "pool_memory_reconciles",
        state["pool"]["occupied_memory_fixed"] == expected_memory,
        {
            "expected": expected_memory,
            "actual": state["pool"]["occupied_memory_fixed"],
        },
    )
    check(
        "budget_reconciles",
        state["budget"]["used_fixed"] == total_cost,
        {"expected": total_cost, "actual": state["budget"]["used_fixed"]},
    )
    check(
        "pool_not_oversold",
        state["pool"]["occupied_cpu_fixed"]
        <= state["pool"]["available_cpu_fixed"]
        and state["pool"]["occupied_memory_fixed"]
        <= state["pool"]["available_memory_fixed"],
        {
            "occupied_cpu": state["pool"]["occupied_cpu_fixed"],
            "available_cpu": state["pool"]["available_cpu_fixed"],
            "occupied_memory": state["pool"]["occupied_memory_fixed"],
            "available_memory": state["pool"]["available_memory_fixed"],
        },
    )
    check(
        "budget_not_exceeded",
        state["budget"]["used_fixed"] <= state["budget"]["limit_fixed"],
        {
            "used": state["budget"]["used_fixed"],
            "limit": state["budget"]["limit_fixed"],
        },
    )
    recomputed, _, _ = build_derived(
        scenario,
        {**copy.deepcopy(state), "derived": {}},
        topo_subset=set(scenario.topo_order),
    )
    check(
        "derived_digest_reconciles",
        canonical_json(recomputed) == canonical_json(state["derived"]),
        {
            "expected_digest": digest_json(recomputed),
            "actual_digest": digest_json(state["derived"]),
        },
    )
    for name in scenario.topo_order:
        derived = state["derived"].get(name)
        check(
            f"derived_present:{name}",
            derived is not None,
            {"service": name},
        )
        if derived is None:
            continue
        check(
            f"effective_bounded:{name}",
            0 < derived["effective_capacity"] <= derived["accepted_replicas"],
            {
                "effective": derived["effective_capacity"],
                "accepted": derived["accepted_replicas"],
            },
        )
    return {
        "ok": not errors,
        "checks": checks,
        "errors": errors,
        "state_digest": digest_json(state),
        "derived_digest": digest_json(state["derived"]),
    }
