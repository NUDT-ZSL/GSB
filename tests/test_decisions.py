from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from elasticctl.service import decide, dispatch, status

from tests.common import load_fixture


class DecisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="elastic-decision-")
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_priority_selection_and_rejection_reasons(self) -> None:
        db_file = self.root / "decide.db"
        load_fixture(db_file)
        payload = decide(db_file, at=120000)
        queue = next(
            decision
            for decision in payload["decisions"]
            if decision["workloadId"] == "wl-queue"
        )
        self.assertEqual(queue["selectedRuleId"], "r-queue-fast-out")
        self.assertEqual(queue["fromReplicas"], 3)
        self.assertEqual(queue["targetReplicas"], 8)
        rejected = {
            evaluation["ruleId"]: evaluation
            for evaluation in queue["ruleEvaluations"]
            if evaluation["status"] == "rejected"
        }
        self.assertEqual(
            rejected["r-queue-steady-out"]["reason"],
            "shadowed_by_higher_priority",
        )
        self.assertEqual(
            rejected["r-queue-in"]["reason"],
            "threshold_not_met",
        )

    def test_candidate_is_ceiled_then_clamped(self) -> None:
        db_file = self.root / "clamp.db"
        load_fixture(db_file)
        payload = decide(db_file, at=120000)
        api = next(
            decision
            for decision in payload["decisions"]
            if decision["workloadId"] == "wl-api"
        )
        self.assertEqual(api["ruleEvaluations"][0]["ratioCandidateReplicas"], 6)
        self.assertEqual(api["candidateReplicas"], 6)
        self.assertLessEqual(api["candidateReplicas"], 12)

    def test_freeze_blocks_scale_out(self) -> None:
        db_file = self.root / "freeze.db"
        load_fixture(db_file)
        dispatch(db_file, at=120000)
        payload = decide(db_file, at=180000)
        freeze_reasons = [
            evaluation["reason"]
            for decision in payload["decisions"]
            for evaluation in decision["ruleEvaluations"]
            if evaluation["direction"] == "scale_out"
        ]
        self.assertIn("freeze_window_blocks_scale_out", freeze_reasons)

    def test_repeated_dispatch_is_byte_stable(self) -> None:
        db_file = self.root / "stable.db"
        load_fixture(db_file)
        first = dispatch(db_file, at=120000)
        version_after_first = status(db_file)["stateVersion"]
        second = dispatch(db_file, at=120000)
        version_after_second = status(db_file)["stateVersion"]
        self.assertEqual(first, second)
        self.assertEqual(version_after_first, version_after_second)

    def test_two_independent_runs_produce_identical_outputs(self) -> None:
        db_one = self.root / "run-one.db"
        db_two = self.root / "run-two.db"
        load_fixture(db_one)
        load_fixture(db_two)
        decide_one = decide(db_one, at=120000)
        decide_two = decide(db_two, at=120000)
        self.assertEqual(decide_one, decide_two)
        dispatch_one = dispatch(db_one, at=120000)
        dispatch_two = dispatch(db_two, at=120000)
        self.assertEqual(dispatch_one, dispatch_two)
        self.assertEqual(status(db_one), status(db_two))


if __name__ == "__main__":
    unittest.main()

