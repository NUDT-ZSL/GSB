from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from mvcc_app.cli import main
from mvcc_app.errors import BusinessError, ErrorCode
from mvcc_app.validation import (
    load_config,
    load_sessions,
    load_table,
    validate_config,
    validate_sessions,
    validate_table,
)
from tests.helpers import SAMPLES, row, sample_config


class ValidationTests(unittest.TestCase):
    def test_samples_are_valid_and_normalized(self) -> None:
        table = load_table(str(SAMPLES / "table.json"))
        config = load_config(str(SAMPLES / "config.json"))
        sessions = load_sessions(str(SAMPLES / "sessions.json"), config["clock_step_ms"])
        self.assertEqual(table["table_name"], "accounts")
        self.assertEqual(len(sessions["sessions"]), 10)
        starts = [item["start_clock_ms"] for item in sessions["sessions"]]
        self.assertEqual(starts, sorted(starts))

    def test_duplicate_column_is_rejected(self) -> None:
        with self.assertRaises(BusinessError) as caught:
            validate_table(
                {
                    "table": "t",
                    "columns": [
                        {"name": "id", "type": "integer", "primary_key": True},
                        {"name": "id", "type": "text"},
                    ],
                }
            )
        self.assertEqual(caught.exception.code, ErrorCode.DUPLICATE_COLUMN)

    def test_column_type_is_closed_enum(self) -> None:
        with self.assertRaises(BusinessError) as caught:
            validate_table(
                {
                    "table": "t",
                    "columns": [{"name": "id", "type": "uuid", "primary_key": True}],
                }
            )
        self.assertEqual(caught.exception.code, ErrorCode.INVALID_COLUMN_TYPE)

    def test_start_clock_order_and_isolation(self) -> None:
        base = {
            "transaction_id": 1,
            "start_clock_ms": 100,
            "isolation": "repeatable_read",
            "commit_clock_ms": 200,
            "operations": [],
        }
        later = {
            **base,
            "transaction_id": 2,
            "start_clock_ms": 90,
        }
        with self.assertRaises(BusinessError) as caught:
            validate_sessions({"sessions": [base, later]})
        self.assertEqual(caught.exception.code, ErrorCode.TXN_START_ORDER)

        bad_isolation = {**base, "isolation": "serializable"}
        with self.assertRaises(BusinessError) as caught:
            validate_sessions({"sessions": [bad_isolation]})
        self.assertEqual(caught.exception.code, ErrorCode.INVALID_ISOLATION)

    def test_operation_type_is_closed_enum(self) -> None:
        with self.assertRaises(BusinessError) as caught:
            validate_sessions(
                {
                    "sessions": [
                        {
                            "transaction_id": 1,
                            "start_clock_ms": 10,
                            "isolation": "repeatable_read",
                            "commit_clock_ms": 20,
                            "operations": [
                                {
                                    "seq": 1,
                                    "clock_ms": 11,
                                    "type": "upsert",
                                    "row": row(1, "a", 0),
                                }
                            ],
                        }
                    ]
                }
            )
        self.assertEqual(caught.exception.code, ErrorCode.INVALID_OPERATION)

    def test_bad_config_is_rejected(self) -> None:
        with self.assertRaises(BusinessError) as caught:
            validate_config({"clock_step_ms": 0})
        self.assertEqual(caught.exception.code, ErrorCode.INVALID_CONFIG)

    def test_invalid_init_does_not_create_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            table_path = root / "table.json"
            config_path = root / "config.json"
            sessions_path = root / "sessions.json"
            db_path = root / "never.db"
            table_path.write_text(
                json.dumps(
                    {
                        "table": "t",
                        "columns": [
                            {"name": "id", "type": "integer", "primary_key": True},
                            {"name": "id", "type": "text"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            config_path.write_text(json.dumps(sample_config()), encoding="utf-8")
            sessions_path.write_text(json.dumps({"sessions": []}), encoding="utf-8")
            output = io.StringIO()
            with redirect_stdout(output):
                code = main(
                    [
                        "init",
                        "--table",
                        str(table_path),
                        "--config",
                        str(config_path),
                        "--sessions",
                        str(sessions_path),
                        "--db",
                        str(db_path),
                    ]
                )
            self.assertEqual(code, 2)
            self.assertFalse(db_path.exists())
            self.assertIn("DUPLICATE_COLUMN", output.getvalue())


if __name__ == "__main__":
    unittest.main()

