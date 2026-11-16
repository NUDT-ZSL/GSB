from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from txlab.errors import E_SCRIPT, ScriptError
from txlab.script import validate_script
from txlab.service import execute_script_file
from txlab.storage import SQLiteStore


ROOT = Path(__file__).resolve().parents[1]
INIT_SQL = ROOT / "samples" / "initial_rows.sql"


class ScriptValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="txlab-script-test-")
        self.db_path = Path(self.temp.name) / "state.sqlite3"
        self.store = SQLiteStore(self.db_path)
        self.store.reset_from_sql(INIT_SQL.read_text(encoding="utf-8"))
        self.keys = self.store.row_keys()

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def test_missing_key_reports_session_and_operation_index(self) -> None:
        raw = self._raw(
            [
                {
                    "id": "bad-key",
                    "isolation": "read_committed",
                    "operations": [
                        {"op": "read", "key": "missing:row"},
                        {"op": "commit"},
                    ],
                }
            ]
        )
        with self.assertRaises(ScriptError) as caught:
            validate_script(raw, self.keys)
        self.assertEqual(caught.exception.code, E_SCRIPT)
        self.assertEqual(caught.exception.session_id, "bad-key")
        self.assertEqual(caught.exception.op_index, 0)

    def test_invalid_isolation_is_rejected(self) -> None:
        raw = self._raw(
            [
                {
                    "id": "bad-isolation",
                    "isolation": "serializable",
                    "operations": [{"op": "commit"}],
                }
            ]
        )
        with self.assertRaises(ScriptError) as caught:
            validate_script(raw, self.keys)
        self.assertEqual(caught.exception.session_id, "bad-isolation")

    def test_operation_after_rollback_is_rejected(self) -> None:
        raw = self._raw(
            [
                {
                    "id": "late-op",
                    "isolation": "read_committed",
                    "operations": [
                        {"op": "rollback"},
                        {"op": "read", "key": "account:A"},
                    ],
                }
            ]
        )
        with self.assertRaises(ScriptError) as caught:
            validate_script(raw, self.keys)
        self.assertEqual(caught.exception.session_id, "late-op")
        self.assertEqual(caught.exception.op_index, 1)

    def test_session_must_end_with_terminal_operation(self) -> None:
        raw = self._raw(
            [
                {
                    "id": "unterminated",
                    "isolation": "repeatable_read",
                    "operations": [{"op": "read", "key": "account:A"}],
                }
            ]
        )
        with self.assertRaises(ScriptError) as caught:
            validate_script(raw, self.keys)
        self.assertEqual(caught.exception.session_id, "unterminated")
        self.assertEqual(caught.exception.op_index, 0)

    def test_whole_script_rejected_before_any_writes(self) -> None:
        raw = self._raw(
            [
                {
                    "id": "valid-first",
                    "isolation": "read_committed",
                    "operations": [
                        {
                            "op": "write",
                            "key": "account:A",
                            "value": {"balance": 1},
                        },
                        {"op": "commit"},
                    ],
                },
                {
                    "id": "invalid-second",
                    "isolation": "read_committed",
                    "operations": [
                        {"op": "read", "key": "no:such:row"},
                        {"op": "commit"},
                    ],
                },
            ]
        )
        script_path = Path(self.temp.name) / "invalid.json"
        script_path.write_text(
            json.dumps(raw, ensure_ascii=False), encoding="utf-8"
        )
        with self.assertRaises(ScriptError):
            execute_script_file(
                script_path,
                db_path=self.db_path,
                init_sql_path=INIT_SQL,
            )
        self.store.close()
        self.store = SQLiteStore(self.db_path)
        self.assertEqual(self.store.data_version(), 0)
        self.assertEqual(
            self.store.get_row("account:A").payload,
            {"balance": 100, "owner": "Alice"},
        )

    @staticmethod
    def _raw(sessions: list[dict]) -> dict:
        return {
            "script_version": 1,
            "name": "validation-test",
            "sessions": sessions,
        }


if __name__ == "__main__":
    unittest.main()

