from __future__ import annotations

import fcntl
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .errors import ErrorCode, GatewayError

SCHEMA_VERSION = 1

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS topics (
    name TEXT PRIMARY KEY,
    partitions INTEGER NOT NULL CHECK (partitions > 0),
    partition_key_rule TEXT NOT NULL,
    backlog_limit INTEGER NOT NULL CHECK (backlog_limit > 0),
    global_seed INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS consumers (
    consumer_id TEXT PRIMARY KEY,
    topic TEXT NOT NULL REFERENCES topics(name),
    request_size INTEGER NOT NULL CHECK (request_size > 0),
    rate_limit_scaled INTEGER NOT NULL CHECK (rate_limit_scaled > 0),
    rate_limit_text TEXT NOT NULL,
    delivery_timeout_ms INTEGER NOT NULL CHECK (delivery_timeout_ms > 0)
);

CREATE TABLE IF NOT EXISTS quota_snapshot (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    config_version INTEGER NOT NULL,
    global_capacity INTEGER NOT NULL,
    global_refill_scaled INTEGER NOT NULL,
    per_consumer_capacity INTEGER NOT NULL,
    round_budget_ms INTEGER NOT NULL,
    delivery_timeout_ms INTEGER NOT NULL,
    max_delivery_generation INTEGER NOT NULL,
    retry_backoff_base_ms INTEGER NOT NULL,
    retry_backoff_max_ms INTEGER NOT NULL,
    attempt_duration_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS clock_config (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    initial_now_ms INTEGER NOT NULL,
    regression_tolerance_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS bucket_baselines (
    bucket_id TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    owner TEXT NOT NULL,
    capacity_scaled INTEGER NOT NULL,
    initial_balance_scaled INTEGER NOT NULL,
    initial_last_refill_ms INTEGER NOT NULL,
    refill_rate_scaled INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS buckets (
    bucket_id TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    owner TEXT NOT NULL,
    capacity_scaled INTEGER NOT NULL,
    balance_scaled INTEGER NOT NULL,
    last_refill_ms INTEGER NOT NULL,
    refill_rate_scaled INTEGER NOT NULL,
    CHECK (balance_scaled >= 0),
    CHECK (balance_scaled <= capacity_scaled)
);

CREATE TABLE IF NOT EXISTS messages (
    message_id TEXT PRIMARY KEY,
    topic TEXT NOT NULL REFERENCES topics(name),
    partition INTEGER NOT NULL,
    partition_key TEXT NOT NULL,
    seq_in_partition INTEGER NOT NULL,
    payload TEXT NOT NULL,
    generation INTEGER NOT NULL DEFAULT 0,
    available_at_ms INTEGER NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('ready', 'inflight', 'delivered', 'confirmed', 'dead_letter')),
    current_delivery_id TEXT,
    confirmed_by TEXT,
    confirmed_at_ms INTEGER,
    dead_letter_reason TEXT,
    dead_letter_at_ms INTEGER,
    UNIQUE (topic, partition, seq_in_partition)
);
CREATE INDEX IF NOT EXISTS idx_messages_queue ON messages(topic, state, available_at_ms, partition, seq_in_partition);

CREATE TABLE IF NOT EXISTS fault_events (
    fault_id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT NOT NULL,
    consumer_id TEXT NOT NULL,
    generation INTEGER,
    action TEXT NOT NULL,
    duration_ms INTEGER,
    note TEXT NOT NULL DEFAULT '',
    consumed_delivery_id TEXT,
    consumed_at_ms INTEGER,
    UNIQUE (message_id, consumer_id, generation)
);

CREATE TABLE IF NOT EXISTS delivery_requests (
    request_id TEXT PRIMARY KEY,
    command TEXT NOT NULL,
    consumer_id TEXT NOT NULL,
    topic TEXT NOT NULL,
    now_ms INTEGER NOT NULL,
    start_cursor_ms INTEGER NOT NULL,
    deadline_ms INTEGER NOT NULL,
    limit_count INTEGER NOT NULL,
    message_id TEXT,
    payload_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running', 'completed')),
    cursor_ms INTEGER NOT NULL,
    outcome_count INTEGER NOT NULL DEFAULT 0,
    finish_reason TEXT,
    result_json TEXT,
    created_ms INTEGER NOT NULL,
    updated_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS deliveries (
    delivery_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL REFERENCES delivery_requests(request_id),
    message_id TEXT NOT NULL REFERENCES messages(message_id),
    consumer_id TEXT NOT NULL REFERENCES consumers(consumer_id),
    generation INTEGER NOT NULL CHECK (generation > 0),
    started_at_ms INTEGER NOT NULL,
    duration_ms INTEGER NOT NULL,
    planned_outcome TEXT NOT NULL,
    planned_reason TEXT,
    status TEXT NOT NULL CHECK (status IN ('started', 'delivered', 'failed', 'rejected', 'expired')),
    completed_at_ms INTEGER,
    confirmed INTEGER NOT NULL DEFAULT 0,
    confirmed_at_ms INTEGER,
    confirmed_by TEXT,
    retry_at_ms INTEGER,
    fault_action TEXT,
    UNIQUE (message_id, generation)
);
CREATE INDEX IF NOT EXISTS idx_deliveries_message ON deliveries(message_id, generation);
CREATE INDEX IF NOT EXISTS idx_deliveries_request ON deliveries(request_id, started_at_ms);
CREATE INDEX IF NOT EXISTS idx_deliveries_status ON deliveries(status);

CREATE TABLE IF NOT EXISTS ledger_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    delivery_id TEXT NOT NULL UNIQUE REFERENCES deliveries(delivery_id),
    request_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    consumer_id TEXT NOT NULL,
    at_ms INTEGER NOT NULL,
    charge_scaled INTEGER NOT NULL,
    global_before_scaled INTEGER NOT NULL,
    global_refill_scaled INTEGER NOT NULL,
    global_after_scaled INTEGER NOT NULL,
    global_last_before_ms INTEGER NOT NULL,
    global_last_after_ms INTEGER NOT NULL,
    consumer_before_scaled INTEGER NOT NULL,
    consumer_refill_scaled INTEGER NOT NULL,
    consumer_after_scaled INTEGER NOT NULL,
    consumer_last_before_ms INTEGER NOT NULL,
    consumer_last_after_ms INTEGER NOT NULL,
    global_rate_scaled INTEGER NOT NULL,
    consumer_rate_scaled INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS token_waits (
    wait_id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL,
    consumer_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    at_ms INTEGER NOT NULL,
    global_balance_scaled INTEGER NOT NULL,
    global_last_refill_ms INTEGER NOT NULL,
    consumer_balance_scaled INTEGER NOT NULL,
    consumer_last_refill_ms INTEGER NOT NULL,
    global_rate_scaled INTEGER NOT NULL,
    consumer_rate_scaled INTEGER NOT NULL,
    needed_wait_ms INTEGER NOT NULL,
    reason TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS retry_events (
    retry_id INTEGER PRIMARY KEY AUTOINCREMENT,
    delivery_id TEXT NOT NULL UNIQUE,
    message_id TEXT NOT NULL,
    consumer_id TEXT NOT NULL,
    generation INTEGER NOT NULL,
    at_ms INTEGER NOT NULL,
    capped_base_ms INTEGER NOT NULL,
    jitter_ms INTEGER NOT NULL,
    delay_ms INTEGER NOT NULL,
    next_at_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS dead_letters (
    message_id TEXT PRIMARY KEY,
    reason TEXT NOT NULL CHECK (reason IN ('failed', 'rejected', 'expired')),
    generation INTEGER NOT NULL,
    delivery_id TEXT UNIQUE,
    at_ms INTEGER NOT NULL,
    tombstone TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS request_outcomes (
    request_id TEXT NOT NULL REFERENCES delivery_requests(request_id),
    sequence INTEGER NOT NULL,
    message_id TEXT,
    delivery_id TEXT,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT NOT NULL,
    at_ms INTEGER NOT NULL,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    generation INTEGER,
    detail_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (request_id, sequence)
);

CREATE TABLE IF NOT EXISTS consumer_usage (
    consumer_id TEXT PRIMARY KEY REFERENCES consumers(consumer_id),
    attempts INTEGER NOT NULL DEFAULT 0,
    delivered INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    rejected INTEGER NOT NULL DEFAULT 0,
    expired INTEGER NOT NULL DEFAULT 0,
    abandoned INTEGER NOT NULL DEFAULT 0,
    confirmed INTEGER NOT NULL DEFAULT 0,
    tokens_spent_scaled INTEGER NOT NULL DEFAULT 0,
    logical_time_used_ms INTEGER NOT NULL DEFAULT 0,
    rounds INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS abandonments (
    request_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    generation INTEGER NOT NULL,
    reason TEXT NOT NULL,
    at_ms INTEGER NOT NULL,
    PRIMARY KEY (request_id, message_id, generation)
);
"""


def db_uri(path: str | Path, *, readonly: bool) -> str:
    p = Path(path).resolve()
    if readonly:
        return f"file:{p.as_posix()}?mode=ro"
    return f"file:{p.as_posix()}?mode=rwc"


def require_existing(path: str | Path) -> Path:
    p = Path(path)
    if not p.exists() or not p.is_file():
        raise GatewayError(
            ErrorCode.DATABASE,
            "database file does not exist",
            {"path": str(p)},
        )
    return p


def connect(path: str | Path, *, readonly: bool = False) -> sqlite3.Connection:
    p = Path(path)
    if readonly:
        require_existing(p)
    else:
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise GatewayError(
                ErrorCode.DATABASE,
                "cannot create database directory",
                {"path": str(p.parent), "error": str(exc)},
            ) from exc
    try:
        uri = db_uri(p, readonly=readonly)
        conn = sqlite3.connect(uri, uri=True, timeout=30.0, isolation_level=None)
    except sqlite3.Error as exc:
        raise GatewayError(
            ErrorCode.DATABASE,
            "cannot open database",
            {"path": str(p), "error": str(exc)},
        ) from exc
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        if readonly:
            conn.execute("PRAGMA query_only = ON")
        else:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = FULL")
        return conn
    except sqlite3.Error as exc:
        conn.close()
        raise GatewayError(
            ErrorCode.DATABASE,
            "cannot configure database",
            {"path": str(p), "error": str(exc)},
        ) from exc


@contextmanager
def write_lock(path: str | Path) -> Iterator[None]:
    lock_path = Path(str(path) + ".lock")
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as exc:
        raise GatewayError(
            ErrorCode.DATABASE,
            "cannot open database lock file",
            {"path": str(lock_path), "error": str(exc)},
        ) from exc
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise


@contextmanager
def read_snapshot(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        conn.execute("BEGIN")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise


def initialize_schema(conn: sqlite3.Connection) -> None:
    try:
        conn.executescript(SCHEMA_SQL)
        current = get_meta(conn, "schema_version")
        if current is None:
            set_meta(conn, "schema_version", str(SCHEMA_VERSION))
        elif int(current) != SCHEMA_VERSION:
            raise GatewayError(
                ErrorCode.SCHEMA,
                "unsupported database schema version",
                {"expected": SCHEMA_VERSION, "actual": int(current)},
            )
    except sqlite3.Error as exc:
        raise GatewayError(ErrorCode.SCHEMA, "cannot initialize schema", {"error": str(exc)}) from exc


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return None if row is None else str(row["value"])


def set_meta(conn: sqlite3.Connection, key: str, value: str | int) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


def bump_queue_version(conn: sqlite3.Connection) -> int:
    current = int(get_meta(conn, "queue_version") or "0") + 1
    set_meta(conn, "queue_version", current)
    return current


def clock_watermark(conn: sqlite3.Connection) -> int:
    return int(get_meta(conn, "clock_watermark_ms") or "0")


def advance_clock(conn: sqlite3.Connection, value_ms: int) -> int:
    current = clock_watermark(conn)
    new_value = max(current, value_ms)
    if new_value != current:
        set_meta(conn, "clock_watermark_ms", new_value)
    return new_value

