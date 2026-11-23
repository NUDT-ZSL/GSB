from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from rope_arbiter.sample import build_sample_cases, write_cases_jsonl


class CliTests(unittest.TestCase):
    def test_cli_emits_complete_report(self) -> None:
        repository_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cases_path = root / "cases.jsonl"
            cache_path = root / "cache"
            reports_path = root / "reports"
            write_cases_jsonl(cases_path, build_sample_cases())
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "rope_arbiter.cli",
                    "--cases",
                    str(cases_path),
                    "--cache-dir",
                    str(cache_path),
                    "--reports-dir",
                    str(reports_path),
                    "--max-parallel-nodes",
                    "4",
                    "--parallelism",
                    "8",
                ],
                cwd=repository_root,
                text=True,
                capture_output=True,
                timeout=120,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            self.assertEqual(summary["status"], "ok")
            report = json.loads(Path(summary["report"]).read_text(encoding="utf-8"))
            self.assertTrue(report["self_check"]["ok"])
            self.assertEqual(report["self_check"]["transition_count"], 7)
            self.assertEqual(len(report["snapshot_transitions"]), 7)
            self.assertGreaterEqual(len(report["degradations"]), 1)
            self.assertIn("OK", report["business_codes"])
            for transition in report["snapshot_transitions"]:
                self.assertTrue(
                    transition["directional_invalidation_ok"], transition["name"]
                )
                self.assertTrue(transition["full_recompute_hash_equivalent"])
            first_run = next(iter(report["runs"].values()))
            for case in first_run["cases"]:
                self.assertIsNotNone(case["angle_mapping"]["residual_spectrum"])
                self.assertIn(
                    "score_max_abs_deviation", case["cross_variant"]
                )


if __name__ == "__main__":
    unittest.main()
