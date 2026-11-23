from __future__ import annotations

import heapq
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class PlannedProbe:
    domain: str
    kind: str
    cost_ms: int
    status: str
    start_offset_ms: int | None
    end_offset_ms: int | None
    reason: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "cost_ms": self.cost_ms,
            "domain": self.domain,
            "end_offset_ms": self.end_offset_ms,
            "kind": self.kind,
            "reason": self.reason,
            "start_offset_ms": self.start_offset_ms,
            "status": self.status,
        }


@dataclass(frozen=True, slots=True)
class ProbePlan:
    budget_ms: int
    max_concurrency: int
    batches: tuple[tuple[PlannedProbe, ...], ...]
    consumed_ms: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "batches": [
                [probe.as_dict() for probe in batch] for batch in self.batches
            ],
            "budget_ms": self.budget_ms,
            "consumed_ms": self.consumed_ms,
            "max_concurrency": self.max_concurrency,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ProbePlan":
        batches: list[tuple[PlannedProbe, ...]] = []
        for raw_batch in value["batches"]:
            batches.append(
                tuple(
                    PlannedProbe(
                        domain=item["domain"],
                        kind=item["kind"],
                        cost_ms=item["cost_ms"],
                        status=item["status"],
                        start_offset_ms=item["start_offset_ms"],
                        end_offset_ms=item["end_offset_ms"],
                        reason=item["reason"],
                    )
                    for item in raw_batch
                )
            )
        return cls(
            budget_ms=value["budget_ms"],
            max_concurrency=value["max_concurrency"],
            batches=tuple(batches),
            consumed_ms=value["consumed_ms"],
        )

    def probes_for_domain(self, domain: str) -> tuple[PlannedProbe, ...]:
        return tuple(
            probe
            for batch in self.batches
            for probe in batch
            if probe.domain == domain
        )

    def batch_index(self, domain: str) -> int:
        for index, batch in enumerate(self.batches):
            if any(probe.domain == domain for probe in batch):
                return index
        raise KeyError(domain)


def build_probe_plan(
    *,
    target_domains: tuple[str, ...],
    freeze_order: tuple[str, ...],
    freeze_batch_size: int,
    freeze_cost_ms: int,
    switch_cost_ms: int,
    max_concurrency: int,
    budget_ms: int,
) -> ProbePlan:
    """Create a deterministic logical-time admission plan.

    Budget admission is decided before any probe executes. Once a probe cannot
    finish inside the logical budget, it and every later probe are abandoned.
    This prevents the "label after the fact" failure mode and makes recovery
    replay use exactly the same budget decision.
    """

    target_set = set(target_domains)
    ordered_domains = [domain for domain in freeze_order if domain in target_set]
    batches: list[tuple[PlannedProbe, ...]] = []
    elapsed = 0
    exhausted = False

    for batch_start in range(0, len(ordered_domains), freeze_batch_size):
        domains = ordered_domains[
            batch_start : batch_start + freeze_batch_size
        ]
        if not domains:
            continue
        slots = [elapsed for _ in range(max_concurrency)]
        heapq.heapify(slots)
        planned: list[PlannedProbe] = []
        for domain in domains:
            for kind, cost in (
                ("freeze", freeze_cost_ms),
                ("switch", switch_cost_ms),
            ):
                start = heapq.heappop(slots)
                if exhausted:
                    planned.append(
                        PlannedProbe(
                            domain=domain,
                            kind=kind,
                            cost_ms=cost,
                            status="abandoned_budget",
                            start_offset_ms=start,
                            end_offset_ms=None,
                            reason="budget_exhausted_in_earlier_probe",
                        )
                    )
                    heapq.heappush(slots, start)
                    continue
                end = start + cost
                if end > budget_ms:
                    exhausted = True
                    planned.append(
                        PlannedProbe(
                            domain=domain,
                            kind=kind,
                            cost_ms=cost,
                            status="abandoned_budget",
                            start_offset_ms=start,
                            end_offset_ms=None,
                            reason=(
                                f"budget_insufficient: need={cost}ms, "
                                f"available={max(budget_ms - start, 0)}ms"
                            ),
                        )
                    )
                    heapq.heappush(slots, start)
                else:
                    planned.append(
                        PlannedProbe(
                            domain=domain,
                            kind=kind,
                            cost_ms=cost,
                            status="admitted",
                            start_offset_ms=start,
                            end_offset_ms=end,
                            reason=None,
                        )
                    )
                    heapq.heappush(slots, end)
        batches.append(tuple(planned))
        elapsed = max((slot for slot in slots), default=elapsed)

    return ProbePlan(
        budget_ms=budget_ms,
        max_concurrency=max_concurrency,
        batches=tuple(batches),
        consumed_ms=elapsed,
    )


def plan_from_request(value: dict[str, Any]) -> ProbePlan:
    return ProbePlan.from_dict(value)

