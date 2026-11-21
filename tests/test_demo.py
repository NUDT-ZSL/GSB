from __future__ import annotations

import json
import subprocess
import sys
import unittest

from .helpers import ROOT


class DemoTests(unittest.TestCase):
    def test_demo_is_repeatable_and_meets_acceptance(self) -> None:
        command = [sys.executable, str(ROOT / "run_demo.py")]
        first = subprocess.run(
            command,
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=60,
        )
        second = subprocess.run(
            command,
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=60,
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(first.stdout, second.stdout)
        payload = json.loads(first.stdout)
        self.assertTrue(payload["ok"])
        for key, value in payload["acceptance"].items():
            if key in {"version_replacement", "rollback_rejected_code"}:
                continue
            self.assertIs(value, True, key)
        self.assertEqual(
            payload["acceptance"]["version_replacement"],
            "FULL_REPLACEMENT",
        )
        self.assertEqual(
            payload["acceptance"]["rollback_rejected_code"],
            "E_VERSION_ROLLBACK",
        )
        self.assertTrue(payload["determinism"]["identical"])
        dashboard = ROOT / "artifacts" / "demo" / "run-a" / "dashboard.html"
        self.assertTrue(dashboard.exists())
        self.assertIn("payment-failover", dashboard.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()

