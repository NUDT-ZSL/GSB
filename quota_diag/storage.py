from __future__ import annotations

import contextlib
import sqlite3
from pathlib import Path
from typing import Iterator

from .errors import fail

SCHEMA_VERSION = "1"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS dirs (
    dir_id TEXT PRIMARY KEY,
    parent_id TEXT,
    base_quota INTEGER,
    ext_quotas TEXT NOT NULL DEFAULT '{}',
    name TEXT,
    tombstoned INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS write_events (
    seq INTEGER PRIMARY KEY,
    dir_id TEXT NOT NULL,
    file_key TEXT NOT NULL,
    bytes INTEGER NOT NULL,
    digest TEXT NOT NULL,
    clock_ms INTEGER NOT NULL,
    content_b64 TEXT
);

CREATE TABLE IF NOT EXISTS rules_raw (
    source TEXT NOT NULL,
    rule_key TEXT NOT NULL,
    quota_bytes INTEGER NOT NULL,
    effective_at_ms INTEGER NOT NULL,
    generation INTEGER NOT NULL,
    conversion TEXT,
    source_version TEXT,
    PRIMARY KEY (source, rule_key)
);

CREATE TABLE IF NOT EXISTS rules_v1 (
    manifest_version TEXT NOT NULL,
    rule_key TEXT NOT NULL,
    quota_bytes INTEGER NOT NULL,
    effective_at_ms INTEGER NOT NULL,
    generation INTEGER NOT NULL,
    conversion TEXT,
    PRIMARY KEY (manifest_version, rule_key)
);

CREATE TABLE IF NOT EXISTS rules_v2_versions (
    version TEXT NOT NULL,
    rule_key TEXT NOT NULL,
    quota_bytes INTEGER NOT NULL,
    effective_at_ms INTEGER NOT NULL,
    generation INTEGER NOT NULL,
    PRIMARY KEY (version, rule_key)
);

CREATE TABLE IF NOT EXISTS rules_patch (
    patch_id TEXT NOT NULL,
    rule_key TEXT NOT NULL,
    quota_bytes INTEGER NOT NULL,
    effective_at_ms INTEGER NOT NULL,
    generation INTEGER NOT NULL,
    PRIMARY KEY (patch_id, rule_key)
);

CREATE TABLE IF NOT EXISTS rule_tombstones (
    rule_key TEXT PRIMARY KEY,
    patch_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rules_unified (
    rule_key TEXT PRIMARY KEY,
    quota_bytes INTEGER NOT NULL,
    effective_at_ms INTEGER NOT NULL,
    generation INTEGER NOT NULL,
    source TEXT NOT NULL,
    decision TEXT NOT NULL,
    conversion TEXT,
    source_version TEXT
);

CREATE TABLE IF NOT EXISTS snapshots (
    version TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL,
    applied INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS patches (
    patch_id TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    applied INTEGER NOT NULL DEFAULT 0,
    apply_order INTEGER
);

CREATE TABLE IF NOT EXISTS processed_events (
    seq INTEGER PRIMARY KEY,
    rules_version TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ledger (
    dir_id TEXT NOT NULL,
    file_key TEXT NOT NULL,
    consumed INTEGER NOT NULL,
    remaining INTEGER NOT NULL,
    rules_version TEXT NOT NULL,
    quota INTEGER,
    updated_seq INTEGER,
    PRIMARY KEY (dir_id, file_key)
);

CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    seq INTEGER NOT NULL,
    dir_id TEXT NOT NULL,
    file_key TEXT NOT NULL,
    bytes INTEGER NOT NULL,
    clock_ms INTEGER NOT NULL,
    rules_version TEXT NOT NULL,
    status TEXT NOT NULL,
    fail_dir TEXT,
    reason TEXT,
    deducted_nodes INTEGER NOT NULL,
    digest TEXT,
    deducted_bytes INTEGER NOT NULL DEFAULT 0,
    attempts TEXT NOT NULL DEFAULT '[]',
    is_current INTEGER NOT NULL DEFAULT 1,
    UNIQUE (seq, rules_version)
);

CREATE TABLE IF NOT EXISTS invalidation (
    dir_id TEXT NOT NULL,
    patch_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    recomputed INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (dir_id, patch_id)
);

CREATE TABLE IF NOT EXISTS watermarks (
    key TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_current_seq ON audit(is_current, seq);
CREATE INDEX IF NOT EXISTS idx_invalidation_patch ON invalidation(patch_id);
CREATE INDEX IF NOT EXISTS idx_events_clock ON write_events(clock_ms, seq);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    p = Path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(p),
        timeout=30.0,
        isolation_level=None,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = FULL")
    return conn


def initialize(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('_schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (SCHEMA_VERSION,),
    )


def require_initialized(conn: sqlite3.Connection) -> None:
    table = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='meta'"
    ).fetchone()
    if table is None:
        raise fail(
            "E_DB_NOT_INITIALIZED",
            "database has no schema; run load first",
        )
    row = conn.execute(
        "SELECT value FROM meta WHERE key='_schema_version'"
    ).fetchone()
    if row is None:
        raise fail(
            "E_DB_NOT_INITIALIZED",
            "database has no schema; run load first",
        )


@contextlib.contextmanager
def transaction(
    conn: sqlite3.Connection, *, immediate: bool = True
) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return None if row is None else str(row[0])


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def get_watermark(conn: sqlite3.Connection, key: str, default: int = 0) -> int:
    row = conn.execute("SELECT value FROM watermarks WHERE key=?", (key,)).fetchone()
    return default if row is None else int(row[0])


def set_watermark(conn: sqlite3.Connection, key: str, value: int) -> None:
    conn.execute(
        "INSERT INTO watermarks(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def ensure_empty(conn: sqlite3.Connection) -> None:
    count = int(conn.execute("SELECT COUNT(*) FROM write_events").fetchone()[0])
    if count:
        raise fail(
            "E_ALREADY_LOADED",
            "database already contains write events",
            write_events=count,
        )
