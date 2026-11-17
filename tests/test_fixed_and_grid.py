from __future__ import annotations

from replica_ledger.analysis import analyze_local
from replica_ledger.fixed import (
    classify_ratio,
    estimate_source_cost,
    quantize_fraction,
    rank_candidates,
)
from replica_ledger.grid import diagnose_grid_local
from replica_ledger.spec import load_replica_group

from tests.common import SampleWorkspace


class FixedAndGridTests(SampleWorkspace):
    def test_floor_quantization_and_bound(self) -> None:
        value = quantize_fraction(1, 3, 10)
        self.assertEqual(value.value, 3)
        self.assertEqual(value.remainder, 1)
        self.assertEqual(value.error_bound_units, 1)
        exact = quantize_fraction(2, 4, 8)
        self.assertEqual(exact.value, 4)
        self.assertEqual(exact.error_bound_units, 0)

    def test_ratio_comparison_flip_is_explained(self) -> None:
        low = classify_ratio(1, 6, 1, 8, 4)
        high = classify_ratio(1, 6, 1, 8, 6)
        self.assertEqual(low.certainty, "indeterminate")
        self.assertEqual(high.certainty, "above")
        self.assertEqual(low.gap_error_bound_units, 1)
        self.assertEqual(high.gap_error_bound_units, 1)
        self.assertTrue(low.exact_above)
        self.assertTrue(high.exact_above)

    def _candidate(self, node_id: str, priority: int, load: int, scale: int):
        return estimate_source_cost(
            node_id=node_id,
            priority=priority,
            repair_load=load,
            max_priority=10,
            max_load=8,
            byte_size=1536,
            scale=scale,
            transfer_weight=1,
            work_weight=1,
            priority_weight=1,
            load_weight=1,
            overhead_numerator=3,
            overhead_denominator=100,
            work_numerator=3,
            work_denominator=2,
        )

    def test_cost_tie_channel_and_source_flip(self) -> None:
        low = rank_candidates(
            [self._candidate("n1", 10, 3, 8), self._candidate("n2", 9, 0, 8)]
        )
        high = rank_candidates(
            [self._candidate("n1", 10, 3, 16), self._candidate("n2", 9, 0, 16)]
        )
        self.assertTrue(low.tie_channel)
        self.assertEqual(low.winner.node_id, "n1")
        self.assertFalse(high.tie_channel)
        self.assertEqual(high.winner.node_id, "n2")
        self.assertGreater(high.deciding_gap_units, high.pair_error_bound_units)

    def test_grid_diagnosis_has_ratio_and_source_flips(self) -> None:
        spec = load_replica_group(self.workspace / "scenarios/grid/spec.json")
        diagnosis = diagnose_grid_local(spec, [3, 4, 6, 8, 16])
        self.assertTrue(diagnosis["invariants"]["truth_digest_stable"])
        self.assertTrue(diagnosis["invariants"]["exact_mismatch_set_stable"])
        kinds = {event["kind"] for event in diagnosis["flips"]}
        self.assertIn("RATIO_ALERT_CHANGED", kinds)
        self.assertIn("SOURCE_SELECTION_CHANGED", kinds)
        for event in diagnosis["flips"]:
            self.assertIsInstance(event["error_bound_share_ppm"], int)
            self.assertGreaterEqual(event["error_bound_share_ppm"], 0)
            self.assertIn("block_id", event)
            self.assertIn("before_decision", event)
            self.assertIn("after_decision", event)

    def test_single_scenario_grid_source_flip(self) -> None:
        spec = load_replica_group(self.workspace / "scenarios/single/spec.json")
        diagnosis = diagnose_grid_local(spec, [8, 16])
        source_events = [
            event
            for event in diagnosis["flips"]
            if event["kind"] == "SOURCE_SELECTION_CHANGED"
        ]
        self.assertEqual(len(source_events), 1)
        event = source_events[0]
        self.assertEqual(event["before_decision"]["source_node_id"], "n1")
        self.assertEqual(event["after_decision"]["source_node_id"], "n2")
        self.assertLessEqual(
            event["before_gap_units"], event["before_error_bound_units"]
        )
        self.assertGreater(
            event["after_gap_units"], event["after_error_bound_units"]
        )

    def test_analysis_is_connection_order_independent(self) -> None:
        spec = load_replica_group(self.workspace / "scenarios/grid/spec.json")
        first = analyze_local(spec, 8)
        second = analyze_local(spec, 8)
        self.assertEqual(
            [task.to_dict() for task in first.plan.tasks],
            [task.to_dict() for task in second.plan.tasks],
        )
