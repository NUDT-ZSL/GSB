from __future__ import annotations

import tempfile
from pathlib import Path

from replica_ledger.analysis import analyze_local
from replica_ledger.demo import render_text_report, run_demo
from replica_ledger.presentation import build_diagnostic_rows
from replica_ledger.spec import load_replica_group

from tests.common import SampleWorkspace


class PresentationTests(SampleWorkspace):
    def test_rows_are_stable_and_include_required_views(self) -> None:
        spec = load_replica_group(self.workspace / "scenarios/grid/spec.json")
        result = analyze_local(spec, 8)
        rows = build_diagnostic_rows(result)
        self.assertTrue(rows["replicas"])
        self.assertTrue(rows["truth"])
        self.assertTrue(rows["tasks"])
        self.assertEqual(
            [(row["node_id"], row["block_id"]) for row in rows["replicas"]],
            sorted((row["node_id"], row["block_id"]) for row in rows["replicas"]),
        )
        truth_blocks = [row["block_id"] for row in rows["truth"]]
        self.assertEqual(truth_blocks, sorted(truth_blocks))
        task_keys = [
            (row["target_node_id"], row["block_id"]) for row in rows["tasks"]
        ]
        self.assertEqual(task_keys, sorted(task_keys))


class DemoTests(SampleWorkspace):
    def test_three_experiments_converge_and_ledgers_reopen(self) -> None:
        with tempfile.TemporaryDirectory(prefix="replica-ledger-demo-test-") as temp:
            report = run_demo(Path(temp) / "work")
        self.assertEqual(
            report["experiment_order"],
            [
                "single_replica_mismatch",
                "multi_replica_majority_mismatch",
                "grid_perturbation",
            ],
        )
        self.assertTrue(report["invariants"]["all_repairs_converged"])
        self.assertTrue(report["invariants"]["all_ledgers_reopened_current"])
        self.assertTrue(
            report["invariants"]["exact_truth_digest_and_mismatch_set_are_grid_invariant"]
        )
        self.assertTrue(report["report_digest"])
        text = render_text_report(report)
        self.assertIn("single_replica_mismatch", text)
        self.assertIn("grid_perturbation", text)
