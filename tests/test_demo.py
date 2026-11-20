from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

from blockops.demo import build_demo_report
from blockops.models import ExecutionOptions
from blockops.snapshot import SnapshotStore
from blockops.tolerance import compare_arrays
from blockops.models import ToleranceConfig


ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"


class DemoReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        cls.output = Path(cls.temporary.name) / "demo"
        cls.report = build_demo_report(SAMPLES, cls.output, ExecutionOptions())

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def test_verification_and_required_report_sections(self) -> None:
        verification = self.report["verification"]
        self.assertTrue(verification["passed"])
        self.assertTrue(verification["rerun_byte_exact"])
        self.assertEqual(
            verification["four_injection_stages_seen"],
            [
                "block_statistics",
                "input_validation",
                "normalization_accumulation",
                "output_writeback",
            ],
        )
        self.assertEqual(
            verification["validation_codes_seen"],
            sorted(
                [
                    "E_OP_UNKNOWN",
                    "E_AXIS_INVALID",
                    "E_SHAPE_INVALID",
                    "E_DTYPE_UNSUPPORTED",
                ]
            ),
        )
        self.assertEqual(verification["unattributed_error_count"], 0)
        for job in self.report["jobs"]:
            self.assertIn("block_statistics", job)
            self.assertIn("deviation_spectrum", job)
            self.assertIn("degradations", job)
            self.assertIn("diagnostics", job)
            self.assertIn("self_check", job)
            self.assertIn("snapshot", job)
        self.assertEqual(len(self.report["injections"]["records"]), 4)
        for record in self.report["injections"]["records"]:
            self.assertIn("stage", record)
            self.assertIn("code", record)
            self.assertIn("job_id", record)
            self.assertIn("params", record)
            self.assertIn("params_digest", record)

    def test_snapshot_files_query_and_replacement(self) -> None:
        loaded, error = SnapshotStore.load(self.output / "snapshots.json")
        self.assertIsNone(error)
        plan = loaded.query("block_plan")
        tolerance = loaded.query("tolerance_config")
        self.assertEqual(plan["version"], 2)
        self.assertEqual(tolerance["version"], 2)
        self.assertEqual(
            self.report["snapshots"]["replacement_checks"]["rollback_attempt"]["code"],
            "E_SNAPSHOT_ROLLBACK",
        )
        self.assertEqual(
            self.report["snapshots"]["replacement_checks"]["corruption_attempt"]["code"],
            "E_SNAPSHOT_CORRUPT",
        )
        self.assertTrue(
            self.report["snapshots"]["replacement_checks"]["whole_replacement"][
                "old_key_removed"
            ]
        )

    def test_report_block_details_recompute_deviation_spectrum(self) -> None:
        job_report = next(
            job for job in self.report["jobs"] if job["job_id"] == "softmax_2d_f64"
        )
        tensor = np.load(SAMPLES / "tensors" / "softmax_2d_f64.npy", allow_pickle=False)
        shifted = tensor.astype(np.float64) - np.max(
            tensor.astype(np.float64), axis=-1, keepdims=True
        )
        exponentials = np.exp(shifted)
        reference = exponentials / np.sum(exponentials, axis=-1, keepdims=True)
        reconstructed = np.empty(tensor.shape, dtype=np.float64)
        for block in job_report["block_statistics"]:
            np.put(
                reconstructed,
                block["element_indices"],
                block["output_values"],
            )
        spectrum = compare_arrays(
            reconstructed, reference, ToleranceConfig()
        )
        self.assertEqual(
            spectrum["spectrum_sha256"],
            job_report["deviation_spectrum"]["spectrum_sha256"],
        )
        for degradation in job_report["degradations"]:
            self.assertIn(degradation["trigger_code"], job_report["business_codes"])
            block_ids = {
                block["block_id"] for block in job_report["block_statistics"]
            }
            self.assertTrue(set(degradation["affected_blocks"]) <= block_ids)

    def test_external_cli_rerun_is_byte_identical(self) -> None:
        with tempfile.TemporaryDirectory() as left, tempfile.TemporaryDirectory() as right:
            for directory in (left, right):
                completed = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "blockops",
                        "demo",
                        "--data-dir",
                        str(SAMPLES),
                        "--output-dir",
                        directory,
                    ],
                    cwd=ROOT,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                summary = json.loads(completed.stdout)
                self.assertTrue(summary["verification_passed"])
                self.assertTrue(summary["rerun_byte_exact"])
            left_root = Path(left)
            right_root = Path(right)
            left_files = sorted(
                path.relative_to(left_root)
                for path in left_root.rglob("*")
                if path.is_file()
            )
            right_files = sorted(
                path.relative_to(right_root)
                for path in right_root.rglob("*")
                if path.is_file()
            )
            self.assertEqual(left_files, right_files)
            for relative in left_files:
                self.assertEqual(
                    (left_root / relative).read_bytes(),
                    (right_root / relative).read_bytes(),
                    str(relative),
                )


if __name__ == "__main__":
    unittest.main()
