"""SQLite schema and connection helpers."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA_VERSION = 1


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id TEXT PRIMARY KEY,
    sequence INTEGER NOT NULL UNIQUE,
    parent_id TEXT,
    created_clock_ms INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('alive', 'orphan')),
    is_base INTEGER NOT NULL DEFAULT 0 CHECK (is_base IN (0, 1)),
    content_hash TEXT NOT NULL,
    visible_hash TEXT NOT NULL,
    orphan_reason TEXT,
    orphaned_by TEXT,
    created_operation TEXT
);

CREATE TABLE IF NOT EXISTS snapshot_ranges (
    snapshot_id TEXT NOT NULL,
    range_no INTEGER NOT NULL,
    start_block INTEGER NOT NULL,
    end_block INTEGER NOT NULL,
    PRIMARY KEY (snapshot_id, range_no)
);

CREATE TABLE IF NOT EXISTS snapshot_writes (
    snapshot_id TEXT NOT NULL,
    block_index INTEGER NOT NULL,
    digest TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, block_index)
);

CREATE TABLE IF NOT EXISTS snapshot_blocks (
    snapshot_id TEXT NOT NULL,
    block_index INTEGER NOT NULL,
    digest TEXT NOT NULL,
    owner_snapshot_id TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, block_index)
);

CREATE TABLE IF NOT EXISTS volume_blocks (
    block_index INTEGER PRIMARY KEY,
    digest TEXT NOT NULL,
    owner_snapshot_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS block_refs (
    source_kind TEXT NOT NULL CHECK (
        source_kind IN ('volume', 'snapshot', 'transaction')
    ),
    source_id TEXT NOT NULL,
    block_index INTEGER NOT NULL,
    digest TEXT NOT NULL,
    PRIMARY KEY (source_kind, source_id, block_index)
);

CREATE INDEX IF NOT EXISTS idx_block_refs_digest
    ON block_refs(digest);
CREATE INDEX IF NOT EXISTS idx_snapshot_blocks_digest
    ON snapshot_blocks(digest);

CREATE TABLE IF NOT EXISTS planned_snapshots (
    sequence INTEGER PRIMARY KEY,
    snapshot_id TEXT NOT NULL UNIQUE,
    parent_id TEXT NOT NULL,
    created_clock_ms INTEGER NOT NULL,
    content_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS planned_ranges (
    sequence INTEGER NOT NULL,
    range_no INTEGER NOT NULL,
    start_block INTEGER NOT NULL,
    end_block INTEGER NOT NULL,
    PRIMARY KEY (sequence, range_no)
);

CREATE TABLE IF NOT EXISTS planned_writes (
    sequence INTEGER NOT NULL,
    block_index INTEGER NOT NULL,
    digest TEXT NOT NULL,
    data BLOB NOT NULL,
    PRIMARY KEY (sequence, block_index)
);

CREATE TABLE IF NOT EXISTS planned_rollbacks (
    request_id TEXT PRIMARY KEY,
    target_snapshot_id TEXT NOT NULL,
    clock_ms INTEGER NOT NULL,
    retain_after INTEGER NOT NULL CHECK (retain_after IN (0, 1))
);

CREATE TABLE IF NOT EXISTS rollback_requests (
    request_id TEXT PRIMARY KEY,
    target_snapshot_id TEXT NOT NULL,
    clock_ms INTEGER NOT NULL,
    retain_after INTEGER NOT NULL CHECK (retain_after IN (0, 1)),
    view_version INTEGER NOT NULL,
    result_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fork_decisions (
    rollback_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('orphaned', 'preserved')),
    reason TEXT NOT NULL,
    previous_status TEXT NOT NULL,
    PRIMARY KEY (rollback_id, snapshot_id)
);

CREATE TABLE IF NOT EXISTS operations (
    operation_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('snapshot', 'rollback')),
    status TEXT NOT NULL CHECK (status IN ('pending', 'committed', 'aborted')),
    phase TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    snapshot_id TEXT,
    request_id TEXT,
    clock_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS operation_refs (
    operation_id TEXT NOT NULL,
    block_index INTEGER NOT NULL,
    digest TEXT NOT NULL,
    PRIMARY KEY (operation_id, block_index)
);

CREATE TABLE IF NOT EXISTS gc_runs (
    run_id TEXT PRIMARY KEY,
    view_version INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running', 'completed')),
    reclaimed_count INTEGER NOT NULL DEFAULT 0,
    retained_count INTEGER NOT NULL DEFAULT 0,
    scanned_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS gc_candidates (
    run_id TEXT NOT NULL,
    digest TEXT NOT NULL,
    PRIMARY KEY (run_id, digest)
);

CREATE TABLE IF NOT EXISTS gc_items (
    run_id TEXT NOT NULL,
    digest TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('deleted', 'absent', 'retained')),
    reason TEXT NOT NULL,
    ref_count INTEGER NOT NULL,
    PRIMARY KEY (run_id, digest)
);
"""


def connect(db_path: str | Path, *, wal: bool = False) -> sqlite3.Connection:
    conn = sqlite3.connect(
        str(db_path),
        timeout=30.0,
        isolation_level=None,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    if wal:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = FULL")
    return conn


def initialize_db(
    db_path: str | Path,
    *,
    wal: bool = False,
) -> sqlite3.Connection:
    conn = connect(db_path, wal=wal)
    conn.executescript(SCHEMA_SQL)
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    return conn


@contextmanager
def transaction(
    conn: sqlite3.Connection,
    *,
    immediate: bool = False,
) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
