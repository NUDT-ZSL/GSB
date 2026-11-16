from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from joinlab.acceptance import run_acceptance


class AcceptanceCliTests(unittest.TestCase):
    def test_acceptance_report_is_stable_and_complete(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            first = run_acceptance(ROOT, Path(temp_dir) / "first")
            second = run_acceptance(ROOT, Path(temp_dir) / "second")
            first_report = Path(first["report_path"]).read_bytes()
            second_report = Path(second["report_path"]).read_bytes()
            self.assertEqual(first_report, second_report)
            report = json.loads(first_report)
            self.assertEqual(report["summary"]["query_count"], 4)
            self.assertTrue(report["summary"]["all_cross_path_consistent"])
            self.assertTrue(report["summary"]["snapshot_restored"])
            self.assertTrue(report["summary"]["refresh_converged"])
            self.assertTrue(report["summary"]["all_anomaly_codes_as_expected"])
            self.assertTrue((Path(temp_dir) / "first" / "logs" / "business.jsonl").exists())

    def test_launch_from_unrelated_working_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            completed = subprocess.run(
                [sys.executable, str(ROOT / "run_cli.py"), "validate", "--queries", str(ROOT / "samples" / "queries.json")],
                cwd=temp_dir,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["catalog"]["table_count"], 4)

    def test_unknown_reference_query_file_is_e_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            query_path = Path(temp_dir) / "queries.json"
            query_path.write_text(
                json.dumps(
                    {
                        "queries": [
                            {
                                "name": "bad_ref",
                                "sql": "SELECT orders.id FROM orders JOIN customers ON orders.nope=customers.id",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "run_cli.py"),
                    "validate",
                    "--queries",
                    str(query_path),
                    "--log",
                    str(Path(temp_dir) / "errors.jsonl"),
                ],
                cwd=temp_dir,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 2)
            payload = json.loads(completed.stdout)
            self.assertEqual(payload["code"], "E_SCHEMA")
            self.assertIn("queries[0].sql", payload["path"])
            log_line = json.loads((Path(temp_dir) / "errors.jsonl").read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(log_line["code"], "E_SCHEMA")

    def test_inject_cli_reports_expected_business_code_and_logs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "run_cli.py"),
                    "inject",
                    "--kind",
                    "type",
                    "--out-dir",
                    str(Path(temp_dir) / "injected"),
                    "--log",
                    str(Path(temp_dir) / "business.jsonl"),
                ],
                cwd=temp_dir,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertEqual(payload["detected_code"], "E_TYPE")
            log_line = json.loads((Path(temp_dir) / "business.jsonl").read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(log_line["code"], "E_TYPE")
            self.assertEqual(log_line["injection_kind"], "type")


if __name__ == "__main__":
    unittest.main()
