from __future__ import annotations

import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.helpers import SAMPLES, json_output, load_default, run_cli


def _db_counts(db: Path) -> dict[str, int]:
    conn = sqlite3.connect(db)
    try:
        return {
            "ledger": conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0],
            "pending": conn.execute(
                "SELECT COUNT(*) FROM ledger_attempts WHERE status='pending'"
            ).fetchone()[0],
            "dups": conn.execute(
                "SELECT COUNT(*) FROM (SELECT subtask_id, COUNT(*) c "
                "FROM ledger GROUP BY subtask_id HAVING c > 1)"
            ).fetchone()[0],
            "closures": conn.execute(
                "SELECT COUNT(DISTINCT fingerprint) FROM invalidation_closures"
            ).fetchone()[0],
        }
    finally:
        conn.close()


class InvalidationStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "state.sqlite3"
        load_default(self.db)
        run_cli("build", "--plan-version", "p1", db=self.db, check=True)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_idempotent_build_and_verify(self) -> None:
        first = json_output(run_cli("build", "--plan-version", "p1", db=self.db, check=True))
        second = json_output(run_cli("build", "--plan-version", "p1", db=self.db, check=True))
        self.assertEqual(first["ledger"]["reused"], second["ledger"]["reused"])
        self.assertEqual(0, second["ledger"]["inserted"])
        self.assertEqual(
            first["watermark"]["watermark_version"],
            second["watermark"]["watermark_version"],
        )
        verify = run_cli("verify", "--plan-version", "p1", db=self.db)
        self.assertEqual(0, verify.returncode, verify.stdout)
        self.assertTrue(json_output(verify)["ok"])

    def test_plan_switch_reuses_unaffected_and_protects_removed(self) -> None:
        load = run_cli(
            "load",
            "--plan",
            str(SAMPLES / "plans_v2.json"),
            db=self.db,
            check=True,
        )
        self.assertEqual("p2", json_output(load)["active_plan_version"])
        build = json_output(run_cli("build", "--plan-version", "p2", db=self.db, check=True))
        self.assertEqual(0, build["ledger"]["inserted"])
        self.assertGreater(build["ledger"]["reused"], 0)
        closures = build["invalidations"]
        self.assertEqual(1, len(closures))
        self.assertEqual("plan_version_switch", closures[0]["reason"])
        self.assertGreater(closures[0]["protected_committed_count"], 0)

        again = json_output(run_cli("build", "--plan-version", "p2", db=self.db, check=True))
        self.assertFalse(again["invalidations"][0]["inserted"])
        conn = sqlite3.connect(self.db)
        try:
            protected = json.loads(
                conn.execute(
                    "SELECT protected_json FROM invalidation_closures"
                ).fetchone()[0]
            )
            for subtask_id in protected:
                self.assertIsNotNone(
                    conn.execute(
                        "SELECT 1 FROM ledger WHERE subtask_id=?", (subtask_id,)
                    ).fetchone()
                )
        finally:
            conn.close()

    def test_task_revision_adds_only_affected_chain(self) -> None:
        run_cli(
            "load",
            "--tasks",
            str(SAMPLES / "tasks_revision.jsonl"),
            db=self.db,
            check=True,
        )
        before = _db_counts(self.db)
        build = json_output(run_cli("build", "--plan-version", "p1", db=self.db, check=True))
        self.assertGreater(build["ledger"]["inserted"], 0)
        self.assertGreater(build["ledger"]["reused"], 0)
        revision_closures = [
            item for item in build["invalidations"] if item["reason"] == "task_revision"
        ]
        self.assertEqual(1, len(revision_closures))
        self.assertGreater(revision_closures[0]["protected_committed_count"], 0)
        after = _db_counts(self.db)
        self.assertGreater(after["ledger"], before["ledger"])
        self.assertEqual(0, after["dups"])

        again = json_output(run_cli("build", "--plan-version", "p1", db=self.db, check=True))
        self.assertEqual(0, again["ledger"]["inserted"])
        self.assertGreater(again["ledger"]["reused"], 0)
        self.assertEqual(0, _db_counts(self.db)["dups"])

    def test_hard_crash_then_resume_has_no_duplicate_ledger(self) -> None:
        crash_db = Path(self.temp.name) / "crash.sqlite3"
        load_default(crash_db)
        process = run_cli(
            "build",
            "--plan-version",
            "p1",
            "--crash-after",
            "2",
            db=crash_db,
        )
        self.assertEqual(75, process.returncode, process.stdout + process.stderr)
        crashed = _db_counts(crash_db)
        self.assertGreaterEqual(crashed["ledger"], 2)
        self.assertGreaterEqual(crashed["pending"], 1)
        resumed = json_output(
            run_cli("resume", "--plan-version", "p1", db=crash_db, check=True)
        )
        self.assertGreaterEqual(
            resumed["ledger"]["orphaned_attempts_cleaned"], 1
        )
        final = _db_counts(crash_db)
        self.assertEqual(17, final["ledger"])
        self.assertEqual(0, final["pending"])
        self.assertEqual(0, final["dups"])

    def test_late_result_preserves_accepted_ledger(self) -> None:
        conn = sqlite3.connect(self.db)
        try:
            subtask_id, accepted = conn.execute(
                "SELECT subtask_id, output_digest FROM ledger "
                "ORDER BY subtask_id LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        result = json_output(
            run_cli(
                "inject",
                "--plan-version",
                "p1",
                "--drift-count",
                "0",
                "--anomaly-count",
                "0",
                "--late-subtask-id",
                subtask_id,
                "--late-digest",
                "deadbeef",
                db=self.db,
                check=True,
            )
        )
        self.assertEqual(
            "reject_late_and_keep_accepted", result["late_arrival"]["policy"]
        )
        conn = sqlite3.connect(self.db)
        try:
            current = conn.execute(
                "SELECT output_digest FROM ledger WHERE subtask_id=?",
                (subtask_id,),
            ).fetchone()[0]
            late_count = conn.execute(
                "SELECT COUNT(*) FROM late_arrivals"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(accepted, current)
        self.assertEqual(1, late_count)
        verify = run_cli("verify", "--plan-version", "p1", db=self.db)
        self.assertEqual(0, verify.returncode, verify.stdout)

    def test_parallel_build_commits_each_subtask_once(self) -> None:
        concurrent_db = Path(self.temp.name) / "concurrent.sqlite3"
        load_default(concurrent_db)
        processes = [
            subprocess.Popen(
                [
                    "python3",
                    "-m",
                    "engine_diag",
                    "--db",
                    str(concurrent_db),
                    "build",
                    "--plan-version",
                    "p1",
                ],
                cwd=SAMPLES.parent,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for _ in range(4)
        ]
        outputs = [process.communicate() for process in processes]
        for process, (stdout, stderr) in zip(processes, outputs):
            self.assertEqual(0, process.returncode, stdout + stderr)
        counts = _db_counts(concurrent_db)
        self.assertEqual(17, counts["ledger"])
        self.assertEqual(0, counts["dups"])
        self.assertEqual(0, counts["pending"])

    def test_cache_change_closure_does_not_mutate_ledger(self) -> None:
        conn = sqlite3.connect(self.db)
        try:
            before = dict(
                conn.execute(
                    "SELECT subtask_id, output_digest FROM ledger "
                    "ORDER BY subtask_id"
                ).fetchall()
            )
        finally:
            conn.close()
        load = run_cli(
            "load",
            "--stats",
            str(SAMPLES / "platform_stats_changed.json"),
            db=self.db,
            check=True,
        )
        self.assertTrue(json_output(load)["changed_cache_layers"])
        build = json_output(run_cli("build", "--plan-version", "p1", db=self.db, check=True))
        closures = [
            item for item in build["invalidations"] if item["reason"] == "cache_layer_change"
        ]
        self.assertEqual(1, len(closures))
        self.assertEqual(0, build["ledger"]["inserted"])
        conn = sqlite3.connect(self.db)
        try:
            after = dict(
                conn.execute(
                    "SELECT subtask_id, output_digest FROM ledger "
                    "ORDER BY subtask_id"
                ).fetchall()
            )
        finally:
            conn.close()
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()

