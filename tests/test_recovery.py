from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from .helpers import cli, load_sample, parse_json, status


class RecoveryTests(unittest.TestCase):
    def test_all_fault_points_recover_to_identical_status(self) -> None:
        for fault in (
            "during_plan_build",
            "after_window_plan",
            "during_batch_commit",
            "after_batch_commit",
            "after_window_commit",
        ):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as temp:
                base = Path(temp) / "base"
                crashed = Path(temp) / "crashed"
                load_sample(base)
                load_sample(crashed)
                cli(base, "schedule", "--clock", "1200", check=True)

                crash_result = cli(
                    crashed,
                    "schedule",
                    "--clock",
                    "1200",
                    env={"BATCHKERNEL_FAULT": fault},
                )
                self.assertEqual(crash_result.returncode, 91)

                resume_result = cli(crashed, "resume", check=True)
                self.assertEqual(
                    parse_json(resume_result)["command"], "resume"
                )
                self.assertEqual(status(crashed), status(base))
                verify_result = cli(crashed, "verify", check=True)
                self.assertTrue(parse_json(verify_result)["ok"])

                # Repeating resume must not duplicate batches, precision records,
                # or resource reservations.
                before_repeat = status(crashed)
                cli(crashed, "resume", check=True)
                self.assertEqual(status(crashed), before_repeat)

    def test_schedule_target_survives_window_plan_crash(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / "state"
            load_sample(state)
            crashed = cli(
                state,
                "schedule",
                "--clock",
                "1200",
                env={"BATCHKERNEL_FAULT": "after_window_plan"},
            )
            self.assertEqual(crashed.returncode, 91)
            interrupted = status(state)
            self.assertEqual(interrupted["clock"]["schedule_target_ms"], 1200)
            self.assertEqual(interrupted["clock"]["logical_clock_ms"], 90)
            cli(state, "resume", check=True)
            recovered = status(state)
            self.assertEqual(recovered["clock"]["logical_clock_ms"], 1200)
            self.assertIsNone(recovered["clock"]["schedule_target_ms"])


if __name__ == "__main__":
    unittest.main()

