from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

from .errors import EngineError, ErrorCode


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS watermarks (
    name TEXT PRIMARY KEY,
    clock_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS operations (
    operation_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    result_json TEXT,
    clock_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS loaded_samples (
    sample_digest TEXT PRIMARY KEY,
    root_path TEXT NOT NULL,
    manifest_version INTEGER,
    payload_json TEXT NOT NULL,
    loaded_clock_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS edge_nodes (
    node_name TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS input_observations (
    seq INTEGER PRIMARY KEY,
    observation_id TEXT NOT NULL UNIQUE,
    node_name TEXT NOT NULL,
    rule_name TEXT NOT NULL,
    result TEXT NOT NULL,
    content_digest TEXT,
    clock_ms INTEGER,
    payload_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS packages (
    package_key TEXT PRIMARY KEY,
    package_version INTEGER NOT NULL,
    min_generation INTEGER NOT NULL,
    ruleset_digest TEXT NOT NULL,
    manifest_digest TEXT NOT NULL,
    decision TEXT,
    payload_json TEXT NOT NULL,
    loaded_clock_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS view_versions (
    view_id INTEGER PRIMARY KEY AUTOINCREMENT,
    view_uid TEXT NOT NULL UNIQUE,
    version_no INTEGER NOT NULL UNIQUE,
    package_version INTEGER,
    injected_clock_ms INTEGER NOT NULL,
    input_digest TEXT NOT NULL UNIQUE,
    decision_digest TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    patch_id TEXT,
    status TEXT NOT NULL DEFAULT 'active'
);

CREATE TABLE IF NOT EXISTS unified_rules (
    view_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    source_format TEXT NOT NULL,
    generation INTEGER NOT NULL,
    declared_clock_ms INTEGER NOT NULL,
    effective_at_ms INTEGER NOT NULL,
    tombstone INTEGER NOT NULL DEFAULT 0,
    verify_type TEXT NOT NULL,
    content_digest TEXT,
    dependencies_json TEXT NOT NULL,
    effective_json TEXT NOT NULL,
    decision_json TEXT NOT NULL,
    rule_json TEXT NOT NULL,
    PRIMARY KEY(view_id, name),
    FOREIGN KEY(view_id) REFERENCES view_versions(view_id)
);

CREATE INDEX IF NOT EXISTS idx_unified_rules_view ON unified_rules(view_id);

CREATE TABLE IF NOT EXISTS patches (
    patch_id TEXT PRIMARY KEY,
    payload_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    submitted_clock_ms INTEGER NOT NULL,
    view_id INTEGER,
    closure_id TEXT,
    status TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS closures (
    closure_id TEXT PRIMARY KEY,
    patch_id TEXT NOT NULL,
    view_id INTEGER NOT NULL,
    payload_hash TEXT NOT NULL,
    result_json TEXT NOT NULL,
    result_digest TEXT NOT NULL UNIQUE,
    created_clock_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS recompute_runs (
    run_id TEXT PRIMARY KEY,
    closure_id TEXT NOT NULL,
    view_id INTEGER NOT NULL,
    result_json TEXT NOT NULL,
    created_clock_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS windows (
    window_id TEXT PRIMARY KEY,
    view_id INTEGER NOT NULL,
    node_name TEXT NOT NULL,
    rule_name TEXT NOT NULL,
    start_ms INTEGER NOT NULL,
    end_ms INTEGER NOT NULL,
    status TEXT NOT NULL,
    bound_clock_ms INTEGER NOT NULL,
    committed_clock_ms INTEGER,
    conclusion_id INTEGER,
    recompute_of_window_id TEXT,
    UNIQUE(view_id, node_name, rule_name, start_ms)
);

CREATE INDEX IF NOT EXISTS idx_windows_status_clock ON windows(status, end_ms);

CREATE TABLE IF NOT EXISTS processed_observations (
    processed_id INTEGER PRIMARY KEY AUTOINCREMENT,
    view_id INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    observation_id TEXT NOT NULL,
    window_id TEXT NOT NULL,
    node_name TEXT NOT NULL,
    rule_name TEXT NOT NULL,
    result TEXT NOT NULL,
    content_digest TEXT,
    event_clock_ms INTEGER,
    payload_hash TEXT NOT NULL,
    origin_json TEXT,
    processed_clock_ms INTEGER NOT NULL,
    UNIQUE(view_id, seq),
    UNIQUE(window_id, seq)
);

CREATE TABLE IF NOT EXISTS conclusions (
    conclusion_id INTEGER PRIMARY KEY AUTOINCREMENT,
    conclusion_uid TEXT NOT NULL UNIQUE,
    view_id INTEGER NOT NULL,
    window_id TEXT NOT NULL UNIQUE,
    node_name TEXT NOT NULL,
    rule_name TEXT NOT NULL,
    start_ms INTEGER NOT NULL,
    end_ms INTEGER NOT NULL,
    verdict_code TEXT NOT NULL,
    consistent INTEGER NOT NULL,
    cache_hit INTEGER NOT NULL,
    result_digest TEXT NOT NULL,
    result_json TEXT NOT NULL,
    committed_clock_ms INTEGER NOT NULL,
    reuse_status TEXT NOT NULL DEFAULT 'computed',
    source_conclusion_id INTEGER
);

CREATE INDEX IF NOT EXISTS idx_conclusions_rule ON conclusions(view_id, rule_name);
"""


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), timeout=30.0, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA synchronous=FULL")
        if str(self.path) != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(windows)").fetchall()}
        if "conclusion_id" not in columns:
            self.conn.execute("ALTER TABLE windows ADD COLUMN conclusion_id INTEGER")

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise

    def get_meta(self, key: str, default: Optional[str] = None) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def active_view_id(self) -> Optional[int]:
        value = self.get_meta("active_view_id")
        return int(value) if value is not None else None

    def require_active_view_id(self) -> int:
        view_id = self.active_view_id()
        if view_id is None:
            raise EngineError(
                ErrorCode.NO_ACTIVE_VIEW,
                "no active rule view; run load and merge first",
            )
        return view_id

    def watermark(self, name: str = "logical_clock") -> Optional[int]:
        row = self.conn.execute(
            "SELECT clock_ms FROM watermarks WHERE name=?", (name,)
        ).fetchone()
        return int(row["clock_ms"]) if row else None

    def advance_watermark(self, clock_ms: int, name: str = "logical_clock") -> int:
        current = self.watermark(name)
        if current is not None and clock_ms < current:
            raise EngineError(
                ErrorCode.CLOCK_ROLLBACK,
                "injected logical clock cannot move backwards",
                details={"requested_clock_ms": clock_ms, "watermark_ms": current},
            )
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO watermarks(name, clock_ms) VALUES(?, ?) "
                "ON CONFLICT(name) DO UPDATE SET clock_ms=excluded.clock_ms",
                (name, clock_ms),
            )
        return clock_ms

    def row_to_dict(self, row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
        return dict(row) if row is not None else None


def open_store(path: str | Path) -> Store:
    return Store(path)
