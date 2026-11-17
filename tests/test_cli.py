from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.helpers import ROOT, SAMPLES


def run_cli(*args: str, cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [sys.executable, "-m", "shardrouter", *args],
        cwd=cwd,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


class CLITests(unittest.TestCase):
    def test_validate_command(self) -> None:
        result = run_cli("validate", "--config", str(SAMPLES / "config.json"))
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["validation"], "accepted")

    def test_three_injected_error_codes_and_jsonl_logs(self) -> None:
        cases = [
            ("broken-config", "E_CONFIG"),
            ("ring-conflict", "E_RING"),
            ("version-mismatch", "E_VERSION"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            for anomaly_type, expected in cases:
                log_path = Path(directory) / f"{anomaly_type}.jsonl"
                result = run_cli(
                    "anomaly",
                    "--type",
                    anomaly_type,
                    "--log-file",
                    str(log_path),
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                payload = json.loads(result.stdout)
                self.assertEqual(payload["status"], "rejected")
                self.assertEqual(payload["error"]["code"], expected)
                line = log_path.read_text(encoding="utf-8").strip()
                logged = json.loads(line)
                self.assertEqual(logged["event"], "anomaly_rejected")
                self.assertEqual(logged["code"], expected)

    def test_sql_error_has_cli_position(self) -> None:
        sql = "tenant_id = 7 AND region ="
        result = run_cli(
            "route",
            "--table",
            "orders",
            "--predicate",
            sql,
        )
        self.assertEqual(result.returncode, 2)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["error"]["code"], "E_SQL")
        self.assertEqual(payload["error"]["position"]["offset"], len(sql))

    def test_headless_ui_model(self) -> None:
        result = run_cli("ui", "--headless")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "headless_view_model")
        self.assertEqual(len(payload["model"]["business_codes"]), 1)

    def test_independent_demo_processes_are_byte_identical(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first_dir = Path(directory) / "first"
            second_dir = Path(directory) / "second"
            first = run_cli("demo", "--output-dir", str(first_dir))
            second = run_cli("demo", "--output-dir", str(second_dir))
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            first_summary = json.loads(first.stdout)
            second_summary = json.loads(second.stdout)
            self.assertEqual(first_summary, second_summary)
            self.assertEqual(first_summary["status"], "ok")
            files_first = sorted(path.name for path in first_dir.iterdir())
            files_second = sorted(path.name for path in second_dir.iterdir())
            self.assertEqual(files_first, files_second)
            for name in files_first:
                self.assertEqual(
                    (first_dir / name).read_bytes(),
                    (second_dir / name).read_bytes(),
                    f"artifact differs across independent runs: {name}",
                )


if __name__ == "__main__":
    unittest.main()
