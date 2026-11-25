from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tests.support import ROOT, cli


class CliTests(unittest.TestCase):
    def test_missing_state_has_business_code(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            result = cli("status", "--db", str(Path(temp) / "missing.db"))
            self.assertEqual(result.returncode, 3)
            payload = json.loads(result.stderr)
            self.assertEqual(payload["error"]["code"], "E_STATE_NOT_FOUND")
            self.assertFalse(Path(str(Path(temp) / "missing.db") + ".lock").exists())

    def test_verify_incomplete_has_stable_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db_path = Path(temp) / "incomplete.db"
            self.assertEqual(
                cli(
                    "load",
                    str(ROOT / "samples" / "default"),
                    "--db",
                    str(db_path),
                ).returncode,
                0,
            )
            result = cli("verify", "--db", str(db_path))
            self.assertEqual(result.returncode, 4)
            self.assertEqual(
                json.loads(result.stderr)["error"]["code"], "E_VERIFY_INCOMPLETE"
            )

    def test_status_json_is_stable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db_path = Path(temp) / "stable.db"
            self.assertEqual(
                cli(
                    "load",
                    str(ROOT / "samples" / "default"),
                    "--db",
                    str(db_path),
                ).returncode,
                0,
            )
            first = cli("status", "--db", str(db_path)).stdout
            second = cli("status", "--db", str(db_path)).stdout
            self.assertEqual(first, second)
            payload = json.loads(first)
            self.assertEqual(
                [request["request_id"] for request in payload["requests"]],
                sorted(request["request_id"] for request in payload["requests"]),
            )
            self.assertEqual(
                [
                    (window["start_ms"], window["replica_id"], window["window_index"])
                    for window in payload["windows"]
                ],
                sorted(
                    (window["start_ms"], window["replica_id"], window["window_index"])
                    for window in payload["windows"]
                ),
            )


if __name__ == "__main__":
    unittest.main()
