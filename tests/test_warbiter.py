from __future__ import annotations

import copy
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from warbiter.canonical import load_json_file
from warbiter.engine import (
    apply_script,
    list_conflicts,
    load_database_context,
    resolve_pending,
    resume,
    status_report,
    update_policy,
    verify,
)
from warbiter.errors import WarbiterError
from warbiter.model import parse_config, parse_schema, parse_script
from warbiter.storage import initialize_database


ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"


def run_cli(*args: str, env: dict[str, str] | None = None):
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    return subprocess.run(
        [sys.executable, "-m", "warbiter", *args],
        cwd=ROOT,
        env=merged_env,
        text=True,
        capture_output=True,
        check=False,
    )


class BaseCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema_raw = load_json_file(SAMPLES / "schema.json")
        cls.schema_v2_raw = load_json_file(SAMPLES / "schema_policy_v2.json")
        cls.config_raw = load_json_file(SAMPLES / "arbiter.json")
        cls.script_raw = load_json_file(SAMPLES / "transactions.json")
        cls.schema = parse_schema(cls.schema_raw)
        cls.schema_v2 = parse_schema(cls.schema_v2_raw)
        cls.config = parse_config(cls.config_raw)
        cls.script = parse_script(cls.script_raw, cls.schema, cls.config)

    def make_db(self, directory: str, name: str = "state.db") -> Path:
        path = Path(directory) / name
        initialize_database(str(path), self.schema, self.config)
        return path

    def build_main_db(self, directory: str, name: str = "main.db") -> Path:
        path = self.make_db(directory, name)
        apply_script(str(path), self.script, self.schema, self.config)
        resolve_pending(str(path))
        return path


class ValidationTests(BaseCase):
    def test_schema_rejects_duplicate_column_and_bad_type(self):
        minimal = copy.deepcopy(self.schema_raw)
        minimal["tenants"][0]["tables"][0]["columns"][0].pop("nullable")
        parsed = parse_schema(minimal)
        self.assertFalse(
            parsed.get_table("acme", "inventory").column_map["sku"].nullable
        )

        duplicate = copy.deepcopy(self.schema_raw)
        duplicate["tenants"][0]["tables"][0]["columns"].append(
            {"name": "qty", "type": "integer", "nullable": True}
        )
        with self.assertRaises(WarbiterError) as raised:
            parse_schema(duplicate)
        self.assertEqual(raised.exception.code, "E_BAD_SCHEMA")

        bad_type = copy.deepcopy(self.schema_raw)
        bad_type["tenants"][0]["tables"][0]["columns"][1]["type"] = "float"
        with self.assertRaises(WarbiterError) as raised:
            parse_schema(bad_type)
        self.assertEqual(raised.exception.code, "E_BAD_SCHEMA")

    def test_policy_rejects_row_and_merge_overlap(self):
        invalid = copy.deepcopy(self.schema_raw)
        policy = invalid["tenants"][0]["tables"][0]["conflict_policy"]
        policy["row_overwrite_columns"].append("qty")
        with self.assertRaises(WarbiterError) as raised:
            parse_schema(invalid)
        self.assertEqual(raised.exception.code, "E_BAD_POLICY")

    def test_script_rejects_bad_clock_unknown_column_and_type(self):
        bad_clock = copy.deepcopy(self.script_raw)
        bad_clock["transactions"][0]["start_clock_ms"] = 0
        with self.assertRaises(WarbiterError) as raised:
            parse_script(bad_clock, self.schema, self.config)
        self.assertEqual(raised.exception.code, "E_BAD_SCRIPT")

        unknown_column = copy.deepcopy(self.script_raw)
        unknown_column["transactions"][0]["writes"][0]["values"]["missing"] = 1
        with self.assertRaises(WarbiterError) as raised:
            parse_script(unknown_column, self.schema, self.config)
        self.assertEqual(raised.exception.code, "E_BAD_SCRIPT")

        wrong_type = copy.deepcopy(self.script_raw)
        wrong_type["transactions"][1]["writes"][0]["values"]["qty"] = "eight"
        with self.assertRaises(WarbiterError) as raised:
            parse_script(wrong_type, self.schema, self.config)
        self.assertEqual(raised.exception.code, "E_BAD_SCRIPT")

    def test_invalid_cli_input_writes_no_database(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "must-not-exist.db"
            bad = copy.deepcopy(self.script_raw)
            bad["transactions"][0]["tx_id"] = 0
            bad_path = Path(directory) / "bad.json"
            bad_path.write_text(json.dumps(bad), encoding="utf-8")
            result = run_cli(
                "apply",
                "--db",
                str(db),
                "--schema",
                str(SAMPLES / "schema.json"),
                "--config",
                str(SAMPLES / "arbiter.json"),
                "--script",
                str(bad_path),
            )
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["error"]["code"], "E_BAD_SCRIPT")
            self.assertFalse(db.exists())


class WorkflowTests(BaseCase):
    def test_empty_database_verifies(self):
        with tempfile.TemporaryDirectory() as directory:
            db = self.make_db(directory)
            result = verify(str(db))
            self.assertTrue(result["ok"])
            self.assertTrue(all(result["checks"].values()))
            self.assertEqual(result["counts"]["transactions"], {})

    def test_full_workflow_evidence_and_final_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            db = self.make_db(directory)
            applied = apply_script(
                str(db), self.script, self.schema, self.config, do_resolve=False
            )
            self.assertEqual(len(applied["inserted"]), 18)
            self.assertEqual(applied["conflicts_detected"], 20)
            connection = sqlite3.connect(db)
            try:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM tx WHERE status='committed'"
                    ).fetchone()[0],
                    18,
                )
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM row_state").fetchone()[0],
                    0,
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM conflicts WHERE status='pending'"
                    ).fetchone()[0],
                    20,
                )
            finally:
                connection.close()

            listed = list_conflicts(str(db))
            self.assertEqual(len(listed["conflicts"]), 20)
            resolved = resolve_pending(str(db))
            self.assertEqual(resolved["resolved_conflicts"], 20)
            self.assertEqual(
                resolved["status_counts"]["transactions"],
                {
                    "aborted_assertion": 1,
                    "accepted": 15,
                    "rejected_conflict": 1,
                    "suspended_manual": 1,
                },
            )
            report = status_report(str(db))
            values = {
                (table["tenant"], table["table"], str(row["row_key"])): row
                for table in report["tables"]
                for row in table["rows"]
            }
            self.assertEqual(values[("acme", "inventory", "SKU-1")]["values"]["qty"], 8)
            self.assertEqual(
                values[("acme", "inventory", "SKU-1")]["values"]["price"], 11.0
            )
            self.assertEqual(
                values[("acme", "inventory", "SKU-3")]["values"]["qty"], 9
            )
            self.assertEqual(
                values[("acme", "inventory", "SKU-3")]["values"]["status"], "revised"
            )
            self.assertEqual(
                values[("globex", "orders", "O-1")]["values"]["state"], "new"
            )
            self.assertEqual(
                values[("globex", "orders", "O-1")]["values"]["memo"], "late"
            )
            self.assertEqual(
                values[("globex", "metrics", "1")]["values"],
                {"id": 1, "label": "b", "value": 12},
            )
            self.assertTrue(values[("globex", "metrics", "2")]["deleted"])
            self.assertEqual(values[("globex", "metrics", "2")]["values"], {})
            self.assertEqual(values[("globex", "metrics", "2")]["last_tx_id"], 1206)
            self.assertTrue(values[("acme", "documents", "DOC-1")]["suspended"])

            connection = sqlite3.connect(db)
            connection.row_factory = sqlite3.Row
            try:
                stale = connection.execute(
                    "SELECT * FROM assertion_evidence WHERE tx_id=1003"
                ).fetchone()
                stale_evidence = json.loads(stale["evidence_json"])
                self.assertFalse(bool(stale["result"]))
                self.assertEqual(stale_evidence["recorded_observation"], {
                    "qty": 12,
                    "status": "available",
                })
                self.assertEqual(stale_evidence["record_values"]["qty"], 8)
                self.assertEqual(stale_evidence["action"], "rollback")

                fresh = connection.execute(
                    "SELECT * FROM assertion_evidence WHERE tx_id=1204"
                ).fetchone()
                self.assertTrue(bool(fresh["result"]))
                self.assertEqual(
                    json.loads(fresh["evidence_json"])["record_values"]["value"], 11
                )

                mixed = connection.execute(
                    """
                    SELECT evidence_json FROM conflicts
                    WHERE right_tx_id=1008 AND left_tx_id=1006
                    """
                ).fetchone()
                mixed_evidence = json.loads(mixed["evidence_json"])
                strategies = {
                    item["column"]: item["applicable_strategy"]
                    for item in mixed_evidence["column_evidence"]
                }
                self.assertEqual(strategies["qty"], "column_merge")
                self.assertEqual(strategies["note"], "column_merge")
                self.assertEqual(strategies["price"], "last_write_wins")
                self.assertEqual(strategies["status"], "last_write_wins")

                pseudo = connection.execute(
                    """
                    SELECT kind, decision FROM conflicts
                    WHERE left_tx_id=1002 AND right_tx_id=1009
                    """
                ).fetchone()
                self.assertEqual(pseudo["kind"], "pseudo")
                self.assertEqual(pseudo["decision"], "safe_merge")

                final_write = connection.execute(
                    """
                    SELECT values_json FROM tx_writes
                    WHERE tx_id=1002 AND tenant='acme'
                      AND table_name='inventory' AND row_key_json='"SKU-1"'
                    """
                ).fetchall()
                self.assertEqual(len(final_write), 1)
                self.assertEqual(json.loads(final_write[0]["values_json"])["qty"], 8)
                audit = connection.execute(
                    """
                    SELECT before_json, after_json FROM tx_audit
                    WHERE tx_id=1002 AND column_name='qty'
                    ORDER BY op_index
                    """
                ).fetchall()
                self.assertEqual(
                    [(
                        json.loads(row["before_json"])
                        if row["before_json"] is not None
                        else None,
                        json.loads(row["after_json"]),
                    )
                     for row in audit],
                    [(None, 7), (7, 8)],
                )

                deletion = connection.execute(
                    """
                    SELECT kind, decision, evidence_json FROM conflicts
                    WHERE left_tx_id=1205 AND right_tx_id=1206
                    """
                ).fetchone()
                self.assertEqual(deletion["kind"], "true")
                self.assertEqual(deletion["decision"], "later_accepted")
                deletion_evidence = json.loads(deletion["evidence_json"])
                self.assertEqual(
                    set(deletion_evidence["right"]["write_columns"]),
                    {"id", "value", "label"},
                )
            finally:
                connection.close()

            verified = verify(str(db))
            self.assertTrue(verified["ok"])
            self.assertTrue(all(verified["checks"].values()))

    def test_idempotent_apply_and_resolve(self):
        with tempfile.TemporaryDirectory() as directory:
            db = self.build_main_db(directory)
            before = status_report(str(db))
            repeated = apply_script(str(db), self.script, self.schema, self.config)
            self.assertEqual(repeated["inserted"], [])
            self.assertEqual(len(repeated["skipped_idempotent"]), 18)
            self.assertEqual(repeated["conflicts_detected"], 0)
            no_change = resolve_pending(str(db))
            self.assertFalse(no_change["changed"])
            after = status_report(str(db))
            self.assertEqual(before, after)
            self.assertTrue(verify(str(db))["ok"])

    def test_verify_detects_tampering(self):
        with tempfile.TemporaryDirectory() as directory:
            db = self.build_main_db(directory)
            connection = sqlite3.connect(db)
            try:
                connection.execute(
                    """
                    UPDATE row_state SET values_json='{"id":1,"label":"tampered","value":99}'
                    WHERE tenant='globex' AND table_name='metrics'
                    """
                )
                connection.commit()
            finally:
                connection.close()
            with self.assertRaises(WarbiterError) as raised:
                verify(str(db))
            self.assertEqual(raised.exception.code, "E_VERIFY_FAILED")
            self.assertFalse(
                raised.exception.details["checks"]["final_rows_match_replay"]
            )


class DeterminismTests(BaseCase):
    def test_two_runs_have_identical_status_and_ledger(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            db1 = self.build_main_db(first, "one.db")
            db2 = self.build_main_db(second, "two.db")
            status1 = status_report(str(db1))
            status2 = status_report(str(db2))
            verify1 = verify(str(db1))
            verify2 = verify(str(db2))
            self.assertEqual(status1, status2)
            self.assertEqual(verify1["ledger_digest"], verify2["ledger_digest"])
            self.assertEqual(
                verify1["final_rows_digest"], verify2["final_rows_digest"]
            )
            self.assertEqual(status1["engine"]["ledger_digest"], status2["engine"]["ledger_digest"])


class PolicyVersionTests(BaseCase):
    def test_pending_conflicts_keep_their_policy_version(self):
        with tempfile.TemporaryDirectory() as directory:
            db = self.make_db(directory)
            apply_script(str(db), self.script, self.schema, self.config)
            changed = update_policy(str(db), self.schema_v2, self.config)
            self.assertTrue(changed["changed"])
            self.assertEqual(changed["current_policy_version"], 2)
            resolve_pending(str(db))
            connection = sqlite3.connect(db)
            connection.row_factory = sqlite3.Row
            try:
                versions = {
                    row[0]
                    for row in connection.execute(
                        "SELECT DISTINCT policy_version FROM conflicts"
                    )
                }
                self.assertEqual(versions, {1})
                status = connection.execute(
                    "SELECT status FROM tx WHERE tx_id=1102"
                ).fetchone()["status"]
                self.assertEqual(status, "rejected_conflict")
            finally:
                connection.close()
            self.assertTrue(verify(str(db))["ok"])

            db_schema, db_config = load_database_context(str(db))
            new_script_raw = {
                "format": "warbiter-script-v1",
                "transactions": [
                    {
                        "tx_id": 1301,
                        "tenant": "globex",
                        "start_clock_ms": 1100,
                        "isolation": "read_committed",
                        "writes": [
                            {
                                "op": "update",
                                "table": "orders",
                                "row_key": "O-1",
                                "values": {"state": "cancelled"},
                            }
                        ],
                    }
                ],
            }
            new_script = parse_script(new_script_raw, db_schema, db_config)
            apply_script(str(db), new_script, db_schema, db_config)
            resolve_pending(str(db))
            connection = sqlite3.connect(db)
            connection.row_factory = sqlite3.Row
            try:
                tx_status = connection.execute(
                    "SELECT status, policy_version FROM tx WHERE tx_id=1301"
                ).fetchone()
                self.assertEqual(tx_status["status"], "accepted")
                self.assertEqual(tx_status["policy_version"], 2)
                right_versions = {
                    row[0]
                    for row in connection.execute(
                        "SELECT DISTINCT policy_version FROM conflicts WHERE right_tx_id=1301"
                    )
                }
                self.assertEqual(right_versions, {2})
                state = connection.execute(
                    """
                    SELECT values_json FROM row_state
                    WHERE tenant='globex' AND table_name='orders' AND row_key_json='"O-1"'
                    """
                ).fetchone()
                self.assertEqual(json.loads(state["values_json"])["state"], "cancelled")
            finally:
                connection.close()
            self.assertTrue(verify(str(db))["ok"])


class RecoveryTests(BaseCase):
    def test_resolve_kill_then_resume_matches_clean_run(self):
        with tempfile.TemporaryDirectory() as directory:
            crash_db = self.make_db(directory, "crash.db")
            clean_db = self.build_main_db(directory, "clean.db")
            apply_script(str(crash_db), self.script, self.schema, self.config)
            killed = run_cli(
                "resolve",
                "--db",
                str(crash_db),
                env={"WARBITER_FAULT_RESOLVE": "1"},
            )
            self.assertEqual(killed.returncode, 137, killed.stdout + killed.stderr)
            connection = sqlite3.connect(crash_db)
            try:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM tx WHERE status='committed'"
                    ).fetchone()[0],
                    18,
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM conflicts WHERE status='pending'"
                    ).fetchone()[0],
                    20,
                )
            finally:
                connection.close()
            resumed = resume(str(crash_db))
            self.assertEqual(resumed["resolution"]["resolved_conflicts"], 20)
            self.assertEqual(
                status_report(str(crash_db)), status_report(str(clean_db))
            )
            self.assertTrue(verify(str(crash_db))["ok"])

    def test_apply_kill_leaves_no_half_commit_and_reapply_matches(self):
        with tempfile.TemporaryDirectory() as directory:
            crash_db = self.make_db(directory, "apply_crash.db")
            clean_db = self.build_main_db(directory, "apply_clean.db")
            killed = run_cli(
                "apply",
                "--db",
                str(crash_db),
                "--script",
                str(SAMPLES / "transactions.json"),
                env={"WARBITER_FAULT_APPLY": "3"},
            )
            self.assertEqual(killed.returncode, 137, killed.stdout + killed.stderr)
            connection = sqlite3.connect(crash_db)
            try:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM tx").fetchone()[0], 0
                )
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM conflicts").fetchone()[0],
                    0,
                )
            finally:
                connection.close()
            apply_script(str(crash_db), self.script, self.schema, self.config)
            resume(str(crash_db))
            self.assertEqual(
                status_report(str(crash_db)), status_report(str(clean_db))
            )
            self.assertTrue(verify(str(crash_db))["ok"])


class ConcurrencyTests(BaseCase):
    def test_parallel_apply_and_resolution_are_consistent(self):
        with tempfile.TemporaryDirectory() as directory:
            db = self.make_db(directory)
            scripts = sorted((SAMPLES / "transactions_concurrent").glob("*.json"))
            processes = [
                subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "warbiter",
                        "apply",
                        "--db",
                        str(db),
                        "--script",
                        str(script),
                    ],
                    cwd=ROOT,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                for script in scripts
            ]
            outputs = [process.communicate() for process in processes]
            self.assertTrue(
                all(process.returncode == 0 for process in processes),
                "\n".join(stdout + stderr for stdout, stderr in outputs),
            )
            resolved = resolve_pending(str(db))
            self.assertEqual(resolved["resolved_conflicts"], 6)
            report = status_report(str(db))
            metrics = next(
                table
                for table in report["tables"]
                if table["tenant"] == "globex" and table["table"] == "metrics"
            )
            self.assertEqual(metrics["rows"][0]["values"]["id"], 1)
            self.assertEqual(metrics["rows"][0]["values"]["value"], 4)
            self.assertEqual(metrics["rows"][0]["last_tx_id"], 2004)
            self.assertTrue(verify(str(db))["ok"])

            duplicate_script = scripts[0]
            duplicate_processes = [
                subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "warbiter",
                        "apply",
                        "--db",
                        str(db),
                        "--script",
                        str(duplicate_script),
                    ],
                    cwd=ROOT,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                for _ in range(4)
            ]
            duplicate_outputs = [
                process.communicate() for process in duplicate_processes
            ]
            self.assertTrue(
                all(process.returncode == 0 for process in duplicate_processes),
                "\n".join(stdout + stderr for stdout, stderr in duplicate_outputs),
            )
            connection = sqlite3.connect(db)
            try:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM tx WHERE tx_id=2001"
                    ).fetchone()[0],
                    1,
                )
            finally:
                connection.close()
            self.assertTrue(verify(str(db))["ok"])


if __name__ == "__main__":
    unittest.main()
