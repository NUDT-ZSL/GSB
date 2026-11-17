"""Deterministic request-level budget and quota planning."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import PipelineConfig
from .errors import (
    E_BUDGET_EXHAUSTED,
    E_REQ_QUOTA_EXCEEDED,
    FailureRecord,
)
from .models import RequestData


@dataclass
class RequestPlan:
    request: RequestData | None
    request_id: str
    status: str
    points: tuple[str, ...]
    failure: FailureRecord | None
    budget_events: list[dict[str, Any]]
    ready_queue_position: int | None = None


@dataclass
class BudgetPlan:
    plans: list[RequestPlan]
    global_budget: int
    per_request_quota: int
    consumed: int
    remaining: int
    exhausted: bool
    curve: list[dict[str, Any]]


def plan_budget(
    requests: dict[str, RequestData],
    cfg: PipelineConfig,
) -> BudgetPlan:
    remaining = cfg.global_budget
    plans: list[RequestPlan] = []
    curve: list[dict[str, Any]] = []
    exhausted = False
    sequence = 0

    for request_id in sorted(requests):
        request = requests[request_id]
        candidate_ids = [candidate.candidate_id for candidate in request.candidates]
        ready_position = len(plans) + 1
        if remaining <= 0:
            exhausted = True
            failure = FailureRecord(
                request_id=request_id,
                stage="schedule",
                code=E_BUDGET_EXHAUSTED,
                exception_type="BudgetExhausted",
                message="global scoring budget is exhausted before this request",
                injection_ids=[],
                ready_queue_position=ready_position,
            )
            plans.append(
                RequestPlan(
                    request=request,
                    request_id=request_id,
                    status="budget_exhausted",
                    points=(),
                    failure=failure,
                    budget_events=[],
                    ready_queue_position=ready_position,
                )
            )
            continue

        process_count = min(len(candidate_ids), cfg.per_request_quota, remaining)
        status = "success"
        failure = None
        event_result = "scored"
        if process_count < len(candidate_ids):
            if process_count == remaining:
                status = "budget_exhausted"
                exhausted = True
                event_result = "attempted_before_budget_abort"
                failure = FailureRecord(
                    request_id=request_id,
                    stage="schedule",
                    code=E_BUDGET_EXHAUSTED,
                    exception_type="BudgetExhausted",
                    message=(
                        f"global budget ended after {process_count} of "
                        f"{len(candidate_ids)} candidates"
                    ),
                    injection_ids=[],
                    ready_queue_position=ready_position,
                )
            else:
                status = "quota_exceeded"
                event_result = "attempted_before_quota_abort"
                failure = FailureRecord(
                    request_id=request_id,
                    stage="schedule",
                    code=E_REQ_QUOTA_EXCEEDED,
                    exception_type="QuotaExceeded",
                    message=(
                        f"request candidates {len(candidate_ids)} exceed per-request "
                        f"quota {cfg.per_request_quota}"
                    ),
                    injection_ids=[],
                    ready_queue_position=ready_position,
                )

        budget_events: list[dict[str, Any]] = []
        for candidate_id in candidate_ids[:process_count]:
            sequence += 1
            remaining_before = remaining
            remaining -= 1
            event = {
                "sequence": sequence,
                "request_id": request_id,
                "candidate_id": candidate_id,
                "remaining_before": remaining_before,
                "remaining_after": remaining,
                "result": event_result if status == "success" else event_result,
            }
            budget_events.append(event)
            curve.append(event)
        if remaining <= 0:
            exhausted = True

        plans.append(
            RequestPlan(
                request=request,
                request_id=request_id,
                status=status,
                points=tuple(candidate_ids[:process_count]),
                failure=failure,
                budget_events=budget_events,
                ready_queue_position=ready_position,
            )
        )

    return BudgetPlan(
        plans=plans,
        global_budget=cfg.global_budget,
        per_request_quota=cfg.per_request_quota,
        consumed=len(curve),
        remaining=remaining,
        exhausted=exhausted,
        curve=curve,
    )
