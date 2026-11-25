from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from lock_wait_lab.engine import Engine
from lock_wait_lab.errors import ErrorCode, LabError
from lock_wait_lab.samples import load_bundle
from lock_wait_lab.store import Store


ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"
MAIN = ROOT / "main.py"


def tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(str(path.relative_to(root)).encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


class SampleTests(unittest.TestCase):
    def test_generator_is_byte_reproducible(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            first = Path(temp_dir) / "first"
            second = Path(temp_dir) / "second"
            subprocess.run(
                [sys.executable, str(ROOT / "generate_samples.py"), "--out", str(first)],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                [sys.executable, str(ROOT / "generate_samples.py"), "--out", str(second)],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(tree_digest(first), tree_digest(second))
            self.assertEqual(tree_digest(first), tree_digest(SAMPLES))

    def test_duplicate_table_reports_code_and_first_field(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            copied = Path(temp_dir) / "samples"
            shutil.copytree(SAMPLES, copied)
            database_path = copied / "sample_db.json"
            database = json.loads(database_path.read_text(encoding="utf-8"))
            database["tables"][1]["name"] = database["tables"][0]["name"]
            database_path.write_text(
                json.dumps(database, sort_keys=True, indent=2),
                encoding="utf-8",
            )
            with self.assertRaises(LabError) as captured:
                load_bundle(copied)
            self.assertEqual(
                captured.exception.code, ErrorCode.DB_DUPLICATE_TABLE
            )
            self.assertEqual(captured.exception.field, "tables[1].name")

    def test_negative_relative_budget_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            copied = Path(temp_dir) / "samples"
            shutil.copytree(SAMPLES, copied)
            policy_path = copied / "policies" / "v1.json"
            policy = json.loads(policy_path.read_text(encoding="utf-8"))
            policy["flat_mapping"]["tx:T06"] = -1
            policy_path.write_text(
                json.dumps(policy, sort_keys=True, indent=2),
                encoding="utf-8",
            )
            with self.assertRaises(LabError) as captured:
                load_bundle(copied)
            self.assertEqual(
                captured.exception.code, ErrorCode.POLICY_V1_INVALID
            )
            self.assertEqual(
                captured.exception.field, "flat_mapping.tx:T06"
            )


class EngineTests(unittest.TestCase):
    def _run(self, db_path: Path) -> dict:
        bundle = load_bundle(SAMPLES)
        store = Store(db_path)
        try:
            engine = Engine.initialize(bundle, store, reset=True)
            return engine.run()
        finally:
            store.close()

    def test_end_to_end_ledger_and_rollback_decisions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            summary = self._run(Path(temp_dir) / "state.sqlite3")
        statuses = {
            tx["tx_id"]: tx["status"] for tx in summary["transactions"]
        }
        self.assertEqual(summary["status"], "COMPLETED")
        self.assertEqual(statuses["T04"], "DEADLOCK_VICTIM_ROLLBACK")
        self.assertEqual(statuses["T06"], "ABANDONED_WAIT_TIMEOUT")
        self.assertEqual(statuses["T09"], "MANUAL_ROLLBACK")
        self.assertEqual(statuses["T12"], "NOT_STARTED_WITHIN_BUDGET")
        self.assertEqual(summary["actual_policy_versions"], [1, 2])
        self.assertEqual(len(summary["policy_switches"]), 1)
        self.assertEqual(summary["policy_switches"][0]["ledger_version"], 2)
        self.assertEqual(summary["ledger_summary"]["entry_count"], 6)
        self.assertEqual(summary["ledger_summary"]["open_entry_count"], 0)
        self.assertEqual(summary["ledger_summary"]["max_cumulative_error_bound_ns"], 1500)

        by_tx = {
            entry["tx_id"]: entry for entry in summary["ledger_entries"]
        }
        self.assertEqual(by_tx["T02"]["status"], "WAIT_GRANTED")
        self.assertEqual(by_tx["T02"]["dequeue_ns"], 1_800_700)
        self.assertEqual(by_tx["T06"]["rounded_ms"], "2.000")
        self.assertEqual(by_tx["T06"]["rounding_direction"], "UP")
        self.assertEqual(by_tx["T06"]["discarded_remainder_ns"], 500)
        self.assertEqual(by_tx["T06"]["own_error_bound_ns"], 500)
        self.assertEqual(by_tx["T06"]["dependency_error_bound_ns"], 500)
        self.assertEqual(by_tx["T06"]["cumulative_error_bound_ns"], 1000)
        self.assertEqual(by_tx["T10"]["policy_generation"], 2)
        self.assertEqual(by_tx["T02"]["policy_generation"], 1)

        bound = {
            tx["tx_id"]: tx["policy_generation"]
            for tx in summary["transactions"]
        }
        self.assertEqual(bound["T01"], 1)
        self.assertEqual(bound["T10"], 2)

        flipped = {
            (record["kind"], record["tx_ids"][0]): record["flip"]
            for record in summary["precision_sensitive_records"]
            if record["tx_ids"]
        }
        self.assertTrue(flipped[("DEADLOCK_VICTIM_SELECTION", "T03")])
        self.assertTrue(flipped[("WAIT_BUDGET_BOUNDARY", "T06")])
        self.assertFalse(flipped[("WAIT_BUDGET_BOUNDARY", "T10")])

    def test_repeated_runs_are_bitwise_identical(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            first = self._run(Path(temp_dir) / "first.sqlite3")
            second = self._run(Path(temp_dir) / "second.sqlite3")
        self.assertEqual(first["state_digest"], second["state_digest"])
        self.assertEqual(first["ledger_digest"], second["ledger_digest"])
        self.assertEqual(first["decisions"], second["decisions"])
        self.assertEqual(
            first["precision_sensitive_records"],
            second["precision_sensitive_records"],
        )

    def test_durable_recovery_probe_matches_fresh_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bundle = load_bundle(SAMPLES)
            fresh = self._run(Path(temp_dir) / "fresh.sqlite3")
            probe_db = Path(temp_dir) / "probe.sqlite3"
            store = Store(probe_db)
            try:
                engine = Engine.initialize(bundle, store, "probe", reset=True)
                try:
                    engine.run(crash_after_step=18, simulate_crash=True)
                except Exception as exc:
                    self.assertEqual(type(exc).__name__, "CrashInjected")
            finally:
                store.close()
            store = Store(probe_db)
            try:
                engine = Engine(bundle, store, "probe")
                engine.mark_resumed()
                recovered = engine.run()
                journal = store.journal("probe")
            finally:
                store.close()
        self.assertEqual(recovered["state_digest"], fresh["state_digest"])
        self.assertEqual(recovered["ledger_digest"], fresh["ledger_digest"])
        self.assertEqual(recovered["decisions"], fresh["decisions"])
        self.assertEqual(recovered["resume_count"], 1)
        self.assertEqual([row["step"] for row in journal], list(range(len(journal))))


class CliTests(unittest.TestCase):
    def _invoke(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(MAIN), *args],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )

    def test_actual_sigkill_then_resume_and_verify(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = str(Path(temp_dir) / "kill.sqlite3")
            load = self._invoke(
                "load", "--samples", str(SAMPLES), "--db", db
            )
            self.assertEqual(load.returncode, 0, load.stdout + load.stderr)
            crashed = self._invoke(
                "run",
                "--samples",
                str(SAMPLES),
                "--db",
                db,
                "--crash-after-step",
                "20",
            )
            self.assertEqual(crashed.returncode, -9, crashed.stdout + crashed.stderr)
            resumed = self._invoke(
                "resume", "--samples", str(SAMPLES), "--db", db
            )
            self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
            resumed_data = json.loads(resumed.stdout)["data"]
            self.assertEqual(resumed_data["status"], "COMPLETED")
            self.assertEqual(resumed_data["resume_count"], 1)
            self.assertEqual(resumed_data["committed_step"], 71)

            verify = self._invoke(
                "verify", "--samples", str(SAMPLES), "--db", db
            )
            self.assertEqual(verify.returncode, 0, verify.stdout + verify.stderr)
            verify_data = json.loads(verify.stdout)["data"]
            self.assertTrue(verify_data["ok"])
            self.assertTrue(verify_data["recovery"]["all_match"])
            self.assertTrue(
                all(check["ok"] for check in verify_data["checks"])
            )
            self.assertEqual(
                resumed_data["state_digest"],
                verify_data["fresh_replay"]["state_digest"],
            )

            rollback = self._invoke(
                "snapshot",
                "--samples",
                str(SAMPLES),
                "--db",
                db,
                "--generation",
                "1",
            )
            self.assertEqual(rollback.returncode, 2)
            error = json.loads(rollback.stdout)["error"]
            self.assertEqual(error["code"], ErrorCode.POLICY_VERSION_ROLLBACK)
            self.assertEqual(error["field"], "generation")


if __name__ == "__main__":
    unittest.main()
