from __future__ import annotations

from replica_ledger.analysis import analyze_local
from replica_ledger.consensus import determine_truth
from replica_ledger.planner import build_repair_plan
from replica_ledger.spec import load_replica_group

from tests.common import SampleWorkspace


class ConsensusAndPlannerTests(SampleWorkspace):
    def test_single_replica_mismatch_and_repair(self) -> None:
        spec = load_replica_group(self.workspace / "scenarios/single/spec.json")
        result = analyze_local(spec, 8)
        decision = result.consensus.decisions["B-SINGLE"]
        self.assertEqual(decision.truth_nodes, ("n1", "n2", "n3"))
        self.assertEqual(decision.mismatch_nodes, ("n4",))
        self.assertTrue(decision.strict_majority)
        self.assertEqual(decision.ratio.ratio.value, 2)
        self.assertEqual(decision.ratio.scale, 8)
        self.assertEqual(decision.ratio.gap_error_bound_units, 1)
        self.assertEqual(len(result.plan.tasks), 1)
        task = result.plan.tasks[0]
        self.assertEqual((task.source_node_id, task.target_node_id), ("n1", "n4"))
        self.assertGreater(task.transfer.value, 0)
        self.assertLessEqual(task.transfer.error_bound_units, 1)
        self.assertGreater(task.workload.value, 0)
        self.assertLessEqual(task.workload.error_bound_units, 1)
        self.assertGreater(task.cost_error_bound_units, 0)

    def test_multi_replica_majority_and_after_state(self) -> None:
        spec = load_replica_group(self.workspace / "scenarios/multi/spec.json")
        result = analyze_local(spec, 8)
        decision = result.consensus.decisions["B-MULTI"]
        self.assertEqual(decision.mismatch_nodes, ("n5", "n6"))
        self.assertEqual(decision.replica_count, 6)
        self.assertEqual(len(decision.truth_nodes), 4)
        self.assertEqual(len(result.plan.tasks), 2)
        after = determine_truth(spec, result.plan.after_replicas, 8)
        self.assertTrue(
            all(item.mismatch_count == 0 for item in after.decisions.values())
        )

    def test_priority_tie_break(self) -> None:
        spec = load_replica_group(self.workspace / "scenarios/grid/spec.json")
        result = analyze_local(spec, 8)
        decision = result.consensus.decisions["B-TIE"]
        self.assertEqual(decision.replica_count, 6)
        self.assertTrue(decision.tie_break_applied)
        self.assertEqual(decision.truth_nodes, ("n1", "n2", "n3"))
        self.assertEqual(decision.mismatch_nodes, ("n4", "n5", "n6"))

    def test_node_id_tuple_tie_break_when_frequency_and_priority_tie(self) -> None:
        root = self.copy_scenario("grid")
        path = root / "spec.json"
        document = self.read_json(path)
        for node in document["nodes"]:
            node["priority"] = 5
        self.write_json(path, document)
        spec = load_replica_group(path)
        result = analyze_local(spec, 8)
        decision = result.consensus.decisions["B-TIE"]
        self.assertTrue(decision.tie_break_applied)
        self.assertEqual(decision.tie_break_stage, "node_id")
        self.assertEqual(decision.truth_nodes, ("n1", "n2", "n3"))
        self.assertEqual(decision.mismatch_nodes, ("n4", "n5", "n6"))

    def test_reversed_state_order_is_identical(self) -> None:
        spec = load_replica_group(self.workspace / "scenarios/multi/spec.json")
        result = analyze_local(spec, 8)
        reversed_consensus = determine_truth(
            spec, tuple(reversed(result.consensus.replicas)), 8
        )
        reversed_plan = build_repair_plan(spec, reversed_consensus)
        self.assertEqual(
            [item.to_dict() for item in result.plan.tasks],
            [item.to_dict() for item in reversed_plan.tasks],
        )
        self.assertEqual(result.group_state_digest, reversed_consensus.state_digest)
        for block_id in result.consensus.decisions:
            self.assertEqual(
                result.consensus.decisions[block_id].truth_digest,
                reversed_consensus.decisions[block_id].truth_digest,
            )
            self.assertEqual(
                result.consensus.decisions[block_id].mismatch_nodes,
                reversed_consensus.decisions[block_id].mismatch_nodes,
            )
