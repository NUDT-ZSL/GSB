from __future__ import annotations

import unittest

from elastic_control.demand import choose_target_replicas
from elastic_control.models import (
    NodePool,
    NodeSpec,
    ServiceSpec,
    WorkloadWindow,
)
from elastic_control.packing import PackingState, place_replica


def service(
    *,
    name: str = "svc",
    memory: int = 6000,
    compute: int = 10,
    min_replicas: int = 0,
    max_replicas: int = 8,
    throughput: float = 1000.0,
    domains: tuple[str, ...] = (),
) -> ServiceSpec:
    return ServiceSpec(
        name=name,
        model_version="v1",
        memory_mib_per_replica=memory,
        compute_units_per_replica=compute,
        min_replicas=min_replicas,
        max_replicas=max_replicas,
        cold_start_ticks=1,
        batch_window_ms=10,
        throughput_tokens_per_tick=throughput,
        throughput_source="explicit",
        max_concurrent_launches=2,
        allowed_topology_domains=domains,
    )


class DemandTests(unittest.TestCase):
    def test_demand_is_repeatable_and_ceil_is_recorded(self) -> None:
        svc = service(min_replicas=1, max_replicas=8, throughput=1000)
        window = WorkloadWindow(tick=4, arrival_rate=3.3, avg_sequence_length=900.0)
        outputs = [choose_target_replicas(svc, window).to_dict() for _ in range(100)]
        self.assertTrue(all(item == outputs[0] for item in outputs))
        decision = outputs[0]
        self.assertEqual(decision["estimated_replicas_before_clamp"], 3)
        self.assertIn("ROUND_CEILING", decision["aggregation_order"][2])
        self.assertEqual(decision["target_replicas"], 3)
        self.assertEqual(decision["clamp"]["applied"], "none")

    def test_demand_clamps_to_bounds(self) -> None:
        low = choose_target_replicas(
            service(min_replicas=2, max_replicas=5),
            WorkloadWindow(tick=0, arrival_rate=0.0, avg_sequence_length=1000.0),
        )
        high = choose_target_replicas(
            service(min_replicas=1, max_replicas=4),
            WorkloadWindow(tick=0, arrival_rate=20.0, avg_sequence_length=1000.0),
        )
        self.assertEqual(low.target_replicas, 2)
        self.assertEqual(low.clamp["applied"], "min_replicas")
        self.assertEqual(high.target_replicas, 4)
        self.assertEqual(high.clamp["applied"], "max_replicas")


class PackingTests(unittest.TestCase):
    def test_card_level_free_memory_and_rejections_are_recorded(self) -> None:
        pool = NodePool(
            nodes=(
                NodeSpec("n0", 2, 8000, 2, "rack-a"),
                NodeSpec("n1", 1, 5000, 1, "rack-b"),
            ),
            compute_units_capacity=100,
            max_replicas_per_domain_per_service=None,
        )
        state = PackingState.empty(pool)
        svc = service(memory=6000, compute=10)
        first = place_replica(state, svc, "svc--0001")
        second = place_replica(state, svc, "svc--0002")
        third = place_replica(state, svc, "svc--0003")
        self.assertIsNotNone(first.placement)
        self.assertIsNotNone(second.placement)
        self.assertIsNone(third.placement)
        self.assertEqual(third.report["first_constraint"], "node_instance_limit")
        self.assertEqual(state.gpu_free_mib[("n0", 0)], 2000)
        self.assertEqual(state.gpu_free_mib[("n0", 1)], 2000)
        self.assertEqual(first.report["gpu_free_mib"]["n0"], [2000, 8000])
        self.assertEqual(second.report["gpu_free_mib"]["n0"], [2000, 2000])

    def test_memory_shortage_never_partially_places(self) -> None:
        pool = NodePool(
            nodes=(NodeSpec("n0", 1, 5000, 4, "rack-a"),),
            compute_units_capacity=100,
            max_replicas_per_domain_per_service=None,
        )
        result = place_replica(
            PackingState.empty(pool),
            service(memory=6000),
            "svc--0001",
        )
        self.assertIsNone(result.placement)
        self.assertEqual(result.report["first_constraint"], "gpu_memory")
        self.assertEqual(result.report["gpu_free_mib"], {"n0": [5000]})

    def test_topology_constraints(self) -> None:
        pool = NodePool(
            nodes=(
                NodeSpec("n0", 1, 8000, 4, "rack-a"),
                NodeSpec("n1", 1, 8000, 4, "rack-b"),
            ),
            compute_units_capacity=100,
            max_replicas_per_domain_per_service=1,
        )
        state = PackingState.empty(pool)
        svc = service(memory=1000, domains=("rack-b",))
        first = place_replica(state, svc, "svc--0001")
        second = place_replica(state, svc, "svc--0002")
        self.assertEqual(first.placement.topology_domain, "rack-b")
        self.assertIsNone(second.placement)
        self.assertEqual(second.report["first_constraint"], "topology_not_allowed")
        n0 = next(item for item in second.report["candidates"] if item["node_name"] == "n0")
        self.assertEqual(n0["first_constraint"], "topology_not_allowed")
        n1 = next(item for item in second.report["candidates"] if item["node_name"] == "n1")
        self.assertEqual(n1["first_constraint"], "topology_domain_limit")

    def test_compute_budget_rejects_whole_replica(self) -> None:
        pool = NodePool(
            nodes=(NodeSpec("n0", 1, 8000, 4, "rack-a"),),
            compute_units_capacity=15,
            max_replicas_per_domain_per_service=None,
        )
        state = PackingState.empty(pool)
        svc = service(memory=1000, compute=10)
        self.assertIsNotNone(place_replica(state, svc, "svc--0001").placement)
        rejected = place_replica(state, svc, "svc--0002")
        self.assertIsNone(rejected.placement)
        self.assertEqual(rejected.report["first_constraint"], "compute_budget")


if __name__ == "__main__":
    unittest.main()

