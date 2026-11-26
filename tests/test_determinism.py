from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.helpers import load_and_run, run_cli


class DeterminismTests(unittest.TestCase):
    def test_two_clean_workdirs_produce_same_state_digest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            first_db = Path(temporary) / "one.db"
            second_db = Path(temporary) / "two.db"
            load_and_run(first_db)
            load_and_run(second_db)
            first, first_payload = run_cli(["status", "--db", str(first_db)])
            second, second_payload = run_cli(["status", "--db", str(second_db)])
            self.assertEqual(first.returncode, 0)
            self.assertEqual(second.returncode, 0)
            self.assertEqual(first_payload["state_digest"], second_payload["state_digest"])
            self.assertEqual(first_payload["tenants"], second_payload["tenants"])
            self.assertEqual(first_payload["scheduling"], second_payload["scheduling"])

