from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from elasticctl.service import apply_patches, decide, dispatch, status, verify

from tests.common import SAMPLES, load_fixture


class PatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="elastic-patch-")
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_affected_closure_and_precise_invalidation(self) -> None:
        db_file = self.root / "patch.db"
        load_fixture(db_file)
        dispatch(db_file, at=120000)
        before = status(db_file)
        before_workloads = {
            workload["id"]: workload for workload in before["workloads"]
        }
        report = apply_patches(db_file, SAMPLES / "rule_patches.json")
        after = status(db_file)
        after_workloads = {
            workload["id"]: workload for workload in after["workloads"]
        }

        self.assertEqual(report["beforeConfigVersion"], 1)
        self.assertEqual(report["afterConfigVersion"], 2)
        self.assertEqual(report["unaffectedWorkloads"], ["wl-batch", "wl-cache"])
        affected = {
            entry["workloadId"]: entry for entry in report["affectedWorkloads"]
        }
        self.assertEqual(
            set(affected),
            {"wl-api", "wl-queue", "wl-search", "wl-realtime"},
        )
        self.assertIn("wl-realtime", after_workloads)

        for workload_id in ("wl-batch", "wl-cache"):
            self.assertEqual(
                before_workloads[workload_id],
                after_workloads[workload_id],
            )
        for workload_id in ("wl-api", "wl-queue", "wl-search"):
            self.assertIsNone(after_workloads[workload_id]["lastDecision"])

        queue_triggers = affected["wl-queue"]["triggers"]
        self.assertTrue(
            any(
                trigger["chain"]
                == [
                    "metric_source:ms-queue-depth",
                    "rule:r-queue-fast-out",
                    "workload:wl-queue",
                ]
                for trigger in queue_triggers
            )
        )

        rule_states = {
            state["ruleId"]: state
            for workload in after["workloads"]
            for state in workload["ruleStates"]
        }
        self.assertEqual(
            rule_states["r-api-cpu-out"]["cooldownUntilMs"],
            165000,
        )
        self.assertEqual(
            rule_states["r-queue-fast-out"]["cooldownUntilMs"],
            150000,
        )
        self.assertGreater(
            rule_states["r-search-out"]["backoffUntilMs"],
            120000,
        )
        self.assertEqual(before["budget"], after["budget"])

        decision_payload = decide(db_file, at=150000)
        realtime = next(
            decision
            for decision in decision_payload["decisions"]
            if decision["workloadId"] == "wl-realtime"
        )
        self.assertEqual(realtime["status"], "no_action")
        self.assertEqual(
            realtime["ruleEvaluations"][0]["reason"],
            "metric_unavailable",
        )
        self.assertTrue(verify(db_file)["ok"])

    def test_patch_replay_is_idempotent(self) -> None:
        db_file = self.root / "replay.db"
        load_fixture(db_file)
        first = apply_patches(db_file, SAMPLES / "rule_patches.json")
        version = status(db_file)["stateVersion"]
        second = apply_patches(db_file, SAMPLES / "rule_patches.json")
        self.assertEqual(first, second)
        self.assertEqual(status(db_file)["stateVersion"], version)


if __name__ == "__main__":
    unittest.main()

