from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .errors import E_DATABASE_NOT_FOUND, PoolError


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pool_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    state_version INTEGER NOT NULL,
    committed_app_watermark INTEGER NOT NULL,
    logical_clock_ms INTEGER NOT NULL,
    total_allocated INTEGER NOT NULL,
    total_active INTEGER NOT NULL,
    total_idle INTEGER NOT NULL,
    queue_depth INTEGER NOT NULL,
    idle_digest TEXT NOT NULL,
    sample_digest TEXT NOT NULL,
    config_version INTEGER NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('exact', 'tiered')),
    mode_changed_at_ms INTEGER NOT NULL,
    next_connection_seq INTEGER NOT NULL,
    next_lease_seq INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS config_snapshots (
    version INTEGER PRIMARY KEY,
    digest TEXT NOT NULL,
    config_json TEXT NOT NULL,
    applied_at_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS endpoint_snapshots (
    name TEXT PRIMARY KEY,
    address TEXT NOT NULL,
    host TEXT NOT NULL,
    port INTEGER NOT NULL,
    max_connections INTEGER NOT NULL,
    build_latency_ms INTEGER NOT NULL,
    healthy INTEGER NOT NULL,
    pool_labels_json TEXT NOT NULL,
    endpoint_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trace_applications (
    app_seq INTEGER NOT NULL,
    app_version INTEGER NOT NULL,
    tenant TEXT NOT NULL,
    pool_label TEXT NOT NULL,
    apply_clock_ms INTEGER NOT NULL,
    hold_ms INTEGER NOT NULL,
    release_flag INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    PRIMARY KEY (app_seq, app_version)
);

CREATE TABLE IF NOT EXISTS requests (
    app_seq INTEGER NOT NULL,
    app_version INTEGER NOT NULL,
    tenant TEXT NOT NULL,
    pool_label TEXT NOT NULL,
    apply_clock_ms INTEGER NOT NULL,
    hold_ms INTEGER NOT NULL,
    release_flag INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    lease_id TEXT,
    queue_reason TEXT,
    submitted_at_ms INTEGER NOT NULL,
    PRIMARY KEY (app_seq, app_version)
);

CREATE TABLE IF NOT EXISTS quota_accounts (
    tenant TEXT PRIMARY KEY,
    pool_labels_json TEXT NOT NULL,
    allocated INTEGER NOT NULL,
    active INTEGER NOT NULL,
    idle INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS connections (
    conn_id TEXT PRIMARY KEY,
    endpoint_name TEXT NOT NULL,
    pool_label TEXT NOT NULL,
    tenant TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('IDLE', 'IN_USE', 'CLOSED')),
    created_at_ms INTEGER NOT NULL,
    idle_since_ms INTEGER,
    closed_at_ms INTEGER,
    close_reason TEXT,
    current_lease_id TEXT,
    build_latency_ms INTEGER NOT NULL,
    owner_instance TEXT,
    generation INTEGER NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS connections_one_lease_uq
ON connections(current_lease_id)
WHERE current_lease_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS connections_state_idx
ON connections(state, pool_label, tenant);

CREATE TABLE IF NOT EXISTS leases (
    lease_id TEXT PRIMARY KEY,
    app_seq INTEGER NOT NULL,
    app_version INTEGER NOT NULL,
    tenant TEXT NOT NULL,
    pool_label TEXT NOT NULL,
    endpoint_name TEXT NOT NULL,
    conn_id TEXT NOT NULL,
    apply_clock_ms INTEGER NOT NULL,
    issued_at_ms INTEGER NOT NULL,
    hold_ms INTEGER NOT NULL,
    build_latency_ms INTEGER NOT NULL,
    reused INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'RELEASED')),
    released_at_ms INTEGER,
    release_reason TEXT,
    UNIQUE (app_seq, app_version)
);

CREATE INDEX IF NOT EXISTS leases_status_idx ON leases(status, issued_at_ms);

CREATE TABLE IF NOT EXISTS queue_entries (
    app_seq INTEGER NOT NULL,
    app_version INTEGER NOT NULL,
    position INTEGER NOT NULL,
    tenant TEXT NOT NULL,
    pool_label TEXT NOT NULL,
    apply_clock_ms INTEGER NOT NULL,
    hold_ms INTEGER NOT NULL,
    enqueued_at_ms INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('PENDING', 'SATISFIED', 'CANCELLED')),
    lease_id TEXT,
    last_error_code TEXT,
    last_error_at_ms INTEGER,
    PRIMARY KEY (app_seq, app_version),
    UNIQUE (position)
);

CREATE INDEX IF NOT EXISTS queue_pending_idx
ON queue_entries(status, position);

CREATE TABLE IF NOT EXISTS release_receipts (
    app_seq INTEGER NOT NULL,
    app_version INTEGER NOT NULL,
    release_clock_ms INTEGER NOT NULL,
    reason TEXT NOT NULL,
    closed INTEGER NOT NULL,
    lease_id TEXT NOT NULL,
    conn_id TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL,
    PRIMARY KEY (app_seq, app_version)
);

CREATE TABLE IF NOT EXISTS events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    logical_clock_ms INTEGER NOT NULL,
    kind TEXT NOT NULL,
    app_seq INTEGER,
    app_version INTEGER,
    payload_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS events_kind_idx ON events(kind, event_id);

CREATE TABLE IF NOT EXISTS mode_switches (
    switch_id INTEGER PRIMARY KEY AUTOINCREMENT,
    at_clock_ms INTEGER NOT NULL,
    from_mode TEXT NOT NULL,
    to_mode TEXT NOT NULL,
    trigger_json TEXT NOT NULL,
    cost_json TEXT NOT NULL,
    rejected_mode TEXT NOT NULL,
    reason TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS operation_intents (
    intent_id TEXT PRIMARY KEY,
    app_seq INTEGER,
    app_version INTEGER,
    endpoint_name TEXT NOT NULL,
    address TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('OPENING', 'COMMITTED', 'ABORTED')),
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL,
    detail_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS operation_intents_state_idx
ON operation_intents(state, created_at_ms);

CREATE TABLE IF NOT EXISTS recovery_runs (
    recovery_id INTEGER PRIMARY KEY AUTOINCREMENT,
    at_clock_ms INTEGER NOT NULL,
    repaired_rows INTEGER NOT NULL,
    aborted_intents INTEGER NOT NULL,
    physical_rebuilt INTEGER NOT NULL,
    detail_json TEXT NOT NULL
);
"""


class FileLock:
    """Exclusive cross-process lock used around all write transactions."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.file: Any | None = None

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = open(self.path, "a+b")
        if os.name == "nt":
            import msvcrt

            self.file.seek(0, os.SEEK_END)
            if self.file.tell() == 0:
                self.file.write(b"\0")
                self.file.flush()
            deadline = time.monotonic() + 60.0
            while True:
                try:
                    self.file.seek(0)
                    msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        self.file.close()
                        self.file = None
                        raise
                    time.sleep(0.05)
        else:
            import fcntl

            fcntl.flock(self.file.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self.file is None:
            return
        if os.name == "nt":
            import msvcrt

            self.file.seek(0)
            msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
        self.file.close()
        self.file = None


class Database:
    def __init__(self, path: str | Path, *, create: bool = False) -> None:
        self.path = Path(path)
        if not self.path.exists() and not create:
            raise PoolError(
                E_DATABASE_NOT_FOUND,
                "pool database does not exist; run load first",
                operation="open",
                path=str(self.path),
            )
        if create:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(
            str(self.path),
            timeout=30.0,
            isolation_level=None,
        )
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 30000")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA synchronous = FULL")
        if create:
            self.connection.executescript(SCHEMA_SQL)

    @property
    def lock_path(self) -> Path:
        return Path(str(self.path) + ".lock")

    def close(self) -> None:
        self.connection.close()

    def execute(self, sql: str, parameters: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        return self.connection.execute(sql, parameters)

    def executemany(
        self, sql: str, parameters: list[tuple[Any, ...]]
    ) -> sqlite3.Cursor:
        return self.connection.executemany(sql, parameters)

    def fetchone(self, sql: str, parameters: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        return self.connection.execute(sql, parameters).fetchone()

    def fetchall(self, sql: str, parameters: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        return list(self.connection.execute(sql, parameters).fetchall())

    @contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[None]:
        begin = "BEGIN IMMEDIATE" if immediate else "BEGIN"
        self.connection.execute(begin)
        try:
            yield
        except BaseException:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def table_names(self) -> set[str]:
        rows = self.fetchall(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        )
        return {str(row["name"]) for row in rows}
