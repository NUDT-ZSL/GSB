from __future__ import annotations

import json
import sqlite3
import unittest

from lockledger.engine import Engine
from lockledger.storage import Store
from lockledger.validation import validate_scenario

from .support import (
    ROOT,
    SCENARIO,
    fresh_dir,
    run_cli,
    wait_signature,
)


class MainScenarioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workdir = fresh_dir("main")
        cls.completed = run_cli(
            "run",
            "--sample",
            str(SCENARIO),
            "--workdir",
            str(cls.workdir),
        )
        cls.status = json.loads(cls.completed.stdout)

    def test_run_succeeds(self) -> None:
        self.assertEqual(self.completed.returncode, 0, self.completed.stderr)
        self.assertEqual(self.status["run"]["status"], "COMPLETE")
        self.assertEqual(self.status["run"]["wait_count"], 5)
        self.assertEqual(self.status["run"]["decision_count"], 3)

    def test_wait_sequence_and_rounding(self) -> None:
        waits = {
            tx["tx_key"]: [
                (
                    wait["registration_seq"],
                    wait["raw_wait_ms"],
                    wait["fixed_units"],
                    wait["exact_units"],
                    wait["discarded_remainder"],
                )
                for wait in tx["wait_records"]
            ]
            for tx in self.status["transactions"]
        }
        self.assertEqual(
            waits["T08_RANGE_WRITER"],
            [(1, 1, 0, "2/5", "2/5")],
        )
        self.assertEqual(
            waits["T07_INVENTORY_WAITER"],
            [(2, 2, 0, "4/5", "4/5")],
        )
        self.assertEqual(
            waits["T02_TIMEOUT"],
            [(3, 2, 0, "4/5", "4/5")],
        )
        self.assertEqual(
            waits["T03_DEADLOCK_LOW_TXNO"],
            [(4, 1, 0, "2/5", "2/5")],
        )
        self.assertEqual(
            waits["T04_DEADLOCK_HIGH_TXNO"],
            [(5, 2, 0, "4/5", "4/5")],
        )

    def test_three_endings_are_distinct(self) -> None:
        by_key = {tx["tx_key"]: tx for tx in self.status["transactions"]}
        self.assertEqual(by_key["T02_TIMEOUT"]["status"], "TIMEOUT")
        self.assertEqual(
            by_key["T02_TIMEOUT"]["reason"], "WAIT_BUDGET_EXCEEDED"
        )
        self.assertEqual(by_key["T09_ACTIVE_ROLLBACK"]["status"], "ABORTED")
        self.assertEqual(
            by_key["T09_ACTIVE_ROLLBACK"]["reason"], "VOLUNTARY_ROLLBACK"
        )
        self.assertEqual(
            by_key["T03_DEADLOCK_LOW_TXNO"]["status"], "SACRIFICED"
        )

    def test_timeout_releases_held_lock(self) -> None:
        timeout_decision = next(
            decision
            for decision in self.status["decisions"]
            if decision["kind"] == "WAIT_TIMEOUT"
        )
        held = timeout_decision["basis"]["held_locks_released"]
        self.assertEqual(
            held,
            [
                {
                    "resource_key": "row:jobs:7",
                    "mode": "X",
                    "acquire_seq": 2,
                }
            ],
        )
        self.assertEqual(self.status["locks"]["holders"], [])

    def test_deadlock_cycle_and_sensitive_flip(self) -> None:
        decision = next(
            decision
            for decision in self.status["decisions"]
            if decision["kind"] == "DEADLOCK_SACRIFICE"
        )
        self.assertEqual(
            decision["cycle"],
            ["T03_DEADLOCK_LOW_TXNO", "T04_DEADLOCK_HIGH_TXNO"],
        )
        self.assertEqual(decision["tx_key"], "T03_DEADLOCK_LOW_TXNO")
        self.assertEqual(decision["basis"]["victim_wait_raw_ms"], 1)
        self.assertEqual(decision["basis"]["victim_deviation_units"], "2/5")
        self.assertEqual(decision["basis"]["victim_error_upper_bound"], "2/5")
        self.assertTrue(decision["ledger_sensitive"])
        self.assertTrue(
            decision["sensitivity"]["would_flip_at_higher_scale"]
        )
        pair = decision["sensitivity"]["pairwise"][0]
        self.assertEqual(pair["current_order"], "TIE")
        self.assertEqual(pair["higher_scale_victim"], "T04_DEADLOCK_HIGH_TXNO")
        by_key = {tx["tx_key"]: tx for tx in self.status["transactions"]}
        self.assertTrue(by_key["T03_DEADLOCK_LOW_TXNO"]["ledger_sensitive"])
        self.assertTrue(by_key["T04_DEADLOCK_HIGH_TXNO"]["ledger_sensitive"])

    def test_error_source_chain(self) -> None:
        by_key = {tx["tx_key"]: tx for tx in self.status["transactions"]}
        tx = by_key["T04_DEADLOCK_HIGH_TXNO"]
        self.assertEqual(tx["error_upper_bound"], "4/5")
        self.assertFalse(tx["error_budget_exceeded"])
        self.assertEqual(
            tx["error_source_chain"],
            [
                {
                    "registration_seq": 5,
                    "weight_position": 1,
                    "discarded_remainder": "4/5",
                    "signed_error_units": "4/5",
                    "resource_key": "row:accounts:2",
                    "lock_mode": "S",
                }
            ],
        )

    def test_repeated_lock_and_declared_lock(self) -> None:
        by_key = {tx["tx_key"]: tx for tx in self.status["transactions"]}
        self.assertEqual(
            by_key["T10_DECLARED_LOCK"]["status"], "COMMITTED"
        )
        self.assertEqual(by_key["T10_DECLARED_LOCK"]["wait_records"], [])
        connection = sqlite3.connect(self.workdir / "ledger.sqlite")
        try:
            count = connection.execute(
                "SELECT COUNT(*) FROM op_commits WHERE tx_key='T10_DECLARED_LOCK'"
            ).fetchone()[0]
            outcomes = [
                row[0]
                for row in connection.execute(
                    "SELECT outcome FROM op_commits"
                    " WHERE tx_key='T10_DECLARED_LOCK' ORDER BY op_index"
                )
            ]
        finally:
            connection.close()
        self.assertEqual(count, 3)
        self.assertIn("REUSED", outcomes)

    def test_verify_and_stable_status(self) -> None:
        verify = run_cli("verify", "--workdir", str(self.workdir))
        self.assertEqual(verify.returncode, 0, verify.stderr)
        self.assertTrue(json.loads(verify.stdout)["ok"])
        status_one = run_cli("status", "--workdir", str(self.workdir))
        status_two = run_cli("status", "--workdir", str(self.workdir))
        self.assertEqual(status_one.stdout, status_two.stdout)

    def test_all_queues_drained(self) -> None:
        self.assertEqual(
            [entry for entry in self.status["locks"]["queue"] if entry["status"] == "WAITING"],
            [],
        )


class NotStartedTests(unittest.TestCase):
    def test_horizon_does_not_synthesize_timeout(self) -> None:
        workdir = fresh_dir("notstarted")
        completed = run_cli(
            "run",
            "--sample",
            str(ROOT / "samples" / "notstarted.json"),
            "--workdir",
            str(workdir),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        status = json.loads(completed.stdout)
        by_key = {tx["tx_key"]: tx for tx in status["transactions"]}
        self.assertEqual(status["run"]["status"], "HORIZON")
        self.assertEqual(by_key["T01_LONG_JOB_HOLDER"]["status"], "ABORTED")
        self.assertEqual(by_key["T01_LONG_JOB_HOLDER"]["reason"], "GLOBAL_HORIZON")
        self.assertEqual(by_key["T02_TIMEOUT"]["status"], "NOT_STARTED")
        self.assertEqual(
            by_key["T02_TIMEOUT"]["reason"], "BUDGET_NOT_STARTED"
        )
        self.assertEqual(by_key["T02_TIMEOUT"]["wait_records"], [])
        self.assertFalse(
            any(d["tx_key"] == "T02_TIMEOUT" and d["kind"] == "WAIT_TIMEOUT"
                for d in status["decisions"])
        )
        verify = run_cli("verify", "--workdir", str(workdir))
        self.assertEqual(verify.returncode, 0, verify.stderr)


class LockSemanticsTests(unittest.TestCase):
    def test_shared_compatibility_fifo_and_reuse(self) -> None:
        raw = {
            "database": {
                "tables": [
                    {
                        "name": "rows",
                        "primary_key": "id",
                        "columns": [
                            {"name": "id", "type": "INTEGER"},
                            {"name": "value", "type": "TEXT"},
                        ],
                    }
                ],
                "rows": {"rows": [{"id": 1, "value": "x"}]},
            },
            "transactions": [
                {
                    "tx_id": "T1",
                    "tx_no": 1,
                    "isolation": "SERIALIZABLE",
                    "wait_budget_ms": 5,
                    "max_retries": 1,
                    "operations": [
                        {"type": "read", "table": "rows", "row": 1},
                        {"type": "read", "table": "rows", "row": 1},
                    ],
                },
                {
                    "tx_id": "T2",
                    "tx_no": 2,
                    "isolation": "READ_COMMITTED",
                    "wait_budget_ms": 5,
                    "max_retries": 1,
                    "operations": [{"type": "read", "table": "rows", "row": 1}],
                },
                {
                    "tx_id": "T3",
                    "tx_no": 3,
                    "isolation": "SERIALIZABLE",
                    "wait_budget_ms": 5,
                    "max_retries": 1,
                    "operations": [
                        {
                            "type": "write",
                            "table": "rows",
                            "row": 1,
                            "values": {"value": "three"},
                        }
                    ],
                },
                {
                    "tx_id": "T4",
                    "tx_no": 4,
                    "isolation": "REPEATABLE_READ",
                    "wait_budget_ms": 5,
                    "max_retries": 1,
                    "operations": [{"type": "read", "table": "rows", "row": 1}],
                },
            ],
            "config": {
                "clock_step_ms": 1,
                "scale": 1,
                "tolerance": 0,
                "error_budget": 1,
                "max_concurrency": 4,
                "deadlock_check_period_ms": 1,
                "max_rounds": 50,
            },
        }
        scenario = validate_scenario(raw, source_path="<memory>")
        workdir = fresh_dir("fifo")
        store = Store(workdir)
        Engine(scenario, store).initialize("fifo")
        status = Engine(scenario, store).run()
        by_key = {tx["tx_key"]: tx for tx in status["transactions"]}
        self.assertEqual(by_key["T1"]["wait_records"], [])
        self.assertEqual(by_key["T2"]["wait_records"], [])
        self.assertEqual(by_key["T3"]["wait_records"][0]["raw_wait_ms"], 1)
        self.assertEqual(by_key["T4"]["wait_records"][0]["raw_wait_ms"], 1)
        queue = status["locks"]["queue"]
        self.assertLess(queue[0]["enqueue_seq"], queue[1]["enqueue_seq"])
        connection = sqlite3.connect(workdir / "ledger.sqlite")
        try:
            outcomes = [
                row[0]
                for row in connection.execute(
                    "SELECT outcome FROM op_commits WHERE tx_key='T1'"
                    " ORDER BY op_index"
                )
            ]
        finally:
            connection.close()
        self.assertEqual(outcomes, ["ACQUIRED", "REUSED"])

    def test_database_trigger_blocks_exact_overgrant(self) -> None:
        workdir = fresh_dir("overgrant")
        completed = run_cli(
            "run",
            "--sample",
            str(SCENARIO),
            "--workdir",
            str(workdir),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        connection = sqlite3.connect(workdir / "ledger.sqlite")
        try:
            connection.execute("BEGIN")
            values = (
                9001,
                "accounts",
                "row",
                "manual:row:1",
                "1",
                None,
                None,
                "T01_LONG_JOB_HOLDER",
                "X",
                9001,
                0,
                0,
            )
            connection.execute(
                "INSERT INTO lock_holders("
                "holder_id, table_name, resource_kind, resource_key, row_value,"
                " range_lo, range_hi, tx_key, mode, acquire_seq, acquire_tick, op_index"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                values,
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO lock_holders("
                    "holder_id, table_name, resource_kind, resource_key, row_value,"
                    " range_lo, range_hi, tx_key, mode, acquire_seq, acquire_tick, op_index"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        9002,
                        "accounts",
                        "row",
                        "manual:row:1",
                        "1",
                        None,
                        None,
                        "T02_TIMEOUT",
                        "X",
                        9002,
                        0,
                        0,
                    ),
                )
        finally:
            connection.rollback()
            connection.close()

    def test_wait_budget_is_cumulative_per_transaction(self) -> None:
        raw = {
            "database": {
                "tables": [
                    {
                        "name": "rows",
                        "primary_key": "id",
                        "columns": [
                            {"name": "id", "type": "INTEGER"},
                            {"name": "value", "type": "TEXT"},
                        ],
                    }
                ],
                "rows": {
                    "rows": [{"id": index, "value": str(index)} for index in range(1, 7)]
                },
            },
            "transactions": [
                {
                    "tx_id": "T1",
                    "tx_no": 1,
                    "isolation": "SERIALIZABLE",
                    "wait_budget_ms": 10,
                    "max_retries": 1,
                    "operations": [
                        {"type": "write", "table": "rows", "row": 1,
                         "values": {"value": "one"}},
                        {"type": "write", "table": "rows", "row": 3,
                         "values": {"value": "three"}},
                    ],
                },
                {
                    "tx_id": "T2",
                    "tx_no": 2,
                    "isolation": "READ_COMMITTED",
                    "wait_budget_ms": 2,
                    "max_retries": 1,
                    "operations": [
                        {"type": "read", "table": "rows", "row": 1},
                        {"type": "read", "table": "rows", "row": 2},
                    ],
                },
                {
                    "tx_id": "T3",
                    "tx_no": 3,
                    "isolation": "SERIALIZABLE",
                    "wait_budget_ms": 10,
                    "max_retries": 1,
                    "operations": [
                        {"type": "write", "table": "rows", "row": 2,
                         "values": {"value": "two"}},
                        {"type": "write", "table": "rows", "row": 4,
                         "values": {"value": "four"}},
                        {"type": "write", "table": "rows", "row": 5,
                         "values": {"value": "five"}},
                        {"type": "write", "table": "rows", "row": 6,
                         "values": {"value": "six"}},
                    ],
                },
            ],
            "config": {
                "clock_step_ms": 1,
                "scale": 1,
                "tolerance": 0,
                "error_budget": 1,
                "max_concurrency": 3,
                "deadlock_check_period_ms": 1,
                "max_rounds": 50,
            },
        }
        scenario = validate_scenario(raw, source_path="<memory>")
        workdir = fresh_dir("cumulative-budget")
        store = Store(workdir)
        Engine(scenario, store).initialize("cumulative-budget")
        status = Engine(scenario, store).run()
        t2 = next(tx for tx in status["transactions"] if tx["tx_key"] == "T2")
        self.assertEqual(t2["status"], "TIMEOUT")
        self.assertEqual(
            [(wait["raw_wait_ms"], wait["outcome"]) for wait in t2["wait_records"]],
            [(1, "GRANTED"), (1, "TIMEOUT")],
        )
        timeout = next(
            decision for decision in status["decisions"]
            if decision["kind"] == "WAIT_TIMEOUT"
        )
        self.assertEqual(timeout["basis"]["waited_before_ms"], 1)
        self.assertEqual(timeout["basis"]["remaining_budget_ms"], 1)


class DeterminismAndConcurrencyTests(unittest.TestCase):
    def test_repeated_runs_and_worker_mode_match(self) -> None:
        outputs = []
        for name, workers in (("det-a", 1), ("det-b", 1), ("det-workers", 2)):
            workdir = fresh_dir(name)
            completed = run_cli(
                "run",
                "--sample",
                str(SCENARIO),
                "--workdir",
                str(workdir),
                "--workers",
                str(workers),
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            outputs.append(json.loads(completed.stdout))
        self.assertEqual(
            wait_signature(outputs[0]),
            wait_signature(outputs[1]),
        )
        self.assertEqual(
            wait_signature(outputs[0]),
            wait_signature(outputs[2]),
        )
        self.assertEqual(
            outputs[0]["run"]["ledger_digest"],
            outputs[1]["run"]["ledger_digest"],
        )
        self.assertEqual(
            outputs[0]["run"]["ledger_digest"],
            outputs[2]["run"]["ledger_digest"],
        )

    def test_verify_detects_tampered_snapshot(self) -> None:
        workdir = fresh_dir("tampered")
        completed = run_cli(
            "run",
            "--sample",
            str(SCENARIO),
            "--workdir",
            str(workdir),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        connection = sqlite3.connect(workdir / "ledger.sqlite")
        try:
            version = connection.execute(
                "SELECT version FROM ledger_pointer WHERE singleton=1"
            ).fetchone()[0]
            connection.execute(
                "UPDATE ledger_wait_records SET raw_wait_ms=raw_wait_ms+100"
                " WHERE version=? AND registration_seq=1",
                (version,),
            )
            connection.commit()
        finally:
            connection.close()
        verify = run_cli("verify", "--workdir", str(workdir))
        self.assertEqual(verify.returncode, 3)
        result = json.loads(verify.stdout)
        self.assertFalse(result["ok"])
        self.assertTrue(result["failures"])


if __name__ == "__main__":
    unittest.main()

