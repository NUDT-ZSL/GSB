from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from batchledger.db import LedgerDB
from batchledger.engine import BatchLedgerEngine
from batchledger.fixture import load_fixture
from tests.support import ROOT, cli


class SchedulerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory(prefix="batchledger-scheduler-")
        cls.db_path = Path(cls.temp.name) / "state.db"
        result = cli(
            "load",
            str(ROOT / "samples" / "default"),
            "--db",
            str(cls.db_path),
        )
        assert result.returncode == 0, result.stderr
        result = cli("run", "--db", str(cls.db_path))
        assert result.returncode == 0, result.stderr
        cls.status = BatchLedgerEngine(cls.db_path).status()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp.cleanup()

    def test_every_request_is_routed_to_exactly_one_window(self) -> None:
        routed = [
            request_id
            for window in self.status["windows"]
            for request_id in window["request_ids"]
        ]
        self.assertEqual(sorted(routed), list(range(1, 49)))
        self.assertEqual(len(routed), len(set(routed)))
        assigned = [item["request_id"] for item in self.status["assignments"]]
        self.assertEqual(len(assigned), len(set(assigned)))
        completed = {
            request["request_id"]
            for request in self.status["requests"]
            if request["status"] == "COMPLETED"
        }
        expired = {
            request["request_id"]
            for request in self.status["requests"]
            if request["status"] == "EXPIRED"
        }
        self.assertEqual(completed | expired, set(routed))
        self.assertFalse(completed & expired)
        self.assertFalse(expired & set(assigned))

    def test_windows_follow_logical_boundaries(self) -> None:
        fixture = load_fixture(ROOT / "samples" / "default")
        replicas = fixture.replica_map()
        request_map = fixture.request_map()
        for window in self.status["windows"]:
            replica = replicas[window["replica_id"]]
            self.assertEqual(window["start_ms"] % replica.window_ms, 0)
            self.assertEqual(
                window["end_ms"], window["start_ms"] + replica.window_ms
            )
            for request_id in window["request_ids"]:
                expected = (
                    request_map[request_id].arrival_ms // replica.window_ms
                )
                self.assertEqual(window["window_index"], expected)

    def test_batch_adjudication_observes_all_constraints(self) -> None:
        fixture = load_fixture(ROOT / "samples" / "default")
        replicas = fixture.replica_map()
        batch_sizes = []
        skipped: list[dict] = []
        for window in self.status["windows"]:
            replica = replicas[window["replica_id"]]
            window_request_ids = set(window["request_ids"])
            batched_request_ids: set[int] = set()
            for batch in window["batches"]:
                batch_sizes.append(len(batch["request_ids"]))
                self.assertLessEqual(len(batch["request_ids"]), replica.max_batch_size)
                self.assertLessEqual(
                    batch["metrics"]["compute"]["raw_float"],
                    replica.compute_capacity,
                )
                self.assertLessEqual(
                    batch["metrics"]["memory"]["raw_float"],
                    replica.memory_limit_mib,
                )
                self.assertFalse(
                    batched_request_ids & set(batch["request_ids"]),
                    "a request appeared in two batches of one window",
                )
                batched_request_ids.update(batch["request_ids"])
                skipped.extend(batch["decision"]["skipped"])
                for observation in batch["decision"]["observations"]:
                    self.assertIn("compute_raw", observation)
                    self.assertIn("compute_fixed", observation)
                    self.assertIn("memory_raw", observation)
                    self.assertIn("memory_fixed", observation)
                    self.assertIn("sequence_tokens", observation)
            expired_ids = {
                item["request_id"]
                for item in window["decision"].get("expired", [])
            }
            self.assertEqual(
                batched_request_ids | expired_ids,
                window_request_ids,
            )
        self.assertGreater(max(batch_sizes), 1)
        self.assertTrue(skipped)
        self.assertTrue(
            all(item["first_constraint"] in {
                "max_batch_size",
                "compute_budget",
                "memory_budget",
                "deadline",
            } for item in skipped)
        )

    def test_expiry_is_distinct_and_never_rebatched(self) -> None:
        expired = [
            request for request in self.status["requests"] if request["status"] == "EXPIRED"
        ]
        self.assertTrue(expired)
        self.assertTrue(
            all(request["status"] != "FAILED" for request in expired)
        )
        assigned = {item["request_id"] for item in self.status["assignments"]}
        self.assertFalse({request["request_id"] for request in expired} & assigned)
        self.assertTrue(
            all(
                request["status_reason"]
                for request in expired
            )
        )

    def test_two_fresh_runs_are_bitwise_identical(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            second_path = Path(temp) / "second.db"
            self.assertEqual(
                cli(
                    "load",
                    str(ROOT / "samples" / "default"),
                    "--db",
                    str(second_path),
                ).returncode,
                0,
            )
            self.assertEqual(cli("run", "--db", str(second_path)).returncode, 0)
            second = BatchLedgerEngine(second_path).semantic_snapshot()
            first = BatchLedgerEngine(self.db_path).semantic_snapshot()
            self.assertEqual(first, second)

    def test_injected_clock_advances_incrementally(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db_path = Path(temp) / "clock.db"
            self.assertEqual(
                cli(
                    "load",
                    str(ROOT / "samples" / "default"),
                    "--db",
                    str(db_path),
                ).returncode,
                0,
            )
            partial = cli(
                "run",
                "--db",
                str(db_path),
                "--clock-ms",
                "200",
            )
            self.assertEqual(partial.returncode, 0, partial.stderr)
            self.assertFalse(json.loads(partial.stdout)["complete"])
            snapshot = BatchLedgerEngine(db_path).status()
            self.assertTrue(
                all(window["end_ms"] <= 200 for window in snapshot["windows"] if window["status"] == "COMMITTED")
            )
            self.assertTrue(
                any(window["status"] != "COMMITTED" for window in snapshot["windows"])
            )
            final = cli("resume", "--db", str(db_path))
            self.assertEqual(final.returncode, 0, final.stderr)
            verify = cli("verify", "--db", str(db_path))
            self.assertEqual(verify.returncode, 0, verify.stdout + verify.stderr)

    def test_verify_detects_field_difference(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db_path = Path(temp) / "corrupt.db"
            self.assertEqual(
                cli(
                    "load",
                    str(ROOT / "samples" / "default"),
                    "--db",
                    str(db_path),
                ).returncode,
                0,
            )
            self.assertEqual(cli("run", "--db", str(db_path)).returncode, 0)
            with LedgerDB(db_path) as db:
                row = db.conn.execute(
                    "SELECT replica_id, window_index, version, batch_index FROM batches LIMIT 1"
                ).fetchone()
                db.conn.execute(
                    "UPDATE batches SET decision_json = replace(decision_json, "
                    "'priority_then_arrival_then_request_id', 'corrupted') "
                    "WHERE replica_id = ? AND window_index = ? AND version = ? AND batch_index = ?",
                    (
                        row["replica_id"],
                        row["window_index"],
                        row["version"],
                        row["batch_index"],
                    ),
                )
            result = cli("verify", "--db", str(db_path))
            self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
            payload = json.loads(result.stdout)
            self.assertFalse(payload["ok"])
            self.assertGreater(payload["difference_count"], 0)
            self.assertTrue(
                any(".decision." in diff["path"] for diff in payload["field_differences"])
            )


if __name__ == "__main__":
    unittest.main()
