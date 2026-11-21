from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tests.common import run_cli


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="elastic-cli-")
        self.db_file = Path(self.temp.name) / "cli.db"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_full_cli_flow_and_stable_status_json(self) -> None:
        load = run_cli("load", db_file=self.db_file)
        self.assertEqual(load.returncode, 0, load.stderr)
        self.assertTrue(json.loads(load.stdout)["ok"])

        decide = run_cli(
            "decide",
            "--at",
            "120000",
            db_file=self.db_file,
        )
        self.assertEqual(decide.returncode, 0, decide.stderr)
        self.assertEqual(
            [
                decision["workloadId"]
                for decision in json.loads(decide.stdout)["decisions"]
            ],
            ["wl-api", "wl-batch", "wl-cache", "wl-queue", "wl-search"],
        )

        dispatch = run_cli(
            "dispatch",
            "--at",
            "120000",
            db_file=self.db_file,
        )
        self.assertEqual(dispatch.returncode, 0, dispatch.stderr)
        self.assertEqual(
            json.loads(dispatch.stdout)["scheduler"]["counts"]["succeeded"],
            2,
        )

        apply = run_cli("apply", db_file=self.db_file)
        self.assertEqual(apply.returncode, 0, apply.stderr)
        self.assertEqual(
            json.loads(apply.stdout)["afterConfigVersion"],
            2,
        )

        status_one = run_cli("status", db_file=self.db_file)
        status_two = run_cli("status", db_file=self.db_file)
        self.assertEqual(status_one.returncode, 0, status_one.stderr)
        self.assertEqual(status_one.stdout, status_two.stdout)
        parsed = json.loads(status_one.stdout)
        expected = json.dumps(
            parsed,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        ) + "\n"
        self.assertEqual(status_one.stdout, expected)
        self.assertIn("budget", parsed)
        self.assertIn("workloads", parsed)

        resumed = run_cli("resume", db_file=self.db_file)
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(json.loads(resumed.stdout)["counts"]["resumed"], 0)

        verify = run_cli("verify", db_file=self.db_file)
        self.assertEqual(verify.returncode, 0, verify.stderr)
        self.assertTrue(json.loads(verify.stdout)["ok"])


if __name__ == "__main__":
    unittest.main()

