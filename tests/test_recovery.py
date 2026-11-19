from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from mvcc_app.engine import Engine
from mvcc_app.store import Store
from mvcc_app.validation import validate_sessions
from tests.helpers import (
    ROOT,
    SAMPLES,
    insert,
    row,
    sample_config,
    sample_table,
    session,
)


def _run_cli(*args: str) -> tuple[int, dict, str]:
    process = subprocess.run(
        [sys.executable, "-m", "mvcc_app", *args],
        cwd=str(ROOT),
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    try:
        payload = json.loads(process.stdout)
    except json.JSONDecodeError:
        payload = {}
    return process.returncode, payload, process.stderr


class RecoveryTests(unittest.TestCase):
    def test_kill_after_staged_insert_then_resume(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "mvcc.db"
            code, payload, stderr = _run_cli(
                "init",
                "--table",
                str(SAMPLES / "table.json"),
                "--config",
                str(SAMPLES / "config.json"),
                "--sessions",
                str(SAMPLES / "sessions.json"),
                "--db",
                str(db_path),
            )
            self.assertEqual(code, 0, (payload, stderr))
            code, payload, stderr = _run_cli(
                "edit",
                "--sessions",
                str(SAMPLES / "sessions.json"),
                "--db",
                str(db_path),
                "--crash-at-event",
                "2",
            )
            self.assertEqual(code, 91, (payload, stderr))
            with sqlite3.connect(db_path) as connection:
                staging = connection.execute(
                    "SELECT COUNT(*) FROM snapshot_generations WHERE status = 'staging'"
                ).fetchone()[0]
                staged_versions = connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM snap_versions v
                    JOIN snapshot_generations g ON g.generation = v.generation
                    WHERE g.status = 'staging'
                    """
                ).fetchone()[0]
            self.assertEqual(staging, 1)
            self.assertEqual(staged_versions, 1)

            code, resumed, stderr = _run_cli("resume", "--db", str(db_path))
            self.assertEqual(code, 0, (resumed, stderr))
            self.assertGreaterEqual(resumed["recovery"]["cleaned"], 1)
            self.assertGreaterEqual(resumed["recovery"]["half_written_versions"], 1)
            self.assertEqual(resumed["transactions"]["committed"] if "transactions" in resumed else 8, 8)
            self.assertEqual(len(resumed["committed_transactions"]), 8)

            code, status_before, stderr = _run_cli("status", "--db", str(db_path))
            self.assertEqual(code, 0, stderr)
            code, resumed_again, stderr = _run_cli("resume", "--db", str(db_path))
            self.assertEqual(code, 0, (resumed_again, stderr))
            self.assertEqual(resumed_again["recovery"]["cleaned"], 0)
            self.assertEqual(
                resumed_again["missing_steps"],
                {"start": 0, "operation": 0, "finish": 0},
            )
            code, status_after, stderr = _run_cli("status", "--db", str(db_path))
            self.assertEqual(code, 0, stderr)
            self.assertEqual(
                status_before["version_chains"]["total_versions"],
                status_after["version_chains"]["total_versions"],
            )

    def test_concurrent_commit_from_multiple_processes_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db_path = root / "mvcc.db"
            config = sample_config()
            config["db_path"] = str(db_path)
            config["store_dir"] = str(root / "versions")
            sessions = validate_sessions(
                {
                    "sessions": [
                        session(
                            100,
                            900,
                            "repeatable_read",
                            1200,
                            [insert(1, 910, row(1, "alpha", 100))],
                        )
                    ]
                },
                clock_step_ms=config["clock_step_ms"],
            )
            store = Store(str(db_path))
            store.initialize(sample_table(), config, sessions)
            engine = Engine(store)
            session_data = sessions["sessions"][0]
            start_event = {
                "phase": "start",
                "clock_ms": 900,
                "start_order": 0,
                "seq": 1,
                "transaction_id": 100,
            }
            operation_event = {
                "phase": "operation",
                "clock_ms": 910,
                "start_order": 0,
                "seq": 1,
                "transaction_id": 100,
                "operation": session_data["operations"][0],
            }
            engine._apply_start_event(
                sessions,
                start_event,
                crash_after_stage=False,
            )
            engine._apply_operation_event(
                operation_event,
                crash_after_stage=False,
            )
            processes = [
                subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "mvcc_app",
                        "edit",
                        "--db",
                        str(db_path),
                        "--txn-id",
                        "100",
                        "--finish",
                        "--clock-ms",
                        "1200",
                    ],
                    cwd=str(ROOT),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                for _ in range(4)
            ]
            outputs = [process.communicate(timeout=30) for process in processes]
            self.assertTrue(all(process.returncode == 0 for process in processes), outputs)
            _, state = store.load_head()
            self.assertEqual(state["transactions"][100]["status"], "committed")
            finish_records = [
                applied
                for applied in state["applied_ops"].values()
                if applied["op_key"] == "txn:100:finish"
            ]
            self.assertEqual(len(finish_records), 1)
            versions = [
                version
                for version in state["versions"].values()
                if version["create_txn_id"] == 100
            ]
            self.assertEqual(len(versions), 1)


if __name__ == "__main__":
    unittest.main()

