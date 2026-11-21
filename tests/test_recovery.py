from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from deploylab.demo import DemoRunner
from deploylab.bundle import write_bundle


class RecoveryTests(unittest.TestCase):
    def test_sigkill_restart_is_byte_identical_to_full_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bundle = root / "bundle"
            write_bundle(bundle)
            report = DemoRunner(
                bundle_path=bundle,
                work_dir=root / "demo",
            ).run()
            self.assertTrue(report["ok"])
            crash = report["crash_recovery"]
            self.assertEqual(crash["forced_exit_code"], -9)
            self.assertEqual(crash["forced_exit_signal"], "SIGKILL")
            self.assertEqual(crash["post_crash_version"], 1)
            self.assertEqual(
                crash["post_crash_committed_stages"],
                ["model_validation"],
            )
            self.assertFalse(crash["post_crash_quantization_committed"])
            self.assertTrue(crash["prediction_bytes_equal"])
            self.assertTrue(crash["activation_checksums_equal"])
            self.assertTrue(crash["consistent"])
            self.assertTrue(crash["recovery_report"]["only_committed_snapshots_used"])
            self.assertEqual(
                crash["recovery_report"]["resumed_from_stage"],
                "model_validation",
            )
            self.assertTrue(
                len(crash["recovery_report"]["discarded_uncommitted"]) > 0
            )
            self.assertNotEqual(
                crash["recovered"]["prediction_digest"],
                "",
            )
            revision = report["revision_update"]
            self.assertTrue(revision["sets_disjoint"])
            self.assertTrue(revision["sets_cover_all"])
            self.assertTrue(revision["all_stages_reused_upstream"])
            self.assertTrue(revision["update_matches_clean_run"])
            self.assertTrue(revision["consistent"])
            self.assertTrue(
                report["calibration_update"]["prediction_matches_clean_run"]
            )
            self.assertIn(
                "DEGRADED_GROUP_SIZE",
                report["budget_demo"]["degradation_categories"],
            )
            self.assertEqual(
                report["budget_demo"]["hard_limit_error"]["code"],
                "E_RESOURCE_LIMIT",
            )


if __name__ == "__main__":
    unittest.main()

