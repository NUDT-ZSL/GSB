from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from elasticctl.service import dispatch, resume, status, verify

from tests.common import load_fixture, run_cli


class RecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="elastic-recovery-")
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_forced_exit_modes_recover_without_double_debit(self) -> None:
        expected_codes = {
            "after_reservation": 86,
            "after_start": 87,
            "before_effect_commit": 88,
            "after_effect_commit": 89,
        }
        for mode, expected_code in expected_codes.items():
            with self.subTest(mode=mode):
                db_file = self.root / f"{mode}.db"
                load_fixture(db_file)
                process = run_cli(
                    "dispatch",
                    "--at",
                    "120000",
                    db_file=db_file,
                    env={"ELASTICCTL_FAULT": mode},
                )
                self.assertEqual(process.returncode, expected_code, process.stderr)
                before_resume = status(db_file)
                core_before = before_resume["budget"]["coreUsedUnits"]
                self.assertGreater(core_before, 0)
                had_unconfirmed = bool(before_resume["inFlightActions"])

                result = resume(db_file)
                self.assertIn("recoveryClassification", result)
                if had_unconfirmed:
                    self.assertGreater(result["counts"]["confirmed"], 0)
                else:
                    self.assertEqual(result["counts"]["confirmed"], 0)
                after_resume = status(db_file)
                self.assertEqual(
                    after_resume["budget"]["coreUsedUnits"],
                    core_before,
                )
                self.assertEqual(
                    after_resume["budget"]["memoryReservedMiB"],
                    0,
                )
                second = resume(db_file)
                self.assertEqual(second["counts"]["resumed"], 0)
                self.assertEqual(
                    status(db_file)["budget"]["coreUsedUnits"],
                    core_before,
                )
                verification = verify(db_file)
                self.assertTrue(verification["ok"], verification)

    def test_per_workload_concurrency_blocks_new_round_while_unconfirmed(self) -> None:
        db_file = self.root / "concurrency.db"
        load_fixture(db_file)
        process = run_cli(
            "dispatch",
            "--at",
            "120000",
            db_file=db_file,
            env={"ELASTICCTL_FAULT": "after_reservation"},
        )
        self.assertEqual(process.returncode, 86)
        payload = dispatch(
            db_file,
            at=150000,
            workload_ids=["wl-api"],
        )
        self.assertEqual(payload["scheduler"]["counts"]["notStarted"], 1)
        self.assertEqual(
            payload["scheduler"]["notStarted"][0]["reason"],
            "workload_concurrency_limit",
        )
        self.assertTrue(verify(db_file)["ok"])
        resume(db_file)
        self.assertTrue(verify(db_file)["ok"])


if __name__ == "__main__":
    unittest.main()

