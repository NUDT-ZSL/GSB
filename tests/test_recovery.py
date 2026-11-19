from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from voice_state.config import AnalysisConfig
from voice_state.demo import run_demo
from voice_state.engine import AnalysisEngine
from voice_state.snapshot import SnapshotStore

from tests.helpers import make_dataset, run_cli


class RecoveryTests(unittest.TestCase):
    def test_crash_after_stage_and_restart_is_byte_identical(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            dataset_dir = make_dataset(root, dialogs=3, speakers=2, seed=51)
            full_engine = AnalysisEngine(dataset_dir, root / "full")
            full = full_engine.run()

            crashed = run_cli(
                [
                    "--workdir",
                    str(root),
                    "--json",
                    "run",
                    "--dataset",
                    str(dataset_dir),
                    "--state",
                    str(root / "resumed"),
                    "--crash-after-stage",
                    "segmentation",
                ]
            )
            self.assertEqual(crashed.returncode, 75, crashed.stderr + crashed.stdout)
            status = SnapshotStore(root / "resumed").status()
            self.assertEqual(status["last_committed_stage"], "segmentation")
            self.assertEqual(status["resume_from_stage"], "regression")

            resumed_engine = AnalysisEngine(dataset_dir, root / "resumed")
            recovered = resumed_engine.run()
            self.assertEqual(recovered.reused_stages, ["framing", "segmentation"])
            self.assertEqual(recovered.computed_stages, ["regression", "aggregation"])
            self.assertEqual(full.report_bytes, recovered.report_bytes)
            self.assertEqual(full.report_hash, recovered.report_hash)

    def test_crash_mid_stage_discards_uncommitted_work(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            dataset_dir = make_dataset(root, dialogs=3, speakers=2, seed=52)
            full = AnalysisEngine(dataset_dir, root / "full").run()
            crashed = run_cli(
                [
                    "--workdir",
                    str(root),
                    "--json",
                    "run",
                    "--dataset",
                    str(dataset_dir),
                    "--state",
                    str(root / "resumed"),
                    "--crash-during-stage",
                    "regression",
                    "--crash-after-dialogs",
                    "1",
                ]
            )
            self.assertEqual(crashed.returncode, 75, crashed.stderr + crashed.stdout)
            status = SnapshotStore(root / "resumed").status()
            self.assertEqual(status["last_committed_stage"], "segmentation")
            self.assertEqual(
                status["stages"]["regression"]["status"],
                "pending",
            )
            recovered = AnalysisEngine(dataset_dir, root / "resumed").run()
            self.assertEqual(recovered.computed_stages, ["regression", "aggregation"])
            self.assertEqual(full.report_bytes, recovered.report_bytes)

    def test_interactive_demo_reports_identity_and_self_check(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            result = run_demo(
                Path(temp),
                stop_after="framing",
                config=AnalysisConfig(),
                dialog_count=2,
                speaker_count=2,
            )
            self.assertTrue(result["report_bytes_identical"])
            self.assertEqual(result["full_report_hash"], result["recovered_report_hash"])
            self.assertEqual(result["crash_exit_code"], 75)
            self.assertEqual(result["reused_stages"], ["framing"])
            self.assertEqual(result["computed_stages"], ["segmentation", "regression", "aggregation"])
            self.assertTrue((Path(temp) / "demo" / "full_report.json").is_file())
            self.assertTrue((Path(temp) / "demo" / "recovered_report.json").is_file())


if __name__ == "__main__":
    unittest.main()

