from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA_VERSION = "1"


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS services (
    name TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS topology_meta (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    version TEXT NOT NULL,
    digest TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS policies (
    version TEXT PRIMARY KEY,
    revision INTEGER NOT NULL UNIQUE,
    activated_at_ms INTEGER NOT NULL,
    spec_json TEXT NOT NULL,
    digest TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS policies_activation_idx
    ON policies (activated_at_ms, revision);

CREATE TABLE IF NOT EXISTS policy_activations (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    version TEXT NOT NULL,
    revision INTEGER NOT NULL,
    activated_at_ms INTEGER NOT NULL,
    UNIQUE (version, activated_at_ms)
);

CREATE TABLE IF NOT EXISTS events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    arrival_ms INTEGER NOT NULL,
    trace_id TEXT NOT NULL,
    span_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    digest TEXT NOT NULL,
    processed INTEGER NOT NULL DEFAULT 0 CHECK (processed IN (0, 1)),
    late INTEGER NOT NULL DEFAULT 0 CHECK (late IN (0, 1))
);

CREATE INDEX IF NOT EXISTS events_pending_idx
    ON events (processed, arrival_ms, event_id, sequence);

CREATE TABLE IF NOT EXISTS traces (
    trace_id TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('waiting', 'decided', 'quarantined')),
    first_seen_ms INTEGER NOT NULL,
    deadline_ms INTEGER NOT NULL,
    last_seen_ms INTEGER NOT NULL,
    root_seen INTEGER NOT NULL DEFAULT 0 CHECK (root_seen IN (0, 1)),
    span_count INTEGER NOT NULL DEFAULT 0,
    close_sequence INTEGER,
    decision_id TEXT
);

CREATE INDEX IF NOT EXISTS traces_waiting_idx
    ON traces (status, deadline_ms, trace_id);

CREATE TABLE IF NOT EXISTS spans (
    trace_id TEXT NOT NULL,
    span_id TEXT NOT NULL,
    parent_span_id TEXT,
    service TEXT NOT NULL,
    operation TEXT NOT NULL,
    start_ms INTEGER NOT NULL,
    end_ms INTEGER NOT NULL,
    status_code INTEGER NOT NULL,
    is_error INTEGER NOT NULL CHECK (is_error IN (0, 1)),
    is_root_end INTEGER NOT NULL CHECK (is_root_end IN (0, 1)),
    attributes_json TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    digest TEXT NOT NULL,
    arrival_ms INTEGER NOT NULL,
    late INTEGER NOT NULL DEFAULT 0 CHECK (late IN (0, 1)),
    PRIMARY KEY (trace_id, span_id),
    FOREIGN KEY (trace_id) REFERENCES traces (trace_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS spans_trace_order_idx
    ON spans (trace_id, start_ms, end_ms, span_id);

CREATE TABLE IF NOT EXISTS decisions (
    trace_id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL UNIQUE,
    close_sequence INTEGER NOT NULL UNIQUE,
    decided_at_ms INTEGER NOT NULL,
    logical_second INTEGER NOT NULL,
    close_reason TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('KEEP', 'DROP')),
    reason_code TEXT NOT NULL,
    reason_text TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    policy_revision INTEGER NOT NULL,
    sampling_rate REAL NOT NULL,
    quota_consumed INTEGER NOT NULL CHECK (quota_consumed IN (0, 1)),
    evidence_json TEXT NOT NULL,
    rate_estimate_json TEXT NOT NULL,
    chain_digest TEXT NOT NULL,
    decision_fingerprint TEXT NOT NULL,
    late_span_policy TEXT NOT NULL,
    FOREIGN KEY (trace_id) REFERENCES traces (trace_id),
    FOREIGN KEY (policy_version) REFERENCES policies (version)
);

CREATE INDEX IF NOT EXISTS decisions_second_idx
    ON decisions (logical_second, outcome);

CREATE TABLE IF NOT EXISTS quota_ledger (
    logical_second INTEGER PRIMARY KEY,
    capacity INTEGER NOT NULL CHECK (capacity >= 0),
    used INTEGER NOT NULL CHECK (used >= 0),
    updated_at_ms INTEGER NOT NULL,
    CHECK (used <= capacity)
);

CREATE TABLE IF NOT EXISTS trace_chains (
    trace_id TEXT PRIMARY KEY,
    span_count INTEGER NOT NULL,
    start_ms INTEGER,
    end_ms INTEGER,
    duration_ms INTEGER,
    digest TEXT NOT NULL,
    late_span_count INTEGER NOT NULL DEFAULT 0,
    last_late_arrival_ms INTEGER,
    recomputed_at_ms INTEGER NOT NULL,
    FOREIGN KEY (trace_id) REFERENCES traces (trace_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS late_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id TEXT NOT NULL,
    span_id TEXT NOT NULL,
    arrival_ms INTEGER NOT NULL,
    span_digest TEXT NOT NULL,
    canonical_json TEXT NOT NULL,
    UNIQUE (trace_id, span_id)
);

CREATE INDEX IF NOT EXISTS late_events_trace_idx
    ON late_events (trace_id, arrival_ms, span_id);

CREATE TABLE IF NOT EXISTS trace_lifecycle (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id TEXT NOT NULL,
    at_ms INTEGER NOT NULL,
    event TEXT NOT NULL,
    trigger TEXT NOT NULL,
    detail_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS trace_lifecycle_trace_idx
    ON trace_lifecycle (trace_id, sequence);

CREATE TABLE IF NOT EXISTS quarantined_traces (
    trace_id TEXT PRIMARY KEY,
    code TEXT NOT NULL,
    message TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    at_ms INTEGER NOT NULL,
    FOREIGN KEY (trace_id) REFERENCES traces (trace_id)
);
"""


DEFAULT_META = {
    "schema_version": SCHEMA_VERSION,
    "logical_clock_ms": "0",
    "window_ms": "900",
    "quota_per_second": "2",
    "clock_tolerance_ms": "10",
    "waiting_high_water": "0",
    "recovery_runs": "0",
    "recovered_events": "0",
    "recovered_decisions": "0",
    "skipped_committed": "0",
    "late_span_total": "0",
}


def connect(path: str | Path) -> sqlite3.Connection:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(target, timeout=30.0, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 30000")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = FULL")
    return connection


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA_SQL)
    connection.execute("BEGIN IMMEDIATE")
    try:
        for key, value in DEFAULT_META.items():
            connection.execute(
                "INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)",
                (key, value),
            )
        row = connection.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        if row is None or row["value"] != SCHEMA_VERSION:
            from .errors import BusinessError

            raise BusinessError(
                "E_BAD_INPUT",
                "unsupported database schema version",
                details={"found": None if row is None else row["value"]},
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise


@contextmanager
def transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield connection
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


@contextmanager
def read_transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    connection.execute("BEGIN")
    try:
        yield connection
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def get_meta(connection: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = connection.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    if row is None:
        return default
    return str(row["value"])


def get_meta_int(connection: sqlite3.Connection, key: str, default: int = 0) -> int:
    value = get_meta(connection, key)
    if value is None:
        return default
    return int(value)


def set_meta(connection: sqlite3.Connection, key: str, value: str | int) -> None:
    connection.execute(
        """
        INSERT INTO meta (key, value) VALUES (?, ?)
        ON CONFLICT (key) DO UPDATE SET value = excluded.value
        """,
        (key, str(value)),
    )


def increment_meta(connection: sqlite3.Connection, key: str, amount: int = 1) -> int:
    current = get_meta_int(connection, key)
    updated = current + amount
    set_meta(connection, key, updated)
    return updated

