"""Unified deterministic clipping and request-allocation boundary."""

from __future__ import annotations

from typing import Iterable, Mapping

from .demand import estimate_demand, estimate_minute_parallel
from .errors import AllocationError
from .models import (
    ClipReason,
    ClusterDeclaration,
    DemandEstimate,
    MinuteDecision,
    ServiceDecision,
    ServiceSpec,
)


def _error(
    message: str,
    minute: int | None,
    service: str | None,
    **details: object,
) -> AllocationError:
    return AllocationError(
        message,
        minute=minute,
        service=service,
        details=details,
    )


def _round_robin_caps(caps: Mapping[str, int], budget: int) -> dict[str, int]:
    """Grant replicas level-by-level in name order.

    The algorithm depends only on the sorted service-name key and target caps,
    never on the order in which parallel estimators completed.
    """

    names = sorted(caps)
    allocation = {name: 0 for name in names}
    remaining = max(0, int(budget))
    max_target = max(caps.values(), default=0)
    for _level in range(1, max_target + 1):
        progressed = False
        for name in names:
            if remaining <= 0:
                return allocation
            if allocation[name] < caps[name]:
                allocation[name] += 1
                remaining -= 1
                progressed = True
        if not progressed:
            break
    return allocation


def validate_service_decision(
    decision: ServiceDecision,
    service: ServiceSpec,
) -> None:
    """Raise E_ALLOC if one service's record violates allocation invariants."""

    minute = decision.minute
    name = decision.service
    if decision.service != service.name:
        raise _error(
            "service decision does not match its declaration",
            minute,
            name,
            expected_service=service.name,
        )
    if minute < 0 or minute >= len(service.requests):
        raise _error(
            "minute is outside the service load sequence",
            minute,
            name,
            available_minutes=len(service.requests),
        )
    expected_estimate = estimate_demand(service, minute)
    if (
        decision.request_count != expected_estimate.request_count
        or decision.capacity_per_replica != service.concurrency_per_replica
        or decision.unclamped_demand != expected_estimate.unclamped_demand
        or decision.demand_replicas != expected_estimate.demand_replicas
    ):
        raise _error(
            "demand intermediates are inconsistent with the declaration",
            minute,
            name,
            expected=expected_estimate.to_dict(),
        )
    if decision.request_count != service.requests[minute]:
        raise _error(
            "request count differs from declaration",
            minute,
            name,
            declared=service.requests[minute],
            recorded=decision.request_count,
        )
    if decision.final_replicas < 0:
        raise _error("final replicas must not be negative", minute, name)
    if decision.final_replicas > service.max_replicas:
        raise _error(
            "final replicas exceed max_replicas",
            minute,
            name,
            final_replicas=decision.final_replicas,
            max_replicas=service.max_replicas,
        )
    if decision.final_replicas > service.concurrency_quota:
        raise _error(
            "final replicas exceed the per-service concurrency quota",
            minute,
            name,
            final_replicas=decision.final_replicas,
            concurrency_quota=service.concurrency_quota,
        )
    expected_capacity = decision.final_replicas * service.concurrency_per_replica
    if decision.capacity_requests != expected_capacity:
        raise _error(
            "request capacity is not final_replicas * concurrency_per_replica",
            minute,
            name,
            expected=expected_capacity,
            recorded=decision.capacity_requests,
        )
    expected_accepted = min(decision.request_count, expected_capacity)
    if decision.accepted_requests != expected_accepted:
        raise _error(
            "accepted requests are not self-consistent",
            minute,
            name,
            expected=expected_accepted,
            recorded=decision.accepted_requests,
        )
    if decision.rejected_requests != decision.request_count - decision.accepted_requests:
        raise _error(
            "rejected requests are not request_count - accepted_requests",
            minute,
            name,
        )
    if decision.rejected_requests < 0:
        raise _error("rejected requests must not be negative", minute, name)
    if decision.rejected_requests > decision.request_count:
        raise _error(
            "rejected requests must not exceed request_count",
            minute,
            name,
        )
    if decision.final_replicas < decision.demand_replicas and not decision.clip_reasons:
        raise _error(
            "a reduced allocation must carry a clipping reason",
            minute,
            name,
            demand_replicas=decision.demand_replicas,
            final_replicas=decision.final_replicas,
        )
    if decision.final_replicas > decision.demand_replicas:
        raise _error(
            "final replicas must not exceed the demand estimate",
            minute,
            name,
            demand_replicas=decision.demand_replicas,
            final_replicas=decision.final_replicas,
        )
    quota_cap = min(decision.demand_replicas, service.allocation_cap)
    expected_reasons: list[ClipReason] = []
    if quota_cap < decision.demand_replicas:
        expected_reasons.append(
            ClipReason(
                dimension="concurrency_quota",
                before_replicas=decision.demand_replicas,
                after_replicas=quota_cap,
            )
        )
    if decision.final_replicas < quota_cap:
        expected_reasons.append(
            ClipReason(
                dimension="global_budget",
                before_replicas=quota_cap,
                after_replicas=decision.final_replicas,
            )
        )
    if list(decision.clip_reasons) != expected_reasons:
        raise _error(
            "clipping reasons do not match the demand-to-final allocation chain",
            minute,
            name,
            expected=[item.to_dict() for item in expected_reasons],
            recorded=[item.to_dict() for item in decision.clip_reasons],
        )


def validate_minute_decision(
    decision: MinuteDecision,
    declaration: ClusterDeclaration,
) -> None:
    """Raise E_ALLOC for any cross-service or budget inconsistency."""

    if decision.budget_used > declaration.global_replica_budget:
        raise _error(
            "global replica budget was exceeded",
            decision.minute,
            None,
            budget_used=decision.budget_used,
            budget_limit=declaration.global_replica_budget,
        )
    services = declaration.service_map
    names = tuple(item.service for item in decision.services)
    if names != tuple(sorted(services)):
        raise _error(
            "service decision list must contain every service in stable name order",
            decision.minute,
            None,
            expected=list(sorted(services)),
            actual=list(names),
        )
    total_replicas = sum(item.final_replicas for item in decision.services)
    if total_replicas != decision.budget_used:
        raise _error(
            "budget_used is not the sum of final replicas",
            decision.minute,
            None,
            total_replicas=total_replicas,
            budget_used=decision.budget_used,
        )
    total_requests = sum(item.request_count for item in decision.services)
    total_accepted = sum(item.accepted_requests for item in decision.services)
    total_rejected = sum(item.rejected_requests for item in decision.services)
    if (
        decision.requests_total != total_requests
        or decision.accepted_total != total_accepted
        or decision.rejected_total != total_rejected
        or total_accepted + total_rejected != total_requests
    ):
        raise _error(
            "minute totals are not self-consistent",
            decision.minute,
            None,
            requests=total_requests,
            accepted=total_accepted,
            rejected=total_rejected,
        )
    for item in decision.services:
        validate_service_decision(item, services[item.service])


def clip_and_allocate(
    declaration: ClusterDeclaration,
    estimates: Iterable[DemandEstimate],
    minute: int,
) -> MinuteDecision:
    """Commit all per-service estimates through one stable clipping pass."""

    estimate_list = tuple(sorted(estimates, key=lambda item: item.service))
    service_map = declaration.service_map
    if tuple(item.service for item in estimate_list) != tuple(sorted(service_map)):
        raise _error(
            "demand estimates do not cover exactly the declared services",
            minute,
            None,
            expected=list(sorted(service_map)),
            actual=[item.service for item in estimate_list],
        )
    for estimate in estimate_list:
        if estimate.minute != minute:
            raise _error(
                "demand estimate belongs to another minute",
                minute,
                estimate.service,
                estimate_minute=estimate.minute,
            )
        expected = estimate_demand(service_map[estimate.service], minute)
        if estimate != expected:
            raise _error(
                "demand estimate is inconsistent with the declaration",
                minute,
                estimate.service,
            )

    quota_caps: dict[str, int] = {}
    quota_reasons: dict[str, list[ClipReason]] = {}
    for estimate in estimate_list:
        service = service_map[estimate.service]
        cap = min(estimate.demand_replicas, service.allocation_cap)
        quota_caps[estimate.service] = cap
        reasons: list[ClipReason] = []
        if cap < estimate.demand_replicas:
            reasons.append(
                ClipReason(
                    dimension="concurrency_quota",
                    before_replicas=estimate.demand_replicas,
                    after_replicas=cap,
                )
            )
        quota_reasons[estimate.service] = reasons

    requested_after_quota = sum(quota_caps.values())
    if requested_after_quota <= declaration.global_replica_budget:
        final_allocation = dict(quota_caps)
    else:
        final_allocation = _round_robin_caps(
            quota_caps,
            declaration.global_replica_budget,
        )

    decisions: list[ServiceDecision] = []
    for estimate in estimate_list:
        service = service_map[estimate.service]
        final_replicas = final_allocation[estimate.service]
        reasons = list(quota_reasons[estimate.service])
        if final_replicas < quota_caps[estimate.service]:
            reasons.append(
                ClipReason(
                    dimension="global_budget",
                    before_replicas=quota_caps[estimate.service],
                    after_replicas=final_replicas,
                )
            )
        capacity_requests = final_replicas * service.concurrency_per_replica
        accepted = min(estimate.request_count, capacity_requests)
        rejected = estimate.request_count - accepted
        if capacity_requests > 0:
            utilization = round(estimate.request_count / capacity_requests, 6)
        else:
            utilization = 0.0 if estimate.request_count == 0 else 1.0
        threshold_triggered = (
            estimate.request_count > capacity_requests * service.scale_threshold
        )
        decision = ServiceDecision(
            service=service.name,
            minute=minute,
            request_count=estimate.request_count,
            capacity_per_replica=service.concurrency_per_replica,
            unclamped_demand=estimate.unclamped_demand,
            demand_replicas=estimate.demand_replicas,
            concurrency_quota=service.concurrency_quota,
            final_replicas=final_replicas,
            capacity_requests=capacity_requests,
            accepted_requests=accepted,
            rejected_requests=rejected,
            clip_reasons=tuple(reasons),
            request_latency_ms=service.request_latency_ms,
            estimated_work_ms=estimate.request_count * service.request_latency_ms,
            utilization=utilization,
            threshold_triggered=threshold_triggered,
            min_replicas=service.min_replicas,
            max_replicas=service.max_replicas,
            scale_threshold=service.scale_threshold,
        )
        validate_service_decision(decision, service)
        decisions.append(decision)

    minute_decision = MinuteDecision(
        minute=minute,
        declaration_version=declaration.declaration_version,
        budget_limit=declaration.global_replica_budget,
        budget_requested_total=sum(item.demand_replicas for item in estimate_list),
        budget_used=sum(item.final_replicas for item in decisions),
        requests_total=sum(item.request_count for item in decisions),
        accepted_total=sum(item.accepted_requests for item in decisions),
        rejected_total=sum(item.rejected_requests for item in decisions),
        estimated_work_ms_total=sum(item.estimated_work_ms for item in decisions),
        services=tuple(decisions),
    )
    validate_minute_decision(minute_decision, declaration)
    return minute_decision

def schedule_minute(
    declaration: ClusterDeclaration,
    minute: int,
) -> MinuteDecision:
    estimates = estimate_minute_parallel(declaration.services, minute)
    return clip_and_allocate(declaration, estimates, minute)
