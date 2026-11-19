from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from mvcc_app.store import Store
from tests.helpers import ROOT, SAMPLES, insert, row, sample_config, session


def _run(*args: str) -> tuple[int, dict, str]:
    process = subprocess.run(
        [sys.executable, "-m", "mvcc_app", *args],
        cwd=str(ROOT),
        check=False,
        capture_output=True,
        text=True,
        timeout=40,
    )
    try:
        payload = json.loads(process.stdout)
    except json.JSONDecodeError:
        payload = {}
    return process.returncode, payload, process.stderr


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


class CLITests(unittest.TestCase):
    def test_end_to_end_commands_and_core_errors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "mvcc.db"
            code, payload, stderr = _run(
                "init",
                "--table",
                str(SAMPLES / "table.json"),
                "--config",
                str(SAMPLES / "config.json"),
                "--sessions",
                str(SAMPLES / "sessions.json"),
                "--db",
                str(db_path),
                "--replay",
            )
            self.assertEqual(code, 0, (payload, stderr))
            self.assertEqual(payload["replay"]["transactions"]["committed"], 8)

            code, read_payload, stderr = _run(
                "read",
                "--db",
                str(db_path),
                "--txn-id",
                "109",
                "--clock-ms",
                "2010",
                "--where",
                '[{"column":"id","op":"eq","value":3}]',
            )
            self.assertEqual(code, 0, (read_payload, stderr))
            self.assertEqual(read_payload["rows"][0]["values"]["name"], "gamma-rebuilt")
            self.assertLessEqual(
                read_payload["logical_latency_ms"],
                read_payload["latency_budget_ms"],
            )
            self.assertEqual(
                read_payload["snapshot"]["active_transactions"],
                sorted(read_payload["snapshot"]["active_transactions"]),
            )

            code, error, _ = _run(
                "read",
                "--db",
                str(db_path),
                "--txn-id",
                "9999",
                "--clock-ms",
                "2020",
            )
            self.assertEqual(code, 2)
            self.assertEqual(error["error"]["code"], "UNKNOWN_TRANSACTION")

            code, error, _ = _run(
                "read",
                "--db",
                str(db_path),
                "--txn-id",
                "109",
                "--clock-ms",
                "1900",
            )
            self.assertEqual(code, 2)
            self.assertEqual(error["error"]["code"], "CLOCK_REGRESSION")

            code, gc_payload, stderr = _run(
                "gc",
                "--db",
                str(db_path),
                "--clock-ms",
                "2200",
            )
            self.assertEqual(code, 0, (gc_payload, stderr))
            self.assertGreater(gc_payload["collected_count"], 0)
            code, status, stderr = _run("status", "--db", str(db_path))
            self.assertEqual(code, 0, stderr)
            self.assertEqual(
                status["gc"]["collected_total"],
                gc_payload["collected_count"],
            )
            self.assertTrue(status["gc"]["last"]["retained"])

            code, verify_payload, stderr = _run(
                "verify",
                "--db",
                str(db_path),
                "--runs",
                "2",
                "--gc-clock-ms",
                "2200",
            )
            self.assertEqual(code, 0, (verify_payload, stderr))
            self.assertTrue(verify_payload["deterministic"])
            self.assertTrue(verify_payload["numpy_batch_equal"])
            self.assertTrue(verify_payload["current_matches_replay"])

    def test_async_gc_worker_completes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "mvcc.db"
            code, payload, stderr = _run(
                "init",
                "--table",
                str(SAMPLES / "table.json"),
                "--config",
                str(SAMPLES / "config.json"),
                "--sessions",
                str(SAMPLES / "sessions.json"),
                "--db",
                str(db_path),
                "--replay",
            )
            self.assertEqual(code, 0, (payload, stderr))
            code, started, stderr = _run(
                "gc",
                "--db",
                str(db_path),
                "--clock-ms",
                "2200",
                "--async",
            )
            self.assertEqual(code, 0, (started, stderr))
            self.assertEqual(started["status"], "started")
            deadline = time.monotonic() + 8
            status = {}
            while time.monotonic() < deadline:
                code, status, stderr = _run("status", "--db", str(db_path))
                self.assertEqual(code, 0, stderr)
                if status["gc"]["runs"] >= 1:
                    break
                time.sleep(0.05)
            self.assertGreaterEqual(status["gc"]["runs"], 1)
            self.assertGreater(status["gc"]["collected_total"], 0)

    def test_stale_snapshot_budget_and_digest_errors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            stale_config = sample_config()
            stale_config["snapshot_retention"] = 1
            stale_config_path = root / "stale-config.json"
            _write_json(stale_config_path, stale_config)
            stale_db = root / "stale.db"
            code, payload, stderr = _run(
                "init",
                "--table",
                str(SAMPLES / "table.json"),
                "--config",
                str(stale_config_path),
                "--sessions",
                str(SAMPLES / "sessions.json"),
                "--db",
                str(stale_db),
                "--replay",
            )
            self.assertEqual(code, 0, (payload, stderr))
            code, error, _ = _run(
                "read",
                "--db",
                str(stale_db),
                "--txn-id",
                "101",
                "--clock-ms",
                "2100",
            )
            self.assertEqual(code, 2)
            self.assertEqual(error["error"]["code"], "STALE_SNAPSHOT")

            budget_config = sample_config()
            budget_config["query_latency_budget_ms"] = 2
            budget_config_path = root / "budget-config.json"
            budget_sessions_path = root / "budget-sessions.json"
            _write_json(budget_config_path, budget_config)
            _write_json(
                budget_sessions_path,
                {
                    "sessions": [
                        session(
                            100,
                            900,
                            "repeatable_read",
                            980,
                            [insert(1, 910, row(1, "alpha", 100))],
                        )
                    ]
                },
            )
            budget_db = root / "budget.db"
            code, payload, stderr = _run(
                "init",
                "--table",
                str(SAMPLES / "table.json"),
                "--config",
                str(budget_config_path),
                "--sessions",
                str(budget_sessions_path),
                "--db",
                str(budget_db),
                "--replay",
            )
            self.assertEqual(code, 0, (payload, stderr))
            code, error, _ = _run(
                "read",
                "--db",
                str(budget_db),
                "--txn-id",
                "100",
                "--clock-ms",
                "2000",
            )
            self.assertEqual(code, 2)
            self.assertEqual(
                error["error"]["code"],
                "QUERY_LATENCY_BUDGET_EXCEEDED",
            )

            digest_db = root / "digest.db"
            code, payload, stderr = _run(
                "init",
                "--table",
                str(SAMPLES / "table.json"),
                "--config",
                str(SAMPLES / "config.json"),
                "--sessions",
                str(SAMPLES / "sessions.json"),
                "--db",
                str(digest_db),
                "--replay",
            )
            self.assertEqual(code, 0, (payload, stderr))
            store = Store(str(digest_db))

            def tamper(state: dict, context: dict) -> tuple[dict, bool]:
                version = next(iter(state["versions"].values()))
                version["data"]["balance"] = 999_999
                return {}, True

            store.mutate(tamper)
            code, error, _ = _run(
                "read",
                "--db",
                str(digest_db),
                "--txn-id",
                "109",
                "--clock-ms",
                "2010",
            )
            self.assertEqual(code, 2)
            self.assertEqual(error["error"]["code"], "VERSION_DIGEST_MISMATCH")
            code, error, _ = _run("verify", "--db", str(digest_db), "--runs", "2")
            self.assertEqual(code, 2)
            self.assertEqual(error["error"]["code"], "VERSION_DIGEST_MISMATCH")


if __name__ == "__main__":
    unittest.main()

