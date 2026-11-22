"""SQLite persistence, advisory file locks and deterministic JSON helpers.

The database is the single source of truth: every window conclusion, alert
state, watermark and cache entry lives here, committed inside transactions so
readers only ever observe fully materialised versions.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, Optional


def dumps_canonical(payload: Any) -> str:
    """Stable JSON encoding: sorted keys, no whitespace churn, no float casts."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS slo_def (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    service     TEXT NOT NULL,
    version     INTEGER NOT NULL,
    payload     TEXT NOT NULL,
    is_current  INTEGER NOT NULL DEFAULT 1
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_slo_current_name
    ON slo_def(name) WHERE is_current = 1;
CREATE UNIQUE INDEX IF NOT EXISTS idx_slo_current_service
    ON slo_def(service) WHERE is_current = 1;
CREATE TABLE IF NOT EXISTS event (
    seq          INTEGER PRIMARY KEY,
    service      TEXT NOT NULL,
    clock_ms     INTEGER NOT NULL,
    success      INTEGER NOT NULL,
    latency_ms   TEXT NOT NULL,
    valid        INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS ingested_event (
    slo_name   TEXT NOT NULL,
    version    INTEGER NOT NULL,
    event_seq  INTEGER NOT NULL,
    window_no  INTEGER NOT NULL,
    accepted   INTEGER NOT NULL,
    PRIMARY KEY (slo_name, version, event_seq)
);
CREATE TABLE IF NOT EXISTS window (
    slo_name        TEXT NOT NULL,
    version         INTEGER NOT NULL,
    window_no       INTEGER NOT NULL,
    start_ms        INTEGER NOT NULL,
    end_ms          INTEGER NOT NULL,
    status          TEXT NOT NULL,
    payload         TEXT NOT NULL,
    payload_digest  TEXT NOT NULL,
    PRIMARY KEY (slo_name, version, window_no)
);
CREATE TABLE IF NOT EXISTS alert_state (
    slo_name       TEXT PRIMARY KEY,
    version        INTEGER NOT NULL,
    state          TEXT NOT NULL,
    fire_streak    INTEGER NOT NULL,
    release_streak INTEGER NOT NULL,
    since_window   INTEGER,
    updated_window INTEGER
);
CREATE TABLE IF NOT EXISTS alert_event (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    slo_name   TEXT NOT NULL,
    version    INTEGER NOT NULL,
    window_no  INTEGER NOT NULL,
    transition TEXT NOT NULL,
    state      TEXT NOT NULL,
    reason     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS watermark (
    slo_name        TEXT PRIMARY KEY,
    version         INTEGER NOT NULL,
    committed_seq   INTEGER NOT NULL,
    committed_clock INTEGER NOT NULL,
    next_window_no  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS late_event (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    slo_name     TEXT NOT NULL,
    version      INTEGER NOT NULL,
    event_seq    INTEGER NOT NULL,
    clock_ms     INTEGER NOT NULL,
    window_no    INTEGER NOT NULL,
    window_end   INTEGER NOT NULL,
    reason       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS error_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    code       TEXT NOT NULL,
    message    TEXT NOT NULL,
    fields     TEXT NOT NULL,
    created_seq INTEGER
);
CREATE TABLE IF NOT EXISTS cache (
    cache_key   TEXT PRIMARY KEY,
    slo_name    TEXT NOT NULL,
    version     INTEGER NOT NULL,
    window_no   INTEGER,
    watermark   INTEGER NOT NULL,
    payload     TEXT NOT NULL,
    created_tick INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cache_version ON cache(slo_name, version, watermark);
CREATE INDEX IF NOT EXISTS idx_window_lookup ON window(slo_name, version, window_no);
"""


class Workspace:
    """Handle to a writable work directory holding the SQLite database + locks."""

    def __init__(self, path: os.PathLike[str] | str) -> None:
        self.root = Path(path)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "slo_workbench.sqlite3"
        self.lock_path = self.root / "aggregate.lock"

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=30.0,
            isolation_level=None,  # explicit BEGIN/COMMIT
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def bootstrap(self) -> None:
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.executescript(SCHEMA)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


class FileLock:
    """Cross-process advisory lock (``flock``) guarding aggregate writers."""

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace
        self._fh = None

    def __enter__(self) -> "FileLock":
        import fcntl

        self._fh = open(self.workspace.lock_path, "a+")
        fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        import fcntl

        assert self._fh is not None
        fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        self._fh.close()
        self._fh = None


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Commit-on-success / rollback-on-error context around an IMMEDIATE txn."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


def get_meta(conn: sqlite3.Connection, key: str, default: Optional[str] = None) -> Optional[str]:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def log_error(conn: sqlite3.Connection, code: str, message: str, fields: Dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO error_log(code, message, fields, created_seq) VALUES(?, ?, ?, ?)",
        (code, message, dumps_canonical(fields), fields.get("event_seq")),
    )
