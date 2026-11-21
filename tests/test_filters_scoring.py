from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from scheduler_diag.filters import (
    FilterContext,
    filter_node_affinity,
    filter_pod_anti_affinity,
    filter_resource_fit,
    filter_taint_tolerance,
    filter_topology_spread,
)
from scheduler_diag.models import (
    AffinityExpression,
    AffinityRule,
    PodAntiAffinityRule,
    PodSpec,
    SCORE_PLUGINS,
    Toleration,
    TopologySpreadConstraint,
)
from scheduler_diag.scoring import score_nodes
from scheduler_diag.service import load_workspace, schedule_workspace

from tests.helpers import SAMPLE_DIR, entries_by_pod


def _pod(**overrides) -> PodSpec:
    values = {
        "name": "pod-a",
        "cpu_millicores": 1000,
        "memory_mib": 1024,
        "priority": 1,
        "arrival_round": 1,
    }
    values.update(overrides)
    return PodSpec(**values)


def _node(
    name: str = "node-a",
    *,
    cpu: int = 4000,
    memory: int = 8192,
    cpu_remaining: int | None = None,
    memory_remaining: int | None = None,
    labels: dict[str, str] | None = None,
    taints: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "name": name,
        "cpu_total": cpu,
        "memory_total": memory,
        "cpu_remaining": cpu if cpu_remaining is None else cpu_remaining,
        "memory_remaining": memory if memory_remaining is None else memory_remaining,
        "labels": labels or {"zone": "a", "disk": "ssd"},
        "taints": taints or [],
    }


class FilterTests(unittest.TestCase):
    def test_resource_fit_uses_integer_remainder(self) -> None:
        pod = _pod(cpu_millicores=1000)
        node = _node(cpu_remaining=999)
        rejection = filter_resource_fit(
            FilterContext(pod=pod, node=node, nodes=[node], allocations=[])
        )
        self.assertIsNotNone(rejection)
        assert rejection is not None
        self.assertEqual(rejection.plugin, "ResourceFit")
        self.assertEqual(
            rejection.evidence["deficit_millicores"],
            1,
        )
        self.assertEqual(rejection.source_field, "pods[pod-a].cpu_millicores")

    def test_taint_tolerance(self) -> None:
        node = _node(
            taints=[{"key": "dedicated", "value": "gpu", "effect": "NoSchedule"}]
        )
        rejection = filter_taint_tolerance(
            FilterContext(pod=_pod(), node=node, nodes=[node], allocations=[])
        )
        self.assertIsNotNone(rejection)
        assert rejection is not None
        self.assertEqual(rejection.evidence["taint"]["key"], "dedicated")
        tolerated = _pod(
            tolerations=(
                Toleration(
                    key="dedicated",
                    operator="Equal",
                    value="gpu",
                    effect="NoSchedule",
                ),
            )
        )
        self.assertIsNone(
            filter_taint_tolerance(
                FilterContext(
                    pod=tolerated,
                    node=node,
                    nodes=[node],
                    allocations=[],
                )
            )
        )

    def test_node_affinity_and_node_anti_affinity(self) -> None:
        pod = _pod(
            node_affinity=AffinityRule(
                required=(
                    AffinityExpression("zone", "In", ("b",)),
                )
            )
        )
        node = _node(labels={"zone": "a"})
        rejection = filter_node_affinity(
            FilterContext(pod=pod, node=node, nodes=[node], allocations=[])
        )
        self.assertIsNotNone(rejection)
        anti_pod = _pod(
            node_anti_affinity=AffinityRule(
                required=(
                    AffinityExpression("zone", "In", ("a",)),
                )
            )
        )
        anti_rejection = filter_node_affinity(
            FilterContext(
                pod=anti_pod,
                node=node,
                nodes=[node],
                allocations=[],
            )
        )
        self.assertIsNotNone(anti_rejection)
        assert anti_rejection is not None
        self.assertEqual(
            anti_rejection.error_code,
            "E_FILTER_NODE_ANTI_AFFINITY",
        )
        absent_is_not_in = _pod(
            node_affinity=AffinityRule(
                required=(
                    AffinityExpression("missing-label", "NotIn", ("x",)),
                )
            )
        )
        self.assertIsNone(
            filter_node_affinity(
                FilterContext(
                    pod=absent_is_not_in,
                    node=node,
                    nodes=[node],
                    allocations=[],
                )
            )
        )
        greater_than = _pod(
            node_affinity=AffinityRule(
                required=(
                    AffinityExpression("capacity", "Gt", ("2",)),
                )
            )
        )
        capacity_node = _node(labels={"zone": "a", "capacity": "3"})
        self.assertIsNone(
            filter_node_affinity(
                FilterContext(
                    pod=greater_than,
                    node=capacity_node,
                    nodes=[capacity_node],
                    allocations=[],
                )
            )
        )

    def test_pod_anti_affinity(self) -> None:
        node = _node(labels={"zone": "a"})
        pod = _pod(
            pod_anti_affinity=PodAntiAffinityRule(
                label_selector={"app": "web"},
                topology_key="zone",
            )
        )
        allocations = [
            {
                "round": 1,
                "pod": "existing",
                "node": "node-a",
                "labels": {"app": "web"},
            }
        ]
        rejection = filter_pod_anti_affinity(
            FilterContext(
                pod=pod,
                node=node,
                nodes=[node],
                allocations=allocations,
            )
        )
        self.assertIsNotNone(rejection)
        assert rejection is not None
        self.assertEqual(rejection.evidence["conflicting_pod"], "existing")

    def test_topology_spread(self) -> None:
        nodes = [
            _node("node-a", labels={"zone": "a"}),
            _node("node-b", labels={"zone": "b"}),
        ]
        pod = _pod(
            topology_spread=(
                TopologySpreadConstraint(
                    max_skew=0,
                    topology_key="zone",
                    when_unsatisfiable="DoNotSchedule",
                    label_selector={"app": "api"},
                ),
            )
        )
        allocations = [
            {
                "round": 1,
                "pod": "api-1",
                "node": "node-a",
                "labels": {"app": "api"},
            }
        ]
        rejection = filter_topology_spread(
            FilterContext(
                pod=pod,
                node=nodes[0],
                nodes=nodes,
                allocations=allocations,
            )
        )
        self.assertIsNotNone(rejection)
        assert rejection is not None
        self.assertEqual(rejection.evidence["skew_after_placement"], 2)


class ScoringTests(unittest.TestCase):
    def test_tie_break_is_node_name(self) -> None:
        nodes = [
            _node("node-b", labels={"zone": "a"}, cpu=4000, memory=8192),
            _node("node-a", labels={"zone": "a"}, cpu=4000, memory=8192),
        ]
        pod = _pod()
        contexts = [
            FilterContext(pod=pod, node=node, nodes=nodes, allocations=[])
            for node in nodes
        ]
        weights = {
            "ResourceBinPack": Decimal("2.5"),
            "NodeAffinityMatch": Decimal("1.25"),
            "TopologySpreadBalance": Decimal("1.75"),
        }
        result = score_nodes(contexts, weights)
        self.assertEqual(result["_ranking_order"], ["node-a", "node-b"])
        self.assertFalse(result["weight_sum_is_integer"])
        self.assertEqual(result["quantization"]["error_upper_bound"], "0.0000015")
        for node_score in result["ranking"]:
            self.assertEqual(set(node_score["raw_scores"]), set(SCORE_PLUGINS))
            self.assertEqual(set(node_score["normalized_terms"]), set(SCORE_PLUGINS))
            self.assertEqual(node_score["weights"]["ResourceBinPack"], "2.5")

    def test_sample_scoring_and_integer_bound_resources(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "state.sqlite"
            load_workspace(
                bundle_dir=SAMPLE_DIR,
                db_path=database,
                reset=True,
            )
            schedule_workspace(
                bundle_dir=SAMPLE_DIR,
                db_path=database,
            )
            entries = entries_by_pod(database)
            control = entries["r1-web-control"]
            self.assertEqual(control["status"], "bound")
            self.assertEqual(
                control["before_resources"]["cpu_millicores"]
                - control["after_resources"]["cpu_millicores"],
                500,
            )
            self.assertEqual(
                control["before_resources"]["memory_mib"]
                - control["after_resources"]["memory_mib"],
                512,
            )
            scoring = control["scoring"]
            self.assertEqual(scoring["weight_sum"], "5.500000")
            self.assertFalse(scoring["weight_sum_is_integer"])
            self.assertEqual(
                scoring["quantization"]["error_upper_bound"],
                "0.0000015",
            )
            for node_score in scoring["ranking"]:
                self.assertEqual(set(node_score["raw_scores"]), set(SCORE_PLUGINS))
                self.assertEqual(
                    set(node_score["normalized_terms"]),
                    set(SCORE_PLUGINS),
                )
                self.assertRegex(node_score["total_score"], r"^-?\d+\.\d{6}$")
                self.assertEqual(
                    sum(
                        Decimal(term)
                        for term in node_score["normalized_terms"].values()
                    ),
                    Decimal(node_score["total_score"]),
                )
            totals = [Decimal(item["total_score"]) for item in scoring["ranking"]]
            self.assertEqual(totals, sorted(totals, reverse=True))
            for elimination in entries["r1-web-control"]["eliminations"]:
                self.assertTrue(elimination["plugin"])
                self.assertTrue(elimination["node"])
                self.assertTrue(elimination["evidence"])
                self.assertTrue(elimination["source_field"])


if __name__ == "__main__":
    unittest.main()
