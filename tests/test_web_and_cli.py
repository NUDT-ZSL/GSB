from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

from lockfair.engine import LockFairEngine
from lockfair.web import DashboardServer

from tests.helpers import DEMO, ROOT


class WebAndCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "state.sqlite"
        LockFairEngine(self.db).initialize(DEMO, force=True)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_http_dashboard_and_step_api(self) -> None:
        server = DashboardServer(("127.0.0.1", 0), db_path=self.db, bundle_path=DEMO)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        port = server.server_address[1]
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as response:
                html = response.read().decode("utf-8")
            self.assertIn("LockFair", html)
            self.assertIn("批量授予编排与公平性裁决", html)

            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/step",
                data=b"{}",
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=10) as response:
                payload = json.loads(response.read().decode("utf-8"))
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["dashboard"]["snapshot"]["round_no"], 1)
            self.assertEqual(payload["dashboard"]["snapshot"]["version"], 2)

            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state?round=1", timeout=5) as response:
                state_payload = json.loads(response.read().decode("utf-8"))
            self.assertEqual(state_payload["snapshot"]["round_no"], 1)
            self.assertGreater(len(state_payload["rounds"]), 0)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_cli_compact_validation_and_error_json(self) -> None:
        valid = subprocess.run(
            [sys.executable, "-m", "lockfair.cli", "validate", str(DEMO), "--compact"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(valid.returncode, 0, valid.stderr)
        payload = json.loads(valid.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["applications"], 11)

        raw = json.loads(DEMO.read_text(encoding="utf-8"))
        raw["config"]["round_budget_ms"] = 0
        invalid_path = Path(self.temp.name) / "invalid.json"
        invalid_path.write_text(json.dumps(raw), encoding="utf-8")
        invalid = subprocess.run(
            [sys.executable, "-m", "lockfair.cli", "validate", str(invalid_path), "--compact"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(invalid.returncode, 2)
        error_payload = json.loads(invalid.stderr)
        self.assertEqual(error_payload["error"]["code"], "INVALID_ROUND_BUDGET")


if __name__ == "__main__":
    unittest.main()
