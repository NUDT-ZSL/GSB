from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


class PerfClock:
    name = "perf_counter_ns"
    reproducible = False

    def now_ns(self) -> int:
        return time.perf_counter_ns()

    def advance(self, nanoseconds: int) -> None:
        return None


class LogicalClock:
    name = "logical_work_units"
    reproducible = True

    def __init__(self, start_ns: int = 0) -> None:
        self._now = int(start_ns)

    def now_ns(self) -> int:
        return self._now

    def advance(self, nanoseconds: int) -> None:
        self._now += max(0, int(nanoseconds))


QUERY_COST_MODEL = {
    "base_ns": 25_000,
    "per_row_ns": 8,
    "per_feature_ns": 45,
    "per_retained_feature_ns": 7,
}


def estimate_query_latency_ns(row_count: int, feature_count: int, retained_count: int) -> int:
    return int(
        QUERY_COST_MODEL["base_ns"]
        + QUERY_COST_MODEL["per_row_ns"] * row_count
        + QUERY_COST_MODEL["per_feature_ns"] * feature_count
        + QUERY_COST_MODEL["per_retained_feature_ns"] * retained_count
    )


@dataclass
class StageRecorder:
    clock: PerfClock | LogicalClock = field(default_factory=PerfClock)
    stages: list[dict[str, Any]] = field(default_factory=list)

    def record(
        self,
        stage: str,
        *,
        work_units: int,
        unit_kind: str,
        actual_ns: int,
        logical_cost_ns: int,
    ) -> None:
        self.stages.append(
            {
                "stage": stage,
                "work_units": int(work_units),
                "unit_kind": unit_kind,
                "actual_ns": int(actual_ns),
                "logical_ns": int(logical_cost_ns),
            }
        )

    def deterministic_view(self) -> list[dict[str, Any]]:
        return [
            {
                "stage": event["stage"],
                "work_units": event["work_units"],
                "unit_kind": event["unit_kind"],
                "duration_ns": event["logical_ns"],
            }
            for event in self.stages
        ]

    def actual_view(self) -> list[dict[str, Any]]:
        return sorted(
            [
                {
                    "stage": event["stage"],
                    "work_units": event["work_units"],
                    "unit_kind": event["unit_kind"],
                    "actual_ns": event["actual_ns"],
                }
                for event in self.stages
            ],
            key=lambda item: item["stage"],
        )


def logical_cost(units: int, nanos_per_unit: int = 10) -> int:
    return max(1, int(units)) * int(nanos_per_unit)
