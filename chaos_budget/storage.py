from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

from .errors import ChaosError
from .utils import canonical_json


SCHEMA_VERSION = "1"

SCHEMA_SQL = """
CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
) WITHOUT ROWID;

CREATE TABLE decision_counter (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    next_seq INTEGER NOT NULL
);
INSERT INTO decision_counter (id, next_seq) VALUES (1, 0);

CREATE TABLE decisions (
    decision_seq INTEGER PRIMARY KEY,
    decision_id TEXT NOT NULL UNIQUE,
    idempotency_key TEXT UNIQUE,
    experiment_id TEXT,
    generation INTEGER,
    round_id TEXT,
    clock_ms INTEGER NOT NULL,
    action TEXT NOT NULL,
    result TEXT NOT NULL,
    constraint_code TEXT,
    request_hash TEXT,
    evidence_json TEXT NOT NULL,
    prev_hash TEXT,
    evidence_hash TEXT NOT NULL UNIQUE
);
CREATE INDEX idx_decisions_experiment ON decisions(experiment_id, decision_seq);

CREATE TABLE experiments (
    experiment_id TEXT PRIMARY KEY,
    target_service TEXT NOT NULL,
    fault_type TEXT NOT NULL,
    fault_params_json TEXT NOT NULL,
    priority INTEGER NOT NULL,
    start_clock_ms INTEGER NOT NULL,
    expected_duration_ms INTEGER NOT NULL CHECK (expected_duration_ms > 0),
    round_id TEXT NOT NULL,
    state TEXT NOT NULL,
    disposition TEXT NOT NULL,
    outcome_category TEXT NOT NULL,
    generation INTEGER NOT NULL DEFAULT 1,
    revision INTEGER NOT NULL DEFAULT 0,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    backoff_until_ms INTEGER NOT NULL DEFAULT 0,
    admitted_clock_ms INTEGER,
    reserved_points INTEGER NOT NULL DEFAULT 0,
    reserved_round_ms INTEGER NOT NULL DEFAULT 0,
    last_charge_points INTEGER NOT NULL DEFAULT 0,
    first_constraint_code TEXT,
    first_constraint_json TEXT,
    last_decision_seq INTEGER,
    updated_clock_ms INTEGER NOT NULL DEFAULT 0,
    raw_json TEXT NOT NULL
);
CREATE INDEX idx_experiments_schedule ON experiments(state, start_clock_ms, priority);

CREATE TABLE admissions (
    experiment_id TEXT NOT NULL,
    generation INTEGER NOT NULL,
    decision_seq INTEGER NOT NULL UNIQUE,
    clock_ms INTEGER NOT NULL,
    charge_points INTEGER NOT NULL,
    round_charge_ms INTEGER NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    PRIMARY KEY (experiment_id, generation)
) WITHOUT ROWID;

CREATE TABLE budget_accounts (
    account_id TEXT PRIMARY KEY,
    account_type TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    round_id TEXT,
    currency TEXT NOT NULL,
    initial_balance INTEGER NOT NULL CHECK (initial_balance >= 0),
    balance INTEGER NOT NULL CHECK (balance >= 0),
    total_debited INTEGER NOT NULL DEFAULT 0 CHECK (total_debited >= 0),
    total_credited INTEGER NOT NULL DEFAULT 0 CHECK (total_credited >= 0),
    version INTEGER NOT NULL DEFAULT 0,
    UNIQUE(account_type, owner_id, currency)
);

CREATE TABLE budget_ledger (
    ledger_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_seq INTEGER NOT NULL,
    account_id TEXT NOT NULL,
    delta_amount INTEGER NOT NULL,
    balance_after INTEGER NOT NULL CHECK (balance_after >= 0),
    reason TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    evidence_hash TEXT NOT NULL
);
CREATE INDEX idx_ledger_account ON budget_ledger(account_id, ledger_seq);
CREATE INDEX idx_ledger_decision ON budget_ledger(decision_seq);

CREATE TABLE observation_inbox (
    experiment_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    request_hash TEXT NOT NULL,
    request_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('received', 'processed', 'rejected')),
    decision_seq INTEGER,
    received_clock_ms INTEGER NOT NULL,
    updated_clock_ms INTEGER NOT NULL,
    result_json TEXT,
    PRIMARY KEY (experiment_id, sequence)
) WITHOUT ROWID;
CREATE INDEX idx_inbox_status ON observation_inbox(status, experiment_id, sequence);

CREATE TABLE policy_view (
    policy_key TEXT PRIMARY KEY,
    selected_json TEXT NOT NULL
) WITHOUT ROWID;
"""


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path) if str(path) != ":memory:" else Path(":memory:")
        self.conn: Optional[sqlite3.Connection] = None

    def connect(self) -> "Database":
        if str(self.path) == ":memory:":
            self.conn = sqlite3.connect(":memory:", timeout=30, isolation_level=None)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
        assert self.conn is not None
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=30000")
        return self

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def __enter__(self) -> "Database":
        return self.connect()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        assert self.conn is not None
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise

    @classmethod
    def initialize(
        cls,
        path: Path | str,
        bundle: Dict[str, Any],
        *,
        reset: bool = False,
    ) -> "Database":
        db_path = Path(path)
        if str(db_path) != ":memory:":
            if reset:
                for suffix in ("", "-wal", "-shm"):
                    candidate = Path(str(db_path) + suffix)
                    try:
                        candidate.unlink()
                    except FileNotFoundError:
                        pass
            elif db_path.exists():
                db = cls(db_path).connect()
                row = db.conn.execute(  # type: ignore[union-attr]
                    "SELECT value FROM meta WHERE key='bundle_fingerprint'"
                ).fetchone()
                db.close()
                if row is None or row["value"] != bundle["fingerprint"]:
                    raise ChaosError(
                        "STATE_ALREADY_EXISTS",
                        "state database exists with different inputs; rerun load with --reset",
                        details={"db_path": str(db_path)},
                        exit_code=4,
                    )
                return cls(db_path).connect()

        db = cls(db_path).connect()
        assert db.conn is not None
        db.conn.executescript(SCHEMA_SQL)

        config = bundle["budget_config"]
        with db.transaction() as conn:
            meta_items = {
                "schema_version": SCHEMA_VERSION,
                "bundle_fingerprint": bundle["fingerprint"],
                "sample_dir": bundle.get("sample_dir", ""),
                "budget_config_json": canonical_json(config),
                "policy_values_json": canonical_json(bundle["policy_view"]["values"]),
                "policy_view_json": canonical_json(bundle["policy_view"]),
                "clock_watermark_ms": str(bundle.get("injection_clock_ms", 0)),
            }
            conn.executemany(
                "INSERT INTO meta(key, value) VALUES(?, ?)",
                list(meta_items.items()),
            )

            for key, selected in bundle["policy_view"]["policies"].items():
                conn.execute(
                    "INSERT INTO policy_view(policy_key, selected_json) VALUES(?, ?)",
                    (key, canonical_json(selected)),
                )

            rounds = sorted({item["round_id"] for item in bundle["experiments"]})
            for round_id in rounds:
                account_id = f"round:{round_id}:logical_time_ms"
                conn.execute(
                    """
                    INSERT INTO budget_accounts(
                        account_id, account_type, owner_id, round_id, currency,
                        initial_balance, balance
                    ) VALUES(?, 'round', ?, ?, 'logical_ms', ?, ?)
                    """,
                    (
                        account_id,
                        round_id,
                        round_id,
                        config["round_logical_time_budget_ms"],
                        config["round_logical_time_budget_ms"],
                    ),
                )

            for experiment in sorted(bundle["experiments"], key=lambda item: item["experiment_id"]):
                conn.execute(
                    """
                    INSERT INTO experiments(
                        experiment_id, target_service, fault_type, fault_params_json,
                        priority, start_clock_ms, expected_duration_ms, round_id,
                        state, disposition, outcome_category, raw_json, updated_clock_ms
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 'budgeted_not_started',
                              'within_budget_not_started', ?, ?)
                    """,
                    (
                        experiment["experiment_id"],
                        experiment["target_service"],
                        experiment["fault_type"],
                        canonical_json(experiment["fault_params"]),
                        experiment["priority"],
                        experiment["start_clock_ms"],
                        experiment["expected_duration_ms"],
                        experiment["round_id"],
                        "pending_admission",
                        canonical_json(experiment.get("raw", experiment)),
                        experiment["start_clock_ms"],
                    ),
                )
                account_id = f"experiment:{experiment['experiment_id']}:points"
                conn.execute(
                    """
                    INSERT INTO budget_accounts(
                        account_id, account_type, owner_id, currency,
                        initial_balance, balance
                    ) VALUES(?, 'experiment', ?, 'points', ?, ?)
                    """,
                    (
                        account_id,
                        experiment["experiment_id"],
                        config["per_experiment_budget"],
                        config["per_experiment_budget"],
                    ),
                )

        return db

    def meta(self, key: str, default: Optional[str] = None) -> Optional[str]:
        assert self.conn is not None
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row is not None else default

    def meta_json(self, key: str, default: Any = None) -> Any:
        value = self.meta(key)
        return json.loads(value) if value is not None else default

    def watermark(self) -> int:
        value = self.meta("clock_watermark_ms", "0") or "0"
        return int(value)
