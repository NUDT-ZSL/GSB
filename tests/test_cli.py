from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from tests.helpers import SampleTest


class CliTests(SampleTest):
    def test_invalid_task_returns_json_error_and_no_output(self) -> None:
        bad_task = self.samples / "bad_summary.json"
        bad_task.write_text('{"schema_version": 1,', encoding="utf-8")
        output = self.root / "out_cli"
        process = subprocess.run(
            [
                sys.executable,
                "-m",
                "pointcloud_workbench",
                "render",
                "--task",
                str(bad_task),
                "--state",
                str(self.root / "state_cli"),
                "--output",
                str(output),
            ],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(process.returncode, 2)
        payload = json.loads(process.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "E_JSON")
        self.assertFalse(output.exists())

    def test_non_finite_error_detail_is_valid_strict_json(self) -> None:
        document = self.task_document("task_base.json")
        document["render"]["supersample"] = float("nan")
        task = self.write_task(document, "task_nan_cli.json")
        output = self.root / "out_cli_nan"
        process = subprocess.run(
            [
                sys.executable,
                "-m",
                "pointcloud_workbench",
                "render",
                "--task",
                str(task),
                "--state",
                str(self.root / "state_cli_nan"),
                "--output",
                str(output),
            ],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(process.returncode, 2)
        payload = json.loads(process.stdout)
        self.assertEqual(payload["error"]["code"], "E_SUPERSAMPLE")
        self.assertEqual(payload["error"]["details"]["value"], "NaN")
        self.assertFalse(output.exists())

