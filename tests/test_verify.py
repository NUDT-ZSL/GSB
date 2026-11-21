from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from elasticctl.service import dispatch, verify

from tests.common import load_fixture


class VerifyTests(unittest.TestCase):
    def test_verify_detects_ledger_corruption(self) -> None:
        with tempfile.TemporaryDirectory(prefix="elastic-verify-") as temp:
            db_file = Path(temp) / "verify.db"
            load_fixture(db_file)
            dispatch(db_file, at=120000)
            connection = sqlite3.connect(db_file)
            try:
                connection.execute(
                    """
                    UPDATE budget_state
                    SET core_used_units = core_used_units + 1
                    WHERE id = 'budget'
                    """
                )
                connection.commit()
            finally:
                connection.close()
            result = verify(db_file)
            self.assertFalse(result["ok"])
            self.assertIn(
                "E_BUDGET_CORE_LEDGER",
                {error["code"] for error in result["errors"]},
            )


if __name__ == "__main__":
    unittest.main()

