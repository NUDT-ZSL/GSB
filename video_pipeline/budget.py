"""Deterministic priority/fairness-aware frame budget allocation."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RouteBudgetInput:
    route: str
    priority: int
    route_frame_budget: int
    source_frame_count: int


@dataclass(frozen=True)
class RouteBudget:
    route: str
    priority: int
    route_frame_budget: int
    source_frame_count: int
    eligible_frames: int
    allocated_frames: int

    @property
    def truncated(self) -> bool:
        return self.source_frame_count > 0 and self.allocated_frames < self.source_frame_count

    @property
    def truncated_frame(self) -> int | None:
        return self.allocated_frames if self.truncated else None


def allocate_frame_budget(
    routes: list[RouteBudgetInput],
    total_frame_budget: int,
    already_consumed: dict[str, int] | None = None,
) -> dict[str, RouteBudget]:
    """Allocate frames deterministically by priority, then least-consumed route.

    Higher priority routes are filled first. Routes at the same priority are
    balanced by current allocation, using route name as the final stable tie
    breaker. The result depends only on the supplied counters, never wall-clock
    thread scheduling. ``already_consumed`` is used when replanning after a
    restored run.
    """

    consumed = already_consumed or {}
    allocations = {route.route: 0 for route in routes}
    eligible = {
        route.route: max(0, min(int(route.source_frame_count), int(route.route_frame_budget)))
        for route in routes
    }
    # Previously consumed frames already occupy the global budget on resume.
    consumed_total = 0
    for route_name, count in consumed.items():
        if route_name in allocations:
            allocations[route_name] = max(0, min(int(count), eligible[route_name]))
            consumed_total += allocations[route_name]
    remaining = max(0, int(total_frame_budget) - consumed_total)

    while remaining > 0:
        pending = [
            route
            for route in routes
            if allocations[route.route] < eligible[route.route]
        ]
        if not pending:
            break
        selected = min(
            pending,
            key=lambda route: (-route.priority, allocations[route.route], route.route),
        )
        allocations[selected.route] += 1
        remaining -= 1

    return {
        route.route: RouteBudget(
            route=route.route,
            priority=route.priority,
            route_frame_budget=route.route_frame_budget,
            source_frame_count=route.source_frame_count,
            eligible_frames=eligible[route.route],
            allocated_frames=allocations[route.route],
        )
        for route in routes
    }
