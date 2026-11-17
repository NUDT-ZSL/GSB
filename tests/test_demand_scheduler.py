from __future__ import annotations

import dataclasses
import unittest
from pathlib import Path

from model_service_workbench.declaration import load_declaration
from model_service_workbench.demand import (
    estimate_demand,
    estimate_minute_parallel,
)
from model_service_workbench.errors import AllocationError, DemandError
from model_service_workbench.models import canonical_json
from model_service_workbench.scheduler import (
    clip_and_allocate,
    schedule_minute,
    validate_minute_decision,
    validate_service_decision,
)


ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "samples" / "cluster.json"


class DemandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.declaration = load_declaration(SAMPLE)
        self.alpha = self.declaration.service_map["alpha"]

    def test_integer_intermediates_and_clamp(self) -> None:
        estimate = estimate_demand(self.alpha, 3)
        self.assertEqual(estimate.request_count, 22)
        self.assertEqual(estimate.capacity_per_replica, 6)
        self.assertEqual(estimate.unclamped_demand, 4)
        self.assertEqual(estimate.demand_replicas, 4)

    def test_estimate_is_repeatable(self) -> None:
        values = {
            canonical_json(estimate_demand(self.alpha, 4).to_dict())
            for _ in range(100)
        }
        self.assertEqual(len(values), 1)

    def test_minimum_replicas_clamps_zero_load(self) -> None:
        estimate = estimate_demand(self.alpha, 0)
        self.assertEqual(estimate.unclamped_demand, 0)
        self.assertEqual(estimate.demand_replicas, 1)

    def test_missing_input_is_e_demand(self) -> None:
        with self.assertRaises(DemandError) as context:
            estimate_demand(
                {
                    "name": "broken",
                    "requests": [1],
                    "min_replicas": 0,
                    "max_replicas": 2,
                    "scale_threshold": 0.8,
                },
                0,
            )
        self.assertEqual(context.exception.code, "E_DEMAND")
        self.assertIn("concurrency_per_replica", context.exception.path)
        self.assertIn(
            "concurrency_per_replica",
            context.exception.details["missing_inputs"],
        )

    def test_missing_minute_is_e_demand(self) -> None:
        with self.assertRaises(DemandError) as context:
            estimate_demand(self.alpha, 99)
        self.assertEqual(context.exception.code, "E_DEMAND")
        self.assertEqual(context.exception.path, "$.services[alpha].requests[99]")

    def test_parallel_estimation_is_order_independent(self) -> None:
        ordered = estimate_minute_parallel(self.declaration.services, 3)
        shuffled = estimate_minute_parallel(
            tuple(reversed(self.declaration.services)),
            3,
        )
        self.assertEqual(ordered, shuffled)
        self.assertEqual(tuple(item.service for item in ordered), ("alpha", "beta", "gamma"))


class SchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.declaration = load_declaration(SAMPLE)

    def test_budget_and_service_quota_are_hard_constraints(self) -> None:
        decision = schedule_minute(self.declaration, 4)
        self.assertEqual(decision.budget_used, 10)
        self.assertEqual(decision.budget_used, sum(item.final_replicas for item in decision.services))
        for item in decision.services:
            service = self.declaration.service_map[item.service]
            self.assertLessEqual(item.final_replicas, service.concurrency_quota)
            self.assertLessEqual(item.final_replicas, service.max_replicas)
            self.assertGreaterEqual(item.rejected_requests, 0)
            self.assertLessEqual(item.rejected_requests, item.request_count)

    def test_clipping_reasons_include_dimensions_and_values(self) -> None:
        decision = schedule_minute(self.declaration, 4)
        beta = next(item for item in decision.services if item.service == "beta")
        reasons = [reason.to_dict() for reason in beta.clip_reasons]
        self.assertEqual(
            reasons,
            [
                {
                    "dimension": "concurrency_quota",
                    "before_replicas": 7,
                    "after_replicas": 6,
                },
                {
                    "dimension": "global_budget",
                    "before_replicas": 6,
                    "after_replicas": 3,
                },
            ],
        )

    def test_clipping_is_independent_of_estimate_order(self) -> None:
        from model_service_workbench.demand import estimate_minute_parallel

        estimates = estimate_minute_parallel(self.declaration.services, 4)
        normal = clip_and_allocate(self.declaration, estimates, 4)
        shuffled = clip_and_allocate(
            self.declaration,
            tuple(reversed(estimates)),
            4,
        )
        self.assertEqual(normal, shuffled)

    def test_allocation_error_identifies_service_and_minute(self) -> None:
        decision = schedule_minute(self.declaration, 2)
        alpha = next(item for item in decision.services if item.service == "alpha")
        bad = dataclasses.replace(alpha, accepted_requests=0)
        with self.assertRaises(AllocationError) as context:
            validate_service_decision(
                bad,
                self.declaration.service_map["alpha"],
            )
        self.assertEqual(context.exception.code, "E_ALLOC")
        self.assertEqual(context.exception.details["minute"], 2)
        self.assertEqual(context.exception.details["service"], "alpha")
        self.assertIn("$.minutes[2].services[alpha]", context.exception.path)

    def test_minute_budget_error_is_e_alloc(self) -> None:
        decision = schedule_minute(self.declaration, 2)
        bad = dataclasses.replace(decision, budget_used=11)
        with self.assertRaises(AllocationError) as context:
            validate_minute_decision(bad, self.declaration)
        self.assertEqual(context.exception.code, "E_ALLOC")
        self.assertEqual(context.exception.details["minute"], 2)


if __name__ == "__main__":
    unittest.main()
