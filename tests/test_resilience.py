from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from lockfair.engine import LockFairEngine

from tests.helpers import DEMO, ROOT, application, holder, make_bundle, resource


def run_crash_process(db: Path, crash_point: str) -> subprocess.CompletedProcess[str]:
    code = f"""
import os
from lockfair.engine import LockFairEngine
os.environ["LOCKFAIR_CRASH_POINT"] = {crash_point!r}
engine = LockFairEngine({str(db)!r})
engine.execute_round(round_no=1, request_id="crash-test")
"""
    env = os.environ.copy()
    env.pop("LOCKFAIR_CRASH_POINT", None)
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def query_one(db: Path, sql: str, params: tuple = ()) -> tuple:
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute(sql, params).fetchone()
    finally:
        conn.close()


class RecoveryAndConcurrencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "state.sqlite"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _clean_round_hash(self) -> tuple[str, dict]:
        clean_db = Path(self.temp.name) / "clean.sqlite"
        clean = LockFairEngine(clean_db)
        clean.initialize(DEMO, force=True)
        snapshot = clean.execute_round(round_no=1, request_id="crash-test")
        return snapshot["state_hash"], snapshot["state"]

    def _assert_recovered_equals_clean(self, expected_hash: str, expected_state: dict) -> None:
        engine = LockFairEngine(self.db)
        recovered = engine.recover()
        self.assertEqual(recovered["prepared_jobs"], 1)
        self.assertEqual(recovered["results"][0]["status"], "replayed")
        snapshot = engine.get_state()
        self.assertEqual(snapshot["version"], 2)
        self.assertEqual(snapshot["round_no"], 1)
        self.assertEqual(snapshot["state_hash"], expected_hash)
        self.assertEqual(snapshot["state"], expected_state)
        job = query_one(self.db, "SELECT status, result_version FROM round_jobs WHERE request_id='crash-test'")
        self.assertEqual(job, ("completed", 2))
        self.assertEqual(query_one(self.db, "SELECT COUNT(*) FROM rounds WHERE round_no=1")[0], 1)
        self.assertEqual(query_one(self.db, "SELECT COUNT(*) FROM grants")[0], 2)
        wait_counts = sqlite3.connect(str(self.db)).execute(
            "SELECT seq, COUNT(*) FROM actions WHERE kind='wait_advance' GROUP BY seq"
        ).fetchall()
        self.assertTrue(wait_counts)
        self.assertTrue(all(count == 1 for _, count in wait_counts))

    def test_crash_after_prepare_replays_once(self) -> None:
        engine = LockFairEngine(self.db)
        engine.initialize(DEMO, force=True)
        expected_hash, expected_state = self._clean_round_hash()
        process = run_crash_process(self.db, "after_prepare")
        self.assertEqual(process.returncode, 86, process.stderr)
        self.assertEqual(query_one(self.db, "SELECT status FROM round_jobs WHERE request_id='crash-test'"), ("prepared",))
        self.assertEqual(query_one(self.db, "SELECT COUNT(*) FROM rounds")[0], 0)
        self.assertEqual(query_one(self.db, "SELECT version FROM current_snapshot")[0], 1)
        self._assert_recovered_equals_clean(expected_hash, expected_state)
        # A second recovery is a no-op and cannot double-count.
        self.assertEqual(LockFairEngine(self.db).recover()["prepared_jobs"], 0)
        self.assertEqual(query_one(self.db, "SELECT COUNT(*) FROM grants")[0], 2)

    def test_crash_mid_transaction_leaves_no_half_snapshot(self) -> None:
        engine = LockFairEngine(self.db)
        engine.initialize(DEMO, force=True)
        expected_hash, expected_state = self._clean_round_hash()
        process = run_crash_process(self.db, "mid_apply")
        self.assertEqual(process.returncode, 86, process.stderr)
        self.assertEqual(query_one(self.db, "SELECT COUNT(*) FROM snapshots")[0], 1)
        self.assertEqual(query_one(self.db, "SELECT COUNT(*) FROM grants")[0], 0)
        self.assertEqual(query_one(self.db, "SELECT status FROM round_jobs WHERE request_id='crash-test'"), ("prepared",))
        self._assert_recovered_equals_clean(expected_hash, expected_state)

    def test_multiprocess_same_round_is_idempotent(self) -> None:
        LockFairEngine(self.db).initialize(DEMO, force=True)
        code = f"""
import json
from lockfair.engine import LockFairEngine
result = LockFairEngine({str(self.db)!r}).execute_round(round_no=1, request_id="parallel-round")
print(json.dumps({{
    "version": result["version"],
    "hash": result["state_hash"],
    "round": result["round_no"],
    "replay": result.get("idempotent_replay", False),
}}))
"""
        processes = [
            subprocess.Popen(
                [sys.executable, "-c", code],
                cwd=str(ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(8)
        ]
        results = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=45)
            self.assertEqual(process.returncode, 0, stderr)
            results.append(json.loads(stdout))
        self.assertEqual({item["version"] for item in results}, {2})
        self.assertEqual(len({item["hash"] for item in results}), 1)
        self.assertGreaterEqual(sum(1 for item in results if item["replay"]), 1)
        self.assertEqual(query_one(self.db, "SELECT COUNT(*) FROM rounds")[0], 1)
        self.assertEqual(query_one(self.db, "SELECT COUNT(*) FROM grants")[0], 2)
        self.assertEqual(
            query_one(self.db, "SELECT COUNT(*) FROM (SELECT lock_key, lease_generation FROM grants GROUP BY lock_key, lease_generation HAVING COUNT(*) > 1)")[0],
            0,
        )
        duplicate_actions = query_one(
            self.db,
            """
            SELECT COUNT(*) FROM (
                SELECT idempotency_key FROM actions GROUP BY idempotency_key HAVING COUNT(*) > 1
            )
            """,
        )[0]
        self.assertEqual(duplicate_actions, 0)
        wait_counts = sqlite3.connect(str(self.db)).execute(
            "SELECT seq, COUNT(*) FROM actions WHERE kind='wait_advance' GROUP BY seq"
        ).fetchall()
        self.assertTrue(all(count == 1 for _, count in wait_counts))
        max_occupied = query_one(self.db, "SELECT MAX(occupied_count) FROM holders")[0]
        self.assertLessEqual(max_occupied, 2)

    def test_multiprocess_submissions_on_distinct_keys_are_not_lost(self) -> None:
        raw = make_bundle(
            budget=2000,
            quota=1,
            labels=["gold", "silver", "bronze"],
            resources=[
                resource("shard-a", priority=40),
                resource("shard-b", priority=30),
                resource("shard-c", priority=20),
                resource("shard-d", priority=10),
            ],
            holders=[holder("alice", 4)],
            applications=[],
        )
        LockFairEngine(self.db).initialize(raw, force=True)
        definitions = [
            (201, "shard-a", "gold"),
            (202, "shard-b", "silver"),
            (203, "shard-c", "bronze"),
            (204, "shard-d", "gold"),
        ]
        processes = []
        for seq, lock_key, label in definitions:
            application_payload = application(seq, "alice", lock_key, label=label)
            code = f"""
import json
from lockfair.engine import LockFairEngine
engine = LockFairEngine({str(self.db)!r})
result = engine.submit_application({application_payload!r})
print(json.dumps({{"ok": result["ok"], "version": result["state_version"], "seq": {seq}}}))
"""
            processes.append(
                subprocess.Popen(
                    [sys.executable, "-c", code],
                    cwd=str(ROOT),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            )
        results = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=45)
            self.assertEqual(process.returncode, 0, stderr)
            results.append(json.loads(stdout))
        self.assertTrue(all(item["ok"] for item in results))
        self.assertEqual(sorted(item["version"] for item in results), [2, 3, 4, 5])
        snapshot = LockFairEngine(self.db).get_state()
        self.assertEqual(len(snapshot["state"]["applications"]), 4)
        self.assertEqual(
            {item["fairness_label"] for item in snapshot["state"]["applications"].values()},
            {"gold", "silver", "bronze"},
        )

    def test_committed_snapshot_hashes_are_immutable(self) -> None:
        engine = LockFairEngine(self.db)
        engine.initialize(DEMO, force=True)
        engine.run_rounds(4)
        conn = sqlite3.connect(str(self.db))
        try:
            rows = conn.execute("SELECT state_hash, state_json FROM snapshots ORDER BY version").fetchall()
        finally:
            conn.close()
        from lockfair.store import state_hash

        self.assertEqual(len(rows), 5)
        for digest, raw in rows:
            self.assertEqual(digest, state_hash(json.loads(raw)))

    def test_deterministic_replay_across_databases(self) -> None:
        db_a = Path(self.temp.name) / "a.sqlite"
        db_b = Path(self.temp.name) / "b.sqlite"
        engine_a = LockFairEngine(db_a)
        engine_b = LockFairEngine(db_b)
        engine_a.initialize(DEMO, force=True)
        engine_b.initialize(DEMO, force=True)
        for _ in range(6):
            a = engine_a.execute_round()
            b = engine_b.execute_round()
            self.assertEqual(a["state_hash"], b["state_hash"])
            self.assertEqual(a["version"], b["version"])


if __name__ == "__main__":
    unittest.main()
