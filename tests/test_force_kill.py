"""Force-kill recovery: only fully replaced snapshots may be used."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from mm_pipeline import ReasoningEngine, generate_dataset
from mm_pipeline.persistence import load_snapshot

REPO_ROOT = Path(__file__).resolve().parents[1]


class TestForceKillRecovery(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="mm_pipeline_kill_"))
        self.input_dir = self.root / "input"
        self.snapshot_dir = self.root / "snapshots"
        generate_dataset(self.input_dir)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_stale_temp_ignored_and_snapshot_recovers(self):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT)
        common = [sys.executable, "-m", "mm_pipeline.cli", "run",
                  "--input-dir", str(self.input_dir),
                  "--snapshot-dir", str(self.snapshot_dir),
                  "--report", str(self.root / "partial.json")]
        first = subprocess.run(common + ["--exit-after-round", "2"],
                               cwd=REPO_ROOT, env=env, capture_output=True,
                               text=True, timeout=60)
        self.assertEqual(first.returncode, 0, first.stderr)

        # Simulate a process killed mid-write: leave a stale same-volume tmp.
        stale = self.snapshot_dir / ".snapshot.json.leftover.tmp"
        stale.write_text("{incomplete", encoding="utf-8")

        report_path = self.root / "resumed_report.json"
        resumed = ReasoningEngine(self.input_dir, self.snapshot_dir,
                                  report_path)
        result = resumed.run()
        report = result["report"]
        self.assertTrue(report["resumed"])
        self.assertTrue(report["completed"])
        self.assertFalse(stale.exists(),
                         "stale temp file must be cleaned on startup")

        # A second interrupted run followed by resume stays field-consistent.
        snapshot = load_snapshot(self.snapshot_dir)
        self.assertEqual(snapshot["current_round"], report["current_round"])
        self.assertEqual(snapshot["version"], report["snapshot_version"])
        for sample in report["sample_results"]:
            self.assertEqual(
                len(sample["round_score_trajectory"]),
                len(sample["confidence_trajectory"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
