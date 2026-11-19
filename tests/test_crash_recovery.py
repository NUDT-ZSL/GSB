from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.helpers import (
    run_cli_json,
    setup_standard_state,
)


class CrashRecoveryTests(unittest.TestCase):
    def _load(self, state: Path) -> None:
        result, payload = run_cli_json(state, "load")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIsNotNone(payload)
        self.assertTrue(payload["ok"])

    def test_snapshot_crash_before_commit_is_rolled_back_by_resume(self) -> None:
        for crash_point in (
            "snapshot:after_pending",
            "snapshot:after_blocks",
            "snapshot:before_final_commit",
        ):
            with self.subTest(crash_point=crash_point):
                with tempfile.TemporaryDirectory() as directory:
                    state = Path(directory) / "state"
                    self._load(state)
                    crashed = run_cli_json(
                        state,
                        "snapshot",
                        "--sequence",
                        "1",
                        crash_at=crash_point,
                    )[0]
                    self.assertEqual(crashed.returncode, 97)
                    status = run_cli_json(state, "status")[1]
                    self.assertIsNotNone(status)
                    self.assertEqual(status["current_snapshot_id"], "snap-base")
                    self.assertNotIn(
                        "snap-001",
                        {item["snapshot_id"] for item in status["snapshots"]},
                    )
                    resumed = run_cli_json(state, "resume")[1]
                    self.assertIsNotNone(resumed)
                    self.assertEqual(resumed["pending_operation_count"], 0)
                    self.assertTrue(resumed["aborted_operations"])
                    verify = run_cli_json(state, "verify")[1]
                    self.assertIsNotNone(verify)
                    self.assertTrue(verify["ok"], verify)
                    gc = run_cli_json(state, "gc")[1]
                    self.assertIsNotNone(gc)
                    if crash_point != "snapshot:after_pending":
                        self.assertGreaterEqual(gc["reclaimed_blocks"], 1)
                    final_status = run_cli_json(state, "status")[1]
                    self.assertIsNotNone(final_status)
                    self.assertEqual(
                        final_status["block_objects"]["unreferenced"], 0
                    )

    def test_snapshot_crash_after_commit_is_already_complete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            self._load(state)
            crashed = run_cli_json(
                state,
                "snapshot",
                "--sequence",
                "1",
                crash_at="snapshot:after_commit",
            )[0]
            self.assertEqual(crashed.returncode, 97)
            status = run_cli_json(state, "status")[1]
            self.assertIsNotNone(status)
            self.assertEqual(status["current_snapshot_id"], "snap-001")
            self.assertIn(
                "snap-001",
                {item["snapshot_id"] for item in status["snapshots"]},
            )
            resumed = run_cli_json(state, "resume")[1]
            self.assertIsNotNone(resumed)
            self.assertEqual(resumed["aborted_operations"], [])
            verify = run_cli_json(state, "verify")[1]
            self.assertIsNotNone(verify)
            self.assertTrue(verify["ok"], verify)

    def test_rollback_crash_before_commit_keeps_old_chain_view(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            setup_standard_state(state)
            before = run_cli_json(state, "status")[1]
            crashed = run_cli_json(
                state,
                "rollback",
                "--request-id",
                "rb-001",
                crash_at="rollback:before_final_commit",
            )[0]
            self.assertEqual(crashed.returncode, 97)
            after = run_cli_json(state, "status")[1]
            self.assertIsNotNone(before)
            self.assertIsNotNone(after)
            self.assertEqual(after["current_snapshot_id"], "snap-003")
            self.assertEqual(after["chain_version"], before["chain_version"])
            self.assertIn(
                "rollback:rb-001",
                {item["operation_id"] for item in after["pending_operations"]},
            )
            resumed = run_cli_json(state, "resume")[1]
            self.assertIsNotNone(resumed)
            self.assertTrue(
                any(
                    item["request_id"] == "rb-001"
                    for item in resumed["aborted_operations"]
                )
            )
            verify = run_cli_json(state, "verify")[1]
            self.assertIsNotNone(verify)
            self.assertTrue(verify["ok"], verify)

    def test_gc_crash_is_completed_by_resume(self) -> None:
        for crash_point in ("gc:after_plan", "gc:mid_delete", "gc:before_finalize"):
            with self.subTest(crash_point=crash_point):
                with tempfile.TemporaryDirectory() as directory:
                    state = Path(directory) / "state"
                    self._load(state)
                    for sequence in (1, 2, 3):
                        result, _ = run_cli_json(
                            state, "snapshot", "--sequence", str(sequence)
                        )
                        self.assertEqual(result.returncode, 0)
                    result, _ = run_cli_json(
                        state, "rollback", "--request-id", "rb-002"
                    )
                    self.assertEqual(result.returncode, 0)
                    crashed = run_cli_json(state, "gc", crash_at=crash_point)[0]
                    self.assertEqual(crashed.returncode, 97)
                    resumed = run_cli_json(state, "resume")[1]
                    self.assertIsNotNone(resumed)
                    self.assertEqual(resumed["running_gc_count"], 0)
                    self.assertTrue(resumed["completed_gc_runs"])
                    verify = run_cli_json(state, "verify")[1]
                    self.assertIsNotNone(verify)
                    self.assertTrue(verify["ok"], verify)
                    status = run_cli_json(state, "status")[1]
                    self.assertIsNotNone(status)
                    self.assertEqual(status["block_objects"]["unreferenced"], 0)
