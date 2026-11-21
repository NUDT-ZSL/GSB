"""SQLite persistence, versioned ledger snapshots, and recovery primitives."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from fractions import Fraction
from pathlib import Path
from typing import Any, Iterator

from .errors import fail
from .fixed import fraction_text
from .model import Scenario
from .util import FileLock, canonical_json, sha256_text

SCHEMA_VERSION = 1


DDL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS run_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    run_status TEXT NOT NULL,
    run_id TEXT NOT NULL,
    clock_ms INTEGER NOT NULL,
    watermark_tick INTEGER NOT NULL,
    next_queue_id INTEGER NOT NULL,
    next_holder_id INTEGER NOT NULL,
    next_enqueue_seq INTEGER NOT NULL,
    next_registration_seq INTEGER NOT NULL,
    next_decision_seq INTEGER NOT NULL,
    next_event_seq INTEGER NOT NULL,
    round_no INTEGER NOT NULL,
    ledger_version INTEGER NOT NULL,
    last_deadlock_check_tick INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS transactions (
    tx_key TEXT PRIMARY KEY,
    tx_no INTEGER NOT NULL UNIQUE,
    script_hash TEXT NOT NULL,
    isolation_level TEXT NOT NULL,
    wait_budget_ms INTEGER NOT NULL,
    max_retries INTEGER NOT NULL,
    status TEXT NOT NULL,
    reason TEXT,
    next_op INTEGER NOT NULL,
    operation_version INTEGER NOT NULL,
    retries_used INTEGER NOT NULL,
    started_tick INTEGER,
    ended_tick INTEGER,
    waited_ms INTEGER NOT NULL,
    waiting_deadline_tick INTEGER,
    waiting_queue_id INTEGER,
    rollback_after_ops INTEGER
);

CREATE TABLE IF NOT EXISTS op_commits (
    tx_key TEXT NOT NULL,
    op_index INTEGER NOT NULL,
    operation_version INTEGER NOT NULL,
    op_id TEXT NOT NULL,
    operation_kind TEXT NOT NULL,
    resource_key TEXT NOT NULL,
    completed_tick INTEGER NOT NULL,
    outcome TEXT NOT NULL,
    wait_registration_seq INTEGER,
    PRIMARY KEY (tx_key, op_index, operation_version)
);

CREATE TABLE IF NOT EXISTS lock_holders (
    holder_id INTEGER PRIMARY KEY,
    table_name TEXT NOT NULL,
    resource_kind TEXT NOT NULL CHECK (resource_kind IN ('row', 'range')),
    resource_key TEXT NOT NULL,
    row_value TEXT,
    range_lo TEXT,
    range_hi TEXT,
    tx_key TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('S', 'X')),
    acquire_seq INTEGER NOT NULL,
    acquire_tick INTEGER NOT NULL,
    op_index INTEGER NOT NULL,
    UNIQUE (resource_key, tx_key)
);

CREATE INDEX IF NOT EXISTS idx_holders_table ON lock_holders(table_name);
CREATE INDEX IF NOT EXISTS idx_holders_tx ON lock_holders(tx_key);

CREATE TABLE IF NOT EXISTS lock_queue (
    queue_id INTEGER PRIMARY KEY,
    enqueue_seq INTEGER NOT NULL UNIQUE,
    table_name TEXT NOT NULL,
    resource_kind TEXT NOT NULL CHECK (resource_kind IN ('row', 'range')),
    resource_key TEXT NOT NULL,
    row_value TEXT,
    range_lo TEXT,
    range_hi TEXT,
    tx_key TEXT NOT NULL,
    op_index INTEGER NOT NULL,
    operation_version INTEGER NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('S', 'X')),
    enqueue_tick INTEGER NOT NULL,
    deadline_tick INTEGER NOT NULL,
    status TEXT NOT NULL,
    resolved_tick INTEGER,
    resolution TEXT,
    UNIQUE (tx_key, op_index, operation_version)
);

CREATE INDEX IF NOT EXISTS idx_queue_waiting
    ON lock_queue(status, enqueue_seq);
CREATE INDEX IF NOT EXISTS idx_queue_table
    ON lock_queue(table_name, status);

CREATE TABLE IF NOT EXISTS wait_records (
    registration_seq INTEGER PRIMARY KEY,
    record_id TEXT NOT NULL UNIQUE,
    tx_key TEXT NOT NULL,
    op_index INTEGER NOT NULL,
    operation_version INTEGER NOT NULL,
    resource_key TEXT NOT NULL,
    mode TEXT NOT NULL,
    enqueue_tick INTEGER NOT NULL,
    complete_tick INTEGER NOT NULL,
    raw_wait_ms INTEGER NOT NULL,
    scale_num INTEGER NOT NULL,
    scale_den INTEGER NOT NULL,
    exact_units TEXT NOT NULL,
    fixed_units INTEGER NOT NULL,
    remainder TEXT NOT NULL,
    rounding_direction TEXT NOT NULL,
    error_units TEXT NOT NULL,
    wait_ordinal INTEGER NOT NULL,
    wait_outcome TEXT NOT NULL,
    UNIQUE (tx_key, op_index, operation_version)
);

CREATE INDEX IF NOT EXISTS idx_waits_tx ON wait_records(tx_key, registration_seq);

CREATE TABLE IF NOT EXISTS rounding_entries (
    registration_seq INTEGER PRIMARY KEY,
    tx_key TEXT NOT NULL,
    wait_ordinal INTEGER NOT NULL,
    weight_position INTEGER NOT NULL,
    raw_wait_ms INTEGER NOT NULL,
    exact_units TEXT NOT NULL,
    fixed_units INTEGER NOT NULL,
    discarded_remainder TEXT NOT NULL,
    rounding_direction TEXT NOT NULL,
    signed_error_units TEXT NOT NULL,
    resource_key TEXT NOT NULL,
    lock_mode TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS decisions (
    decision_seq INTEGER PRIMARY KEY,
    decision_id TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    tick INTEGER NOT NULL,
    tx_key TEXT,
    reason TEXT NOT NULL,
    cycle_json TEXT,
    basis_json TEXT NOT NULL,
    ledger_sensitive INTEGER NOT NULL,
    sensitivity_json TEXT,
    created_round INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    event_seq INTEGER PRIMARY KEY,
    tick INTEGER NOT NULL,
    round_no INTEGER NOT NULL,
    tx_key TEXT,
    event_type TEXT NOT NULL,
    details_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ledger_versions (
    version INTEGER PRIMARY KEY,
    tick INTEGER NOT NULL,
    watermark_tick INTEGER NOT NULL,
    run_status TEXT NOT NULL,
    transaction_count INTEGER NOT NULL,
    wait_count INTEGER NOT NULL,
    rounding_count INTEGER NOT NULL,
    decision_count INTEGER NOT NULL,
    holder_count INTEGER NOT NULL,
    queue_count INTEGER NOT NULL,
    holder_summary TEXT NOT NULL,
    queue_summary TEXT NOT NULL,
    state_digest TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ledger_transactions (
    version INTEGER NOT NULL,
    tx_key TEXT NOT NULL,
    tx_no INTEGER NOT NULL,
    isolation_level TEXT NOT NULL,
    wait_budget_ms INTEGER NOT NULL,
    max_retries INTEGER NOT NULL,
    status TEXT NOT NULL,
    reason TEXT,
    next_op INTEGER NOT NULL,
    started_tick INTEGER,
    ended_tick INTEGER,
    PRIMARY KEY (version, tx_key)
);

CREATE TABLE IF NOT EXISTS ledger_wait_records (
    version INTEGER NOT NULL,
    registration_seq INTEGER NOT NULL,
    record_id TEXT NOT NULL,
    tx_key TEXT NOT NULL,
    op_index INTEGER NOT NULL,
    resource_key TEXT NOT NULL,
    mode TEXT NOT NULL,
    enqueue_tick INTEGER NOT NULL,
    complete_tick INTEGER NOT NULL,
    raw_wait_ms INTEGER NOT NULL,
    fixed_units INTEGER NOT NULL,
    exact_units TEXT NOT NULL,
    remainder TEXT NOT NULL,
    rounding_direction TEXT NOT NULL,
    error_units TEXT NOT NULL,
    wait_ordinal INTEGER NOT NULL,
    wait_outcome TEXT NOT NULL,
    PRIMARY KEY (version, registration_seq)
);

CREATE TABLE IF NOT EXISTS ledger_rounding_entries (
    version INTEGER NOT NULL,
    registration_seq INTEGER NOT NULL,
    tx_key TEXT NOT NULL,
    weight_position INTEGER NOT NULL,
    discarded_remainder TEXT NOT NULL,
    signed_error_units TEXT NOT NULL,
    resource_key TEXT NOT NULL,
    lock_mode TEXT NOT NULL,
    PRIMARY KEY (version, registration_seq)
);

CREATE TABLE IF NOT EXISTS ledger_decisions (
    version INTEGER NOT NULL,
    decision_seq INTEGER NOT NULL,
    decision_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    tick INTEGER NOT NULL,
    tx_key TEXT,
    reason TEXT NOT NULL,
    cycle_json TEXT,
    basis_json TEXT NOT NULL,
    ledger_sensitive INTEGER NOT NULL,
    sensitivity_json TEXT,
    PRIMARY KEY (version, decision_seq)
);

CREATE TABLE IF NOT EXISTS ledger_lock_holders (
    version INTEGER NOT NULL,
    holder_id INTEGER NOT NULL,
    tx_key TEXT NOT NULL,
    resource_key TEXT NOT NULL,
    mode TEXT NOT NULL,
    acquire_seq INTEGER NOT NULL,
    PRIMARY KEY (version, holder_id)
);

CREATE TABLE IF NOT EXISTS ledger_lock_queue (
    version INTEGER NOT NULL,
    queue_id INTEGER NOT NULL,
    enqueue_seq INTEGER NOT NULL,
    tx_key TEXT NOT NULL,
    resource_key TEXT NOT NULL,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    enqueue_tick INTEGER NOT NULL,
    deadline_tick INTEGER NOT NULL,
    resolved_tick INTEGER,
    PRIMARY KEY (version, queue_id)
);

CREATE TABLE IF NOT EXISTS ledger_pointer (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    version INTEGER NOT NULL
);

CREATE TRIGGER IF NOT EXISTS prevent_exact_overgrant
BEFORE INSERT ON lock_holders
WHEN EXISTS (
    SELECT 1
    FROM lock_holders h
    WHERE h.resource_key = NEW.resource_key
      AND (h.mode = 'X' OR NEW.mode = 'X')
)
BEGIN
    SELECT RAISE(ABORT, 'E_LOCK_OVERGRANT');
END;

CREATE TRIGGER IF NOT EXISTS prevent_holder_mode_downgrade
BEFORE UPDATE OF mode ON lock_holders
WHEN OLD.mode = 'X' AND NEW.mode = 'S'
BEGIN
    SELECT RAISE(ABORT, 'E_LOCK_DOWNGRADE');
END;
"""


TEMP_SNAPSHOT_TABLES = (
    "_stage_transactions",
    "_stage_waits",
    "_stage_rounding",
    "_stage_decisions",
    "_stage_holders",
    "_stage_queue",
)


class Store:
    def __init__(self, workdir: Path):
        self.workdir = workdir
        self.db_path = workdir / "ledger.sqlite"
        self.lock_path = workdir / "ledger.lock"
        self.tmp_dir = workdir / "tmp"

    def exists(self) -> bool:
        return self.db_path.exists()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    @contextmanager
    def write_tx(self, *, snapshot: bool = True) -> Iterator[sqlite3.Connection]:
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        with FileLock(self.lock_path):
            conn = self._connect()
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("BEGIN IMMEDIATE")
                yield conn
                if snapshot:
                    self._snapshot_ledger(conn)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            finally:
                conn.close()

    def read_conn(self) -> sqlite3.Connection:
        if not self.db_path.exists():
            fail("E_NO_RUN", f"ledger does not exist: {self.db_path}")
        uri = self.db_path.resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("BEGIN")
        return conn

    def initialize(self, scenario: Scenario, run_id: str) -> None:
        with self.write_tx() as conn:
            conn.executescript(DDL)
            conn.execute("BEGIN IMMEDIATE")
            for table in (
                "meta",
                "run_state",
                "transactions",
                "op_commits",
                "lock_holders",
                "lock_queue",
                "wait_records",
                "rounding_entries",
                "decisions",
                "events",
                "ledger_versions",
                "ledger_transactions",
                "ledger_wait_records",
                "ledger_rounding_entries",
                "ledger_decisions",
                "ledger_lock_holders",
                "ledger_lock_queue",
                "ledger_pointer",
            ):
                conn.execute(f"DELETE FROM {table}")
            conn.execute(
                "INSERT INTO meta(key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            conn.execute(
                "INSERT INTO meta(key, value) VALUES ('scenario_sha256', ?)",
                (scenario.source_sha256,),
            )
            conn.execute(
                "INSERT INTO run_state("
                "singleton, run_status, run_id, clock_ms, watermark_tick,"
                "next_queue_id, next_holder_id, next_enqueue_seq,"
                "next_registration_seq, next_decision_seq, next_event_seq,"
                "round_no, ledger_version, last_deadlock_check_tick"
                ") VALUES (1, 'RUNNING', ?, 0, -1, 1, 1, 1, 1, 1, 1, 0, 0, -1)",
                (run_id,),
            )
            for tx in sorted(scenario.transactions, key=lambda item: item.tx_no):
                conn.execute(
                    "INSERT INTO transactions("
                    "tx_key, tx_no, script_hash, isolation_level, wait_budget_ms,"
                    "max_retries, status, reason, next_op, operation_version,"
                    "retries_used, started_tick, ended_tick, waited_ms,"
                    "waiting_deadline_tick, waiting_queue_id, rollback_after_ops"
                    ") VALUES (?, ?, ?, ?, ?, ?, 'NOT_STARTED', NULL, 0, 1, 0,"
                    "NULL, NULL, 0, NULL, NULL, ?)",
                    (
                        tx.tx_key,
                        tx.tx_no,
                        scenario.source_sha256,
                        tx.isolation,
                        tx.wait_budget_ms,
                        tx.max_retries,
                        tx.rollback_after_ops,
                    ),
                )
            self._log_event(
                conn,
                tick=0,
                tx_key=None,
                event_type="RUN_INITIALIZED",
                details={"scenario_sha256": scenario.source_sha256, "run_id": run_id},
            )

    def _next(self, conn: sqlite3.Connection, column: str) -> int:
        if column not in {
            "next_queue_id",
            "next_holder_id",
            "next_enqueue_seq",
            "next_registration_seq",
            "next_decision_seq",
            "next_event_seq",
        }:
            fail("E_INTERNAL", f"bad sequence column {column}", exit_code=4)
        row = conn.execute(f"SELECT {column} FROM run_state WHERE singleton=1").fetchone()
        value = int(row[column])
        conn.execute(
            f"UPDATE run_state SET {column}={column}+1 WHERE singleton=1"
        )
        return value

    def _log_event(
        self,
        conn: sqlite3.Connection,
        *,
        tick: int,
        tx_key: str | None,
        event_type: str,
        details: dict[str, Any],
        event_seq: int | None = None,
    ) -> int:
        state = conn.execute(
            "SELECT next_event_seq, round_no FROM run_state WHERE singleton=1"
        ).fetchone()
        seq = event_seq if event_seq is not None else int(state["next_event_seq"])
        if event_seq is None:
            conn.execute(
                "UPDATE run_state SET next_event_seq=next_event_seq+1 WHERE singleton=1"
            )
        conn.execute(
            "INSERT INTO events(event_seq, tick, round_no, tx_key, event_type, details_json)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                seq,
                tick,
                int(state["round_no"]),
                tx_key,
                event_type,
                canonical_json(details),
            ),
        )
        return seq

    def _snapshot_ledger(self, conn: sqlite3.Connection) -> None:
        state = conn.execute("SELECT * FROM run_state WHERE singleton=1").fetchone()
        old_pointer = conn.execute(
            "SELECT version FROM ledger_pointer WHERE singleton=1"
        ).fetchone()
        version = (int(old_pointer["version"]) if old_pointer else 0) + 1

        for table in TEMP_SNAPSHOT_TABLES:
            conn.execute(f"DROP TABLE IF EXISTS temp.{table}")
        conn.execute(
            "CREATE TEMP TABLE _stage_transactions AS SELECT * FROM transactions"
        )
        conn.execute("CREATE TEMP TABLE _stage_waits AS SELECT * FROM wait_records")
        conn.execute(
            "CREATE TEMP TABLE _stage_rounding AS SELECT * FROM rounding_entries"
        )
        conn.execute("CREATE TEMP TABLE _stage_decisions AS SELECT * FROM decisions")
        conn.execute("CREATE TEMP TABLE _stage_holders AS SELECT * FROM lock_holders")
        conn.execute("CREATE TEMP TABLE _stage_queue AS SELECT * FROM lock_queue")

        holder_rows = [
            {
                "holder_id": row["holder_id"],
                "tx_key": row["tx_key"],
                "resource_key": row["resource_key"],
                "mode": row["mode"],
                "acquire_seq": row["acquire_seq"],
            }
            for row in conn.execute(
                "SELECT holder_id, tx_key, resource_key, mode, acquire_seq"
                " FROM _stage_holders ORDER BY holder_id"
            )
        ]
        queue_rows = [
            {
                "queue_id": row["queue_id"],
                "enqueue_seq": row["enqueue_seq"],
                "tx_key": row["tx_key"],
                "resource_key": row["resource_key"],
                "mode": row["mode"],
                "status": row["status"],
                "enqueue_tick": row["enqueue_tick"],
                "deadline_tick": row["deadline_tick"],
            }
            for row in conn.execute(
                "SELECT queue_id, enqueue_seq, tx_key, resource_key, mode, status,"
                " enqueue_tick, deadline_tick FROM _stage_queue ORDER BY enqueue_seq"
            )
        ]
        wait_rows = [
            {
                "registration_seq": row["registration_seq"],
                "tx_key": row["tx_key"],
                "op_index": row["op_index"],
                "raw_wait_ms": row["raw_wait_ms"],
                "fixed_units": row["fixed_units"],
                "remainder": row["remainder"],
                "wait_outcome": row["wait_outcome"],
            }
            for row in conn.execute(
                "SELECT registration_seq, tx_key, op_index, raw_wait_ms, fixed_units,"
                " remainder, wait_outcome FROM _stage_waits"
                " ORDER BY registration_seq"
            )
        ]
        decision_rows = [
            {
                "decision_seq": row["decision_seq"],
                "kind": row["kind"],
                "tx_key": row["tx_key"],
                "reason": row["reason"],
                "ledger_sensitive": row["ledger_sensitive"],
            }
            for row in conn.execute(
                "SELECT decision_seq, kind, tx_key, reason, ledger_sensitive"
                " FROM _stage_decisions ORDER BY decision_seq"
            )
        ]
        transaction_rows = [
            {
                "tx_key": row["tx_key"],
                "tx_no": row["tx_no"],
                "isolation_level": row["isolation_level"],
                "wait_budget_ms": row["wait_budget_ms"],
                "max_retries": row["max_retries"],
                "status": row["status"],
                "reason": row["reason"],
                "next_op": row["next_op"],
            }
            for row in conn.execute(
                "SELECT tx_key, tx_no, isolation_level, wait_budget_ms, max_retries,"
                " status, reason, next_op FROM _stage_transactions ORDER BY tx_no"
            )
        ]
        digest_payload = {
            "tick": state["clock_ms"],
            "watermark_tick": state["watermark_tick"],
            "transactions": transaction_rows,
            "holders": holder_rows,
            "queue": queue_rows,
            "waits": wait_rows,
            "decisions": decision_rows,
        }
        digest = sha256_text(canonical_json(digest_payload))

        conn.execute(
            "INSERT INTO ledger_versions("
            "version, tick, watermark_tick, run_status, transaction_count,"
            "wait_count, rounding_count, decision_count, holder_count, queue_count,"
            "holder_summary, queue_summary, state_digest"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                version,
                state["clock_ms"],
                state["watermark_tick"],
                state["run_status"],
                len(transaction_rows),
                len(wait_rows),
                conn.execute("SELECT COUNT(*) FROM _stage_rounding").fetchone()[0],
                len(decision_rows),
                len(holder_rows),
                len(queue_rows),
                canonical_json(holder_rows),
                canonical_json(queue_rows),
                digest,
            ),
        )
        conn.execute(
            "INSERT INTO ledger_transactions("
            "version, tx_key, tx_no, isolation_level, wait_budget_ms, max_retries,"
            " status, reason, next_op, started_tick, ended_tick"
            ") SELECT ?, tx_key, tx_no, isolation_level, wait_budget_ms, max_retries,"
            " status, reason, next_op, started_tick, ended_tick FROM _stage_transactions",
            (version,),
        )
        conn.execute(
            "INSERT INTO ledger_wait_records("
            "version, registration_seq, record_id, tx_key, op_index, resource_key,"
            " mode, enqueue_tick, complete_tick, raw_wait_ms, fixed_units, exact_units,"
            " remainder, rounding_direction, error_units, wait_ordinal, wait_outcome"
            ") SELECT ?, registration_seq, record_id, tx_key, op_index, resource_key,"
            " mode, enqueue_tick, complete_tick, raw_wait_ms, fixed_units, exact_units,"
            " remainder, rounding_direction, error_units, wait_ordinal, wait_outcome"
            " FROM _stage_waits",
            (version,),
        )
        conn.execute(
            "INSERT INTO ledger_rounding_entries("
            "version, registration_seq, tx_key, weight_position,"
            " discarded_remainder, signed_error_units, resource_key, lock_mode"
            ") SELECT ?, registration_seq, tx_key, weight_position,"
            " discarded_remainder, signed_error_units, resource_key, lock_mode"
            " FROM _stage_rounding",
            (version,),
        )
        conn.execute(
            "INSERT INTO ledger_decisions("
            "version, decision_seq, decision_id, kind, tick, tx_key, reason,"
            " cycle_json, basis_json, ledger_sensitive, sensitivity_json"
            ") SELECT ?, decision_seq, decision_id, kind, tick, tx_key, reason,"
            " cycle_json, basis_json, ledger_sensitive, sensitivity_json"
            " FROM _stage_decisions",
            (version,),
        )
        conn.execute(
            "INSERT INTO ledger_lock_holders("
            "version, holder_id, tx_key, resource_key, mode, acquire_seq"
            ") SELECT ?, holder_id, tx_key, resource_key, mode, acquire_seq"
            " FROM _stage_holders",
            (version,),
        )
        conn.execute(
            "INSERT INTO ledger_lock_queue("
            "version, queue_id, enqueue_seq, tx_key, resource_key, mode, status,"
            " enqueue_tick, deadline_tick, resolved_tick"
            ") SELECT ?, queue_id, enqueue_seq, tx_key, resource_key, mode, status,"
            " enqueue_tick, deadline_tick, resolved_tick FROM _stage_queue",
            (version,),
        )
        conn.execute(
            "INSERT INTO ledger_pointer(singleton, version) VALUES (1, ?)"
            " ON CONFLICT(singleton) DO UPDATE SET version=excluded.version",
            (version,),
        )
        conn.execute(
            "UPDATE run_state SET ledger_version=? WHERE singleton=1",
            (version,),
        )

    def current_pointer(self, conn: sqlite3.Connection) -> sqlite3.Row:
        row = conn.execute(
            "SELECT p.version, v.* FROM ledger_pointer p"
            " JOIN ledger_versions v ON v.version=p.version"
            " WHERE p.singleton=1"
        ).fetchone()
        if row is None:
            fail("E_NO_LEDGER", "ledger pointer is missing")
        return row

    def status(self, error_budget: Fraction | None = None) -> dict[str, Any]:
        conn = self.read_conn()
        try:
            pointer = self.current_pointer(conn)
            version = int(pointer["version"])
            transactions = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM ledger_transactions WHERE version=? ORDER BY tx_no",
                    (version,),
                )
            ]
            waits = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM ledger_wait_records WHERE version=?"
                    " ORDER BY registration_seq",
                    (version,),
                )
            ]
            rounding = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM ledger_rounding_entries WHERE version=?"
                    " ORDER BY registration_seq",
                    (version,),
                )
            ]
            decisions = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM ledger_decisions WHERE version=?"
                    " ORDER BY decision_seq",
                    (version,),
                )
            ]
            holders = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM ledger_lock_holders WHERE version=?"
                    " ORDER BY holder_id",
                    (version,),
                )
            ]
            queue = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM ledger_lock_queue WHERE version=?"
                    " ORDER BY enqueue_seq",
                    (version,),
                )
            ]
            state = dict(
                conn.execute("SELECT * FROM run_state WHERE singleton=1").fetchone()
            )
        finally:
            conn.rollback()
            conn.close()

        waits_by_tx: dict[str, list[dict[str, Any]]] = {}
        rounding_by_tx: dict[str, list[dict[str, Any]]] = {}
        for wait in waits:
            waits_by_tx.setdefault(wait["tx_key"], []).append(wait)
        for entry in rounding:
            rounding_by_tx.setdefault(entry["tx_key"], []).append(entry)
        decisions_by_tx: dict[str, list[dict[str, Any]]] = {}
        sensitive_participants: set[str] = set()
        for decision in decisions:
            if decision["tx_key"]:
                decisions_by_tx.setdefault(decision["tx_key"], []).append(decision)
            if decision["sensitivity_json"]:
                sensitivity = json.loads(decision["sensitivity_json"])
                for pair in sensitivity.get("pairwise", []):
                    sensitive_participants.add(pair["left_tx"])
                    sensitive_participants.add(pair["right_tx"])

        tx_output: list[dict[str, Any]] = []
        for tx in transactions:
            tx_waits = waits_by_tx.get(tx["tx_key"], [])
            tx_rounding = rounding_by_tx.get(tx["tx_key"], [])
            fixed_total = sum(int(wait["fixed_units"]) for wait in tx_waits)
            error_total = sum(
                (Fraction(entry["signed_error_units"]) for entry in tx_rounding),
                Fraction(0, 1),
            )
            source_chain = [
                {
                    "registration_seq": entry["registration_seq"],
                    "weight_position": entry["weight_position"],
                    "discarded_remainder": entry["discarded_remainder"],
                    "signed_error_units": entry["signed_error_units"],
                    "resource_key": entry["resource_key"],
                    "lock_mode": entry["lock_mode"],
                }
                for entry in sorted(
                    tx_rounding, key=lambda item: item["weight_position"]
                )
            ]
            tx_decisions = sorted(
                decisions_by_tx.get(tx["tx_key"], []),
                key=lambda item: item["decision_seq"],
            )
            tx_output.append(
                {
                    "tx_key": tx["tx_key"],
                    "tx_no": tx["tx_no"],
                    "isolation_level": tx["isolation_level"],
                    "wait_budget_ms": tx["wait_budget_ms"],
                    "max_retries": tx["max_retries"],
                    "status": tx["status"],
                    "reason": tx["reason"],
                    "started_tick": tx["started_tick"],
                    "ended_tick": tx["ended_tick"],
                    "wait_duration": {
                        "raw_wait_ms": sum(
                            int(wait["raw_wait_ms"]) for wait in tx_waits
                        ),
                        "fixed_units": fixed_total,
                        "exact_units": fraction_text(
                            sum(
                                (Fraction(wait["exact_units"]) for wait in tx_waits),
                                Fraction(0, 1),
                            )
                        ),
                    },
                    "error_upper_bound": fraction_text(error_total),
                    "error_budget_exceeded": (
                        error_total > error_budget if error_budget is not None else None
                    ),
                    "ledger_sensitive": any(
                        bool(decision["ledger_sensitive"]) for decision in tx_decisions
                    )
                    or tx["tx_key"] in sensitive_participants,
                    "wait_records": [
                        {
                            "registration_seq": wait["registration_seq"],
                            "op_index": wait["op_index"],
                            "resource_key": wait["resource_key"],
                            "mode": wait["mode"],
                            "enqueue_tick": wait["enqueue_tick"],
                            "complete_tick": wait["complete_tick"],
                            "raw_wait_ms": wait["raw_wait_ms"],
                            "fixed_units": wait["fixed_units"],
                            "exact_units": wait["exact_units"],
                            "discarded_remainder": wait["remainder"],
                            "rounding_direction": wait["rounding_direction"],
                            "signed_error_units": wait["error_units"],
                            "wait_ordinal": wait["wait_ordinal"],
                            "outcome": wait["wait_outcome"],
                        }
                        for wait in sorted(
                            tx_waits, key=lambda item: item["registration_seq"]
                        )
                    ],
                    "error_source_chain": source_chain,
                    "decisions": [
                        {
                            "decision_id": decision["decision_id"],
                            "kind": decision["kind"],
                            "tick": decision["tick"],
                            "reason": decision["reason"],
                            "cycle": json.loads(decision["cycle_json"])
                            if decision["cycle_json"]
                            else None,
                            "basis": json.loads(decision["basis_json"]),
                            "ledger_sensitive": bool(decision["ledger_sensitive"]),
                            "sensitivity": json.loads(decision["sensitivity_json"])
                            if decision["sensitivity_json"]
                            else None,
                        }
                        for decision in tx_decisions
                    ],
                }
            )
        return {
            "ok": True,
            "schema_version": SCHEMA_VERSION,
            "run": {
                "run_id": state["run_id"],
                "status": state["run_status"],
                "logical_clock_ms": state["clock_ms"],
                "watermark_tick": state["watermark_tick"],
                "round_no": state["round_no"],
                "ledger_version": version,
                "ledger_digest": pointer["state_digest"],
                "wait_count": pointer["wait_count"],
                "decision_count": pointer["decision_count"],
            },
            "transactions": tx_output,
            "decisions": [
                {
                    "decision_id": decision["decision_id"],
                    "kind": decision["kind"],
                    "tick": decision["tick"],
                    "tx_key": decision["tx_key"],
                    "reason": decision["reason"],
                    "cycle": json.loads(decision["cycle_json"])
                    if decision["cycle_json"]
                    else None,
                    "basis": json.loads(decision["basis_json"]),
                    "ledger_sensitive": bool(decision["ledger_sensitive"]),
                    "sensitivity": json.loads(decision["sensitivity_json"])
                    if decision["sensitivity_json"]
                    else None,
                }
                for decision in decisions
            ],
            "locks": {
                "holders": holders,
                "queue": queue,
            },
        }

