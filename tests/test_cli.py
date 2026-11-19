from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

from sparse_precision.codes import (
    E_RESOURCE_LIMIT,
    E_SHAPE_INVALID,
    E_TOLERANCE_EXCEEDED,
    OK,
)
from sparse_precision.generator import reference_checksum


class CliTests(unittest.TestCase):
    def test_demo_and_run_are_offline_and_complete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            demo_path = root / "demo.json"
            samples = root / "samples"
            demo = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "sparse_precision.cli",
                    "demo",
                    "--out",
                    str(demo_path),
                    "--samples",
                    str(samples),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(demo.returncode, 0, demo.stderr)
            payload = json.loads(demo_path.read_text(encoding="utf-8"))
            self.assertTrue(payload["all_expectations_met"])
            self.assertTrue(all(payload["expectations"].values()))
            self.assertTrue(
                all(item["code"] == OK for item in payload["normal_cases"])
            )
            demonstrations = payload["demonstrations"]
            self.assertEqual(
                demonstrations["invalid_input"]["report"]["code"],
                E_SHAPE_INVALID,
            )
            self.assertTrue(
                demonstrations["resource_downgrade"]["report"]["degradation"][
                    "degraded"
                ]
            )
            self.assertEqual(
                demonstrations["impossible_budget"]["report"]["code"],
                E_RESOURCE_LIMIT,
            )
            self.assertEqual(
                demonstrations["tolerance_probe"]["report"]["code"],
                E_TOLERANCE_EXCEEDED,
            )

            sample_files = sorted(samples.glob("case_*.json"))
            self.assertEqual(len(sample_files), 6)
            reference_files = sorted(samples.glob("reference_*.npy"))
            self.assertEqual(len(reference_files), 6)
            required = {
                "case_id",
                "shape_mnk",
                "nnz_a",
                "nnz_b",
                "dtype",
                "seed",
                "block_candidates",
            }
            for path in sample_files:
                metadata = json.loads(path.read_text(encoding="utf-8"))
                self.assertTrue(required.issubset(metadata))
                reference = np.load(
                    samples / f"reference_{metadata['case_id']}.npy",
                    allow_pickle=False,
                )
                self.assertEqual(
                    reference_checksum(reference),
                    metadata["checksums"]["reference"],
                )

            run_path = root / "run.json"
            run = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "sparse_precision.cli",
                    "run",
                    "--samples",
                    str(samples),
                    "--out",
                    str(run_path),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            run_payload = json.loads(run_path.read_text(encoding="utf-8"))
            self.assertEqual(run_payload["summary"]["case_count"], 6)
            self.assertEqual(run_payload["summary"]["codes"], {OK: 6})


if __name__ == "__main__":
    unittest.main()

