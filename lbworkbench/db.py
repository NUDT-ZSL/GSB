from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .locking import FileLock
from .util import ensure_workdir


SCHEMA_VERSION = "1"


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshots (
    version TEXT PRIMARY KEY,
    package_digest TEXT NOT NULL,
    instance_set_sha256 TEXT NOT NULL,
    logical_clock_ms INTEGER NOT NULL,
    state_generation_floor INTEGER NOT NULL,
    package_json TEXT NOT NULL,
    source_hashes_json TEXT NOT NULL,
    v1_snapshot_id TEXT NOT NULL,
    v2_snapshot_id TEXT NOT NULL,
    merged_digest TEXT NOT NULL,
    baseline_state_digest TEXT NOT NULL,
    record_count INTEGER NOT NULL,
    load_kind TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshot_records (
    snapshot_version TEXT NOT NULL,
    source_format TEXT NOT NULL,
    canonical_instance_id TEXT NOT NULL,
    original_instance_id TEXT NOT NULL,
    record_json TEXT NOT NULL,
    PRIMARY KEY (snapshot_version, source_format, canonical_instance_id),
    FOREIGN KEY (snapshot_version) REFERENCES snapshots(version)
);

CREATE TABLE IF NOT EXISTS merged_instances (
    snapshot_version TEXT NOT NULL,
    instance_id TEXT NOT NULL,
    selected_source TEXT,
    health_status TEXT NOT NULL,
    state_generation INTEGER NOT NULL,
    consecutive_failures INTEGER,
    consecutive_successes INTEGER,
    effective_weight INTEGER NOT NULL,
    base_weight INTEGER NOT NULL,
    failure_at_ms INTEGER,
    tombstone INTEGER NOT NULL,
    manual INTEGER NOT NULL,
    sources_json TEXT NOT NULL,
    decision_json TEXT NOT NULL,
    PRIMARY KEY (snapshot_version, instance_id),
    FOREIGN KEY (snapshot_version) REFERENCES snapshots(version)
);

CREATE TABLE IF NOT EXISTS instance_state (
    snapshot_version TEXT NOT NULL,
    instance_id TEXT NOT NULL,
    health_status TEXT NOT NULL,
    state_generation INTEGER NOT NULL,
    consecutive_failures INTEGER NOT NULL,
    consecutive_successes INTEGER NOT NULL,
    effective_weight INTEGER NOT NULL,
    base_weight INTEGER NOT NULL,
    failure_at_ms INTEGER,
    tombstone INTEGER NOT NULL,
    revision INTEGER NOT NULL DEFAULT 0,
    last_round_id TEXT,
    PRIMARY KEY (snapshot_version, instance_id),
    FOREIGN KEY (snapshot_version) REFERENCES snapshots(version)
);

CREATE TABLE IF NOT EXISTS plans (
    plan_id TEXT PRIMARY KEY,
    plan_digest TEXT NOT NULL,
    plan_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rounds (
    round_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    round_index INTEGER NOT NULL,
    snapshot_version TEXT NOT NULL,
    logical_clock_ms INTEGER NOT NULL,
    plan_digest TEXT NOT NULL,
    round_plan_json TEXT NOT NULL,
    status TEXT NOT NULL,
    expected_instance_ids_json TEXT NOT NULL,
    expected_count INTEGER NOT NULL,
    completed_count INTEGER NOT NULL DEFAULT 0,
    finalization_revision INTEGER NOT NULL DEFAULT 0,
    UNIQUE (plan_id, round_index, snapshot_version),
    FOREIGN KEY (snapshot_version) REFERENCES snapshots(version),
    FOREIGN KEY (plan_id) REFERENCES plans(plan_id)
);

CREATE TABLE IF NOT EXISTS probe_observations (
    round_id TEXT NOT NULL,
    instance_id TEXT NOT NULL,
    error_code TEXT,
    observed_health TEXT NOT NULL,
    result_json TEXT NOT NULL,
    PRIMARY KEY (round_id, instance_id),
    FOREIGN KEY (round_id) REFERENCES rounds(round_id)
);

CREATE TABLE IF NOT EXISTS drift_attributions (
    snapshot_version TEXT NOT NULL,
    instance_id TEXT NOT NULL,
    round_id TEXT NOT NULL,
    round_index INTEGER NOT NULL,
    logical_clock_ms INTEGER NOT NULL,
    cause TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    PRIMARY KEY (snapshot_version, instance_id),
    FOREIGN KEY (snapshot_version) REFERENCES snapshots(version),
    FOREIGN KEY (round_id) REFERENCES rounds(round_id)
);

CREATE INDEX IF NOT EXISTS idx_rounds_snapshot_clock
ON rounds(snapshot_version, logical_clock_ms, round_index, status);

CREATE INDEX IF NOT EXISTS idx_observations_instance
ON probe_observations(instance_id, round_id);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


class WorkbenchDB:
    def __init__(self, workdir: str | Path):
        self.workdir = ensure_workdir(workdir)
        self.db_path = self.workdir / "workbench.sqlite3"
        self.lock_path = self.workdir / ".workbench.lock"
        self.lock = FileLock(self.lock_path)

    def initialize(self) -> None:
        with self.lock:
            conn = connect(self.db_path)
            try:
                conn.execute("PRAGMA journal_mode = WAL")
                conn.execute("PRAGMA synchronous = FULL")
                conn.executescript(SCHEMA_SQL)
                with self.transaction(conn):
                    conn.execute(
                        "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
                        (SCHEMA_VERSION,),
                    )
                    conn.execute(
                        "INSERT OR IGNORE INTO meta(key, value) VALUES('current_snapshot_version', '')"
                    )
                    conn.execute(
                        "INSERT OR IGNORE INTO meta(key, value) VALUES('clock_waterline_ms', '0')"
                    )
            finally:
                conn.close()

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = connect(self.db_path)
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(
        self,
        conn: sqlite3.Connection,
        *,
        immediate: bool = True,
    ) -> Iterator[sqlite3.Connection]:
        conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise

    def read_meta(self, conn: sqlite3.Connection) -> dict[str, str]:
        return {
            row["key"]: row["value"]
            for row in conn.execute("SELECT key, value FROM meta ORDER BY key")
        }

    def get_meta(self, conn: sqlite3.Connection, key: str, default: str = "") -> str:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return default if row is None else str(row["value"])

    def set_meta(self, conn: sqlite3.Connection, key: str, value: str) -> None:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
