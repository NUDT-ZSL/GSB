from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from txlab.models import SchedulerConfig
from txlab.scheduler import Scheduler
from txlab.script import validate_script
from txlab.service import execute_script_file
from txlab.storage import SQLiteStore
from txlab.util import canonical_json


ROOT = Path(__file__).resolve().parents[1]
INIT_SQL = ROOT / "samples" / "initial_rows.sql"


class SchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="txlab-scheduler-test-")
        self.temp_dir = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _run_sample(
        self,
        sample: str,
        *,
        budget: int,
        concurrency: int = 2,
    ) -> dict:
        return execute_script_file(
            ROOT / "samples" / sample,
            db_path=self.temp_dir / f"{sample}.sqlite3",
            init_sql_path=INIT_SQL,
            config=SchedulerConfig(
                lock_wait_budget_ms=budget,
                max_concurrency=concurrency,
                tick_ms=10,
            ),
        )

    def test_normal_parallel_commits_and_digest_matches(self) -> None:
        result = self._run_sample("normal_parallel.json", budget=500)
        self.assertIsNone(result["code"])
        self.assertTrue(result["isolation_ok"])
        self.assertEqual(result["summary"]["committed"], 3)
        self.assertEqual(result["final_data_version"], 3)
        self.assertEqual(result["final_lock_state"], [])
        self.assertEqual(
            result["serialization_order"], ["n1", "n2", "n3"]
        )

    def test_lock_timeout_rolls_back_all_writes_and_releases_locks(self) -> None:
        result = self._run_sample("lock_timeout.json", budget=40)
        t2 = self._session(result, "t2")
        self.assertEqual(t2["outcome"], "rollback")
        self.assertEqual(t2["code"], "E_LOCKTIMEOUT")
        self.assertEqual(t2["committed_writes"], {})
        self.assertEqual(result["summary"]["committed"], 1)
        self.assertEqual(result["summary"]["timeouts"], 1)
        self.assertEqual(result["final_lock_state"], [])
        self.assertIn("lock_timeout", [event["type"] for event in result["events"]])
        self.assertIn("transaction_rollback", [event["type"] for event in result["events"]])

    def test_deadlock_victim_is_stable_across_runs(self) -> None:
        first = self._run_sample("deadlock.json", budget=500)
        second = self._run_sample("deadlock.json", budget=500)
        first_deadlock = first["deadlocks"][0]
        second_deadlock = second["deadlocks"][0]
        self.assertEqual(first_deadlock["cycle_members"], ["d1", "d2"])
        self.assertEqual(first_deadlock["victim_session_id"], "d1")
        self.assertEqual(
            first_deadlock["victim_session_id"],
            second_deadlock["victim_session_id"],
        )
        self.assertEqual(self._session(first, "d1")["code"], "E_DEADLOCK")
        self.assertEqual(self._session(first, "d2")["outcome"], "commit")
        self.assertEqual(first["serialization_order"], ["d2"])

    def test_read_committed_and_repeatable_read_semantics(self) -> None:
        rc = self._run_sample("isolation_rc.json", budget=500)
        rr = self._run_sample("isolation_rr.json", budget=500)
        rc_reads = self._session(rc, "rc-reader")["reads"]
        rr_reads = self._session(rr, "rr-reader")["reads"]
        self.assertEqual(
            [read["payload"]["balance"] for read in rc_reads], [100, 70]
        )
        self.assertEqual(
            [read["payload"]["balance"] for read in rr_reads], [100, 100]
        )
        self.assertEqual(rc_reads[1]["source"], "committed")
        self.assertEqual(rr_reads[1]["source"], "repeatable_read_cache")

    def test_timed_out_session_frees_concurrency_slot(self) -> None:
        raw = {
            "script_version": 1,
            "name": "slot-release",
            "sessions": [
                {
                    "id": "owner",
                    "isolation": "read_committed",
                    "operations": [
                        {
                            "op": "write",
                            "key": "account:A",
                            "value": {"balance": 88, "owner": "Alice"},
                        },
                        {"op": "commit"},
                    ],
                },
                {
                    "id": "waiter",
                    "isolation": "read_committed",
                    "operations": [
                        {
                            "op": "write",
                            "key": "account:A",
                            "value": {"balance": 77, "owner": "Alice"},
                        },
                        {"op": "commit"},
                    ],
                },
                {
                    "id": "next-slot",
                    "isolation": "read_committed",
                    "operations": [
                        {
                            "op": "write",
                            "key": "account:B",
                            "value": {"balance": 120, "owner": "Bob"},
                        },
                        {"op": "commit"},
                    ],
                },
                {
                    "id": "after-timeout",
                    "isolation": "read_committed",
                    "operations": [
                        {
                            "op": "write",
                            "key": "item:1",
                            "value": {"name": "widget", "quantity": 8},
                        },
                        {"op": "commit"},
                    ],
                },
            ],
        }
        script_path = self.temp_dir / "slot-release.json"
        script_path.write_text(canonical_json(raw), encoding="utf-8")
        result = execute_script_file(
            script_path,
            db_path=self.temp_dir / "slot-release.sqlite3",
            init_sql_path=INIT_SQL,
            config=SchedulerConfig(
                lock_wait_budget_ms=20,
                max_concurrency=2,
                tick_ms=10,
            ),
        )
        waiter = self._session(result, "waiter")
        self.assertEqual(waiter["code"], "E_LOCKTIMEOUT")
        next_slot = self._session(result, "next-slot")
        self.assertEqual(next_slot["outcome"], "commit")
        after_timeout = self._session(result, "after-timeout")
        self.assertEqual(after_timeout["outcome"], "commit")
        waiter_rollback_index = next(
            event["seq"]
            for event in result["events"]
            if event["type"] == "transaction_rollback"
            and event["session_id"] == "waiter"
        )
        next_start_index = next(
            event["seq"]
            for event in result["events"]
            if event["type"] == "transaction_begin"
            and event["session_id"] == "next-slot"
        )
        after_timeout_start_index = next(
            event["seq"]
            for event in result["events"]
            if event["type"] == "transaction_begin"
            and event["session_id"] == "after-timeout"
        )
        self.assertLess(next_start_index, after_timeout_start_index)
        self.assertLess(waiter_rollback_index, after_timeout_start_index)

    def test_mismatched_storage_state_reports_e_isolation(self) -> None:
        raw = {
            "script_version": 1,
            "name": "isolation-failure",
            "sessions": [
                {
                    "id": "writer",
                    "isolation": "read_committed",
                    "operations": [
                        {
                            "op": "write",
                            "key": "account:A",
                            "value": {"balance": 42, "owner": "Alice"},
                        },
                        {"op": "commit"},
                    ],
                }
            ],
        }
        store = SQLiteStore(self.temp_dir / "isolation-failure.sqlite3")
        store.reset_from_sql(INIT_SQL.read_text(encoding="utf-8"))
        script = validate_script(raw, store.row_keys())
        original_commit = store.commit_writes

        def corrupt_commit(session_id: str, writes: dict) -> int:
            version = original_commit(session_id, writes)
            store.conn.execute(
                "UPDATE lab_rows SET payload=? WHERE row_key='account:A'",
                (canonical_json({"balance": 999, "owner": "corrupt"}),),
            )
            return version

        store.commit_writes = corrupt_commit  # type: ignore[method-assign]
        result = Scheduler(
            script,
            store,
            SchedulerConfig(lock_wait_budget_ms=100, max_concurrency=1, tick_ms=10),
        ).run()
        store.close()
        self.assertEqual(result["code"], "E_ISOLATION")
        self.assertFalse(result["isolation_ok"])
        self.assertIn(
            "isolation_failed", [event["type"] for event in result["events"]]
        )

    @staticmethod
    def _session(result: dict, session_id: str) -> dict:
        return next(
            session
            for session in result["sessions"]
            if session["session_id"] == session_id
        )


if __name__ == "__main__":
    unittest.main()
