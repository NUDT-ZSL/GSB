from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = "1"

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS volumes (
    volume_id TEXT PRIMARY KEY,
    block_size INTEGER NOT NULL CHECK(block_size > 0),
    block_count INTEGER NOT NULL CHECK(block_count > 0),
    created_clock INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS current_view (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    volume_id TEXT NOT NULL REFERENCES volumes(volume_id),
    view_version INTEGER NOT NULL,
    logical_clock INTEGER NOT NULL,
    anchor_snapshot_id TEXT NOT NULL,
    last_rollback_clock INTEGER,
    last_rollback_request_id TEXT
);

CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id TEXT PRIMARY KEY,
    volume_id TEXT NOT NULL REFERENCES volumes(volume_id),
    seq INTEGER NOT NULL CHECK(seq >= 0),
    parent_snapshot_id TEXT REFERENCES snapshots(snapshot_id),
    created_clock INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('live', 'orphan')),
    is_base INTEGER NOT NULL DEFAULT 0 CHECK(is_base IN (0, 1)),
    covered_start INTEGER,
    covered_end INTEGER,
    manifest_json TEXT NOT NULL,
    manifest_digest TEXT NOT NULL,
    view_version INTEGER NOT NULL,
    orphaned_by TEXT,
    orphan_reason TEXT,
    orphan_path TEXT,
    orphaned_view_version INTEGER,
    UNIQUE(volume_id, seq),
    CHECK(is_base = 0 OR parent_snapshot_id IS NULL)
);

CREATE TABLE IF NOT EXISTS snapshot_blocks (
    snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id),
    block_no INTEGER NOT NULL,
    digest TEXT NOT NULL,
    source TEXT NOT NULL CHECK(source IN ('base', 'cow', 'inherit')),
    PRIMARY KEY(snapshot_id, block_no)
);

CREATE TABLE IF NOT EXISTS volume_blocks (
    block_no INTEGER PRIMARY KEY,
    volume_id TEXT NOT NULL REFERENCES volumes(volume_id),
    digest TEXT NOT NULL,
    owner_snapshot_id TEXT REFERENCES snapshots(snapshot_id)
);

CREATE TABLE IF NOT EXISTS blocks (
    digest TEXT PRIMARY KEY,
    size INTEGER NOT NULL CHECK(size >= 0)
);

CREATE TABLE IF NOT EXISTS block_refs (
    source_kind TEXT NOT NULL CHECK(source_kind IN ('volume', 'snapshot', 'transaction')),
    source_id TEXT NOT NULL,
    block_no INTEGER NOT NULL,
    digest TEXT NOT NULL,
    PRIMARY KEY(source_kind, source_id, block_no)
);

CREATE TABLE IF NOT EXISTS transaction_refs (
    source_id TEXT NOT NULL,
    block_no INTEGER NOT NULL,
    digest TEXT NOT NULL,
    PRIMARY KEY(source_id, block_no)
);

CREATE TABLE IF NOT EXISTS block_counts (
    digest TEXT PRIMARY KEY,
    ref_count INTEGER NOT NULL CHECK(ref_count > 0)
);

CREATE TABLE IF NOT EXISTS snapshot_runs (
    snapshot_id TEXT PRIMARY KEY,
    volume_id TEXT NOT NULL REFERENCES volumes(volume_id),
    seq INTEGER NOT NULL,
    parent_snapshot_id TEXT NOT NULL,
    created_clock INTEGER NOT NULL,
    manifest_digest TEXT NOT NULL,
    spec_json TEXT NOT NULL,
    phase TEXT NOT NULL CHECK(phase IN ('prepared', 'applied')),
    result_view_version INTEGER
);

CREATE TABLE IF NOT EXISTS rollback_requests (
    request_id TEXT PRIMARY KEY,
    target_snapshot_id TEXT NOT NULL,
    rollback_clock INTEGER NOT NULL,
    keep_after_target INTEGER NOT NULL CHECK(keep_after_target IN (0, 1)),
    payload_json TEXT NOT NULL,
    phase TEXT NOT NULL CHECK(phase IN ('prepared', 'applied')),
    prepared_view_version INTEGER NOT NULL,
    result_view_version INTEGER,
    outcome_json TEXT
);

CREATE TABLE IF NOT EXISTS gc_runs (
    run_id TEXT PRIMARY KEY,
    volume_id TEXT NOT NULL REFERENCES volumes(volume_id),
    view_version INTEGER NOT NULL,
    phase TEXT NOT NULL CHECK(phase IN ('prepared', 'deleting', 'applied')),
    report_json TEXT,
    UNIQUE(volume_id, view_version)
);

CREATE TABLE IF NOT EXISTS gc_actions (
    run_id TEXT NOT NULL REFERENCES gc_runs(run_id),
    digest TEXT NOT NULL,
    action TEXT NOT NULL CHECK(action IN ('retain', 'delete')),
    reason TEXT NOT NULL,
    references_json TEXT NOT NULL,
    ref_count INTEGER NOT NULL,
    PRIMARY KEY(run_id, digest)
);

CREATE INDEX IF NOT EXISTS idx_snapshots_parent ON snapshots(parent_snapshot_id);
CREATE INDEX IF NOT EXISTS idx_snapshot_blocks_digest ON snapshot_blocks(digest);
CREATE INDEX IF NOT EXISTS idx_block_refs_digest ON block_refs(digest);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        str(db_path),
        timeout=30.0,
        isolation_level=None,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    connection.execute("PRAGMA synchronous = FULL")
    return connection


def initialize_schema(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA journal_mode = WAL")
    connection.executescript(SCHEMA_SQL)
    connection.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
        (SCHEMA_VERSION,),
    )


def get_meta(connection: sqlite3.Connection, key: str) -> str | None:
    row = connection.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return None if row is None else str(row["value"])


def set_meta(connection: sqlite3.Connection, key: str, value: str) -> None:
    connection.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def table_exists(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None
