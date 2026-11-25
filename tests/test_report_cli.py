from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from feature_diag.report import run_canonical


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class ReportAndCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_canonical_report_is_byte_reproducible_and_complete(self) -> None:
        report_path = self.root / "artifacts" / "report-a.json"
        first, info_a = run_canonical(self.root, report_path=report_path)
        first_bytes = report_path.read_bytes()
        second, info_b = run_canonical(self.root, report_path=report_path)
        second_bytes = report_path.read_bytes()
        self.assertEqual(first_bytes, second_bytes)
        self.assertEqual(info_a["report_sha256"], info_b["report_sha256"])
        self.assertTrue(first["summary"]["ok"])
        self.assertTrue(first["migration"]["selection_equal"])
        self.assertTrue(first["queries"]["all_match_batch"])
        self.assertTrue(first["queries"]["parallelism"]["parallel_execution"]["results_equal"])
        self.assertTrue(first["queries"]["parallelism"]["degradation"]["results_equal"])
        self.assertEqual(len(first["diagnostics"]), 12)
        self.assertTrue(all(item["code_matches"] and item["handled"] for item in first["diagnostics"]))
        self.assertTrue(first["recovery"]["forced_termination"]["selection_equivalence"]["selection_equal_to_baseline"])
        self.assertTrue(first["recovery"]["graceful_stop"]["selection_equivalence"]["selection_equal_to_baseline"])
        expected_codes = {
            "E_BAD_INPUT",
            "E_ENCODING_ERROR",
            "E_LATENCY_BUDGET",
            "E_PARAM_INVALID",
            "E_RESOURCE_LIMIT",
            "E_SNAPSHOT_ROLLBACK",
            "E_STAGE_BUSY",
            "E_STALE_RESULT",
        }
        self.assertTrue(expected_codes.issubset(set(first["business_codes"])))
        self.assertIn("stages", first["timing"])
        self.assertIn("cache_reuse", first["batch"])

    def test_cli_generate_run_query_and_verify(self) -> None:
        environment = {"PYTHONPATH": str(PROJECT_ROOT)}
        generate = subprocess.run(
            [sys.executable, "-m", "feature_diag", "generate", "--root", str(self.root)],
            cwd=PROJECT_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(generate.returncode, 0, generate.stderr)
        run = subprocess.run(
            [sys.executable, "-m", "feature_diag", "run", "--root", str(self.root)],
            cwd=PROJECT_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        output = json.loads(run.stdout)
        self.assertEqual(output["status"], "ok")
        self.assertTrue((self.root / "artifacts" / "run_report.json").exists())
        query = subprocess.run(
            [
                sys.executable,
                "-m",
                "feature_diag",
                "query",
                "--root",
                str(self.root),
                "--row-id",
                "r0030",
                "--clock",
                "logical",
            ],
            cwd=PROJECT_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(query.returncode, 0, query.stderr)
        response = json.loads(query.stdout)
        self.assertEqual(response["status"], "ok")
        self.assertTrue(response["query"]["record"]["retained"])
        self.assertEqual(
            response["query"]["record"]["selection_key"],
            output["final_selection_key"],
        )
        verify = subprocess.run(
            [sys.executable, "-m", "feature_diag", "verify", "--root", str(self.root)],
            cwd=PROJECT_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(verify.returncode, 0, verify.stderr)
        verify_output = json.loads(verify.stdout)
        self.assertTrue(verify_output["reports_byte_identical"])

    def test_cli_query_timeout_code(self) -> None:
        environment = {"PYTHONPATH": str(PROJECT_ROOT)}
        subprocess.run(
            [sys.executable, "-m", "feature_diag", "run", "--root", str(self.root)],
            cwd=PROJECT_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=True,
        )
        timeout = subprocess.run(
            [
                sys.executable,
                "-m",
                "feature_diag",
                "query",
                "--root",
                str(self.root),
                "--row-id",
                "r0001",
                "--timeout-ms",
                "0",
                "--clock",
                "logical",
            ],
            cwd=PROJECT_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(timeout.returncode, 1)
        result = json.loads(timeout.stdout)
        self.assertEqual(result["error"]["code"], "E_LATENCY_BUDGET")
        self.assertEqual(result["error"]["completed_steps"], ["validate_request"])


if __name__ == "__main__":
    unittest.main()
