from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ticket_gate.graph import affected_gate_ids, build_plan
from ticket_gate.parser import load_project


class PlanningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.project = load_project(ROOT / "sample_project")
        cls.plan = build_plan(cls.project)

    def test_affected_gates_are_exactly_changed_event_subscribers(self) -> None:
        expected = {
            gate.gate_id
            for gate in self.project.gates.values()
            if set(gate.subscriptions).intersection(self.project.changes)
        }
        self.assertEqual(set(self.plan.affected_gate_ids), expected)
        self.assertEqual(len(expected), 18)

    def test_batches_partition_affected_gates_and_respect_dependencies(self) -> None:
        flattened = [
            gate_id for batch in self.plan.batches for gate_id in batch.gate_ids
        ]
        self.assertEqual(len(flattened), len(set(flattened)))
        self.assertEqual(set(flattened), self.plan.affected_set)

        batch_by_gate = {
            gate_id: batch.batch_no
            for batch in self.plan.batches
            for gate_id in batch.gate_ids
        }
        cycle_gates = {gate_id for cycle in self.plan.cycles for gate_id in cycle}
        for edge in self.plan.dependencies:
            upstream_batch = batch_by_gate[edge.upstream_gate_id]
            downstream_batch = batch_by_gate[edge.downstream_gate_id]
            self.assertLessEqual(upstream_batch, downstream_batch)
            if upstream_batch == downstream_batch:
                self.assertIn(edge.upstream_gate_id, cycle_gates)
                self.assertIn(edge.downstream_gate_id, cycle_gates)

        self.assertEqual(len(self.plan.batches), 9)
        self.assertEqual(len(self.plan.cycles), 1)
        cycle = set(self.plan.cycles[0])
        cycle_batches = {
            batch.batch_no for batch in self.plan.batches if cycle.intersection(batch.gate_ids)
        }
        self.assertEqual(len(cycle_batches), 1)
        for batch in self.plan.batches:
            if batch.cycle_gate_ids:
                self.assertEqual(set(batch.cycle_gate_ids), cycle)

    def test_multi_event_conflict_is_reported_with_source_fields(self) -> None:
        conflict_gates = {conflict.gate_id for conflict in self.plan.conflicts}
        self.assertIn("GATE-010", conflict_gates)
        gate_conflict = next(
            conflict for conflict in self.plan.conflicts if conflict.gate_id == "GATE-010"
        )
        self.assertEqual(gate_conflict.event_ids, ("EVT-10", "EVT-11"))
        self.assertIn("reentry_policy", gate_conflict.fields)
        for paths in gate_conflict.sources.values():
            self.assertEqual(set(paths), {"EVT-10", "EVT-11"})
            self.assertTrue(all(path.startswith("config/events.json#") for path in paths.values()))

    def test_plan_is_deterministic(self) -> None:
        first = build_plan(self.project).to_dict()
        second = build_plan(self.project).to_dict()
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
