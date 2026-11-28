from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class CliTests(unittest.TestCase):
    def test_invalid_project_is_rejected_with_error_report(self) -> None:
        manifest = {
            "project_id": "invalid",
            "policy": {
                "watermark_ratio": 0.8,
                "max_latency_ms": 100,
            },
            "institutions": [
                {"id": "I1", "required_compliance_level": 1}
            ],
            "regions": [
                {
                    "id": "R1",
                    "capacity_bytes": 1000,
                    "used_bytes": 0,
                    "health": "healthy",
                    "latency_ms": 5,
                    "compliance_level": 3,
                }
            ],
            "objects": [
                {
                    "id": "O1",
                    "institution_id": "I1",
                    "test_category": "生化",
                    "urgency": "routine",
                    "bytes": -1,
                    "created_at": 2,
                    "expires_at": 1,
                    "digest": "sha256:x",
                }
            ],
            "events": [],
            "incidents": [],
        }
        with tempfile.TemporaryDirectory() as temporary:
            temp = Path(temporary)
            project_path = temp / "project.json"
            output = temp / "out"
            project_path.write_text(
                json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "main.py"),
                    "--project",
                    str(project_path),
                    "--seed",
                    "9",
                    "--output",
                    str(output),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 2)
            report_path = output / "validation_errors.json"
            self.assertTrue(report_path.exists())
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertFalse(report["validation"]["valid"])
            self.assertGreaterEqual(report["validation"]["error_count"], 2)
            codes = {
                error["code"] for error in report["validation"]["errors"]
            }
            self.assertIn("INVALID_OBJECT_BYTES", codes)
            self.assertIn("EXPIRES_BEFORE_CREATED", codes)

    def test_cli_generates_sample_and_reports(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temp = Path(temporary)
            output = temp / "reports"
            environment = dict(os.environ)
            environment["PYTHONPATH"] = str(ROOT)
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "exchange_storage",
                    "--seed",
                    "42",
                    "--output",
                    str(output),
                ],
                cwd=temp,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((output / "report.json").exists())
            self.assertTrue((output / "report.md").exists())
            self.assertTrue(
                (temp / "samples" / "sample_project_seed_42.json").exists()
            )
            report = json.loads(
                (output / "report.json").read_text(encoding="utf-8")
            )
            self.assertEqual(report["metadata"]["seed"], 42)
            self.assertTrue(report["reconciliation"]["ok"])


if __name__ == "__main__":
    unittest.main()
