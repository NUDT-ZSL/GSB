from __future__ import annotations

import contextlib
import fcntl
import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterator, Optional

from .errors import Code, KvError


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS config (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    payload_json TEXT NOT NULL,
    total_memory_bytes INTEGER NOT NULL,
    budget_bytes INTEGER NOT NULL,
    max_concurrent INTEGER NOT NULL,
    scale INTEGER NOT NULL,
    workspace TEXT NOT NULL,
    nvme_path TEXT NOT NULL,
    lock_path TEXT NOT NULL,
    loaded_clock INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS requests (
    seq_id TEXT PRIMARY KEY,
    priority INTEGER NOT NULL,
    max_len INTEGER NOT NULL,
    generated_tokens INTEGER NOT NULL,
    request_generation INTEGER NOT NULL DEFAULT 1,
    watermark_token INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'loaded',
    bound_policy_version TEXT,
    payload_json TEXT NOT NULL,
    created_clock INTEGER NOT NULL,
    updated_clock INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS request_layers (
    seq_id TEXT NOT NULL,
    layer_id TEXT NOT NULL,
    total_bytes INTEGER NOT NULL DEFAULT 0,
    hbm_used_bytes INTEGER NOT NULL DEFAULT 0,
    evicted_bytes INTEGER NOT NULL DEFAULT 0,
    fixed_units_cumulative INTEGER NOT NULL DEFAULT 0,
    carry_bytes INTEGER NOT NULL DEFAULT 0,
    error_bound_bytes INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(seq_id, layer_id)
);

CREATE TABLE IF NOT EXISTS layer_state (
    layer_id TEXT PRIMARY KEY,
    ratio_text TEXT NOT NULL,
    capacity_bytes INTEGER NOT NULL,
    hbm_used_bytes INTEGER NOT NULL DEFAULT 0,
    evicted_bytes INTEGER NOT NULL DEFAULT 0,
    fractional_capacity_text TEXT NOT NULL DEFAULT '0'
);

CREATE TABLE IF NOT EXISTS kv_entries (
    entry_id TEXT PRIMARY KEY,
    seq_id TEXT NOT NULL,
    layer_id TEXT NOT NULL,
    token_index INTEGER NOT NULL,
    size_bytes INTEGER NOT NULL,
    digest TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('hbm', 'evicted')),
    created_clock INTEGER NOT NULL,
    UNIQUE(seq_id, layer_id, token_index)
);
CREATE INDEX IF NOT EXISTS idx_entries_selection
    ON kv_entries(seq_id, layer_id, token_index);
CREATE INDEX IF NOT EXISTS idx_entries_state_layer
    ON kv_entries(layer_id, state, token_index);

CREATE TABLE IF NOT EXISTS ledger_conversions (
    conversion_id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_type TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    seq_id TEXT,
    layer_id TEXT,
    source_value_text TEXT NOT NULL,
    scale INTEGER NOT NULL,
    rounding_direction TEXT NOT NULL,
    carry_in INTEGER NOT NULL DEFAULT 0,
    fixed_delta INTEGER NOT NULL,
    carry_out INTEGER NOT NULL DEFAULT 0,
    discarded_remainder INTEGER NOT NULL DEFAULT 0,
    error_bound_bytes INTEGER NOT NULL DEFAULT 0,
    clock_ms INTEGER NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_conversions_request
    ON ledger_conversions(seq_id, layer_id, conversion_id);

CREATE TABLE IF NOT EXISTS policy_versions (
    version TEXT PRIMARY KEY,
    manifest_version INTEGER NOT NULL,
    content_hash TEXT NOT NULL UNIQUE,
    snapshot_type TEXT NOT NULL,
    generation_floor INTEGER NOT NULL,
    declared_clock_ms INTEGER NOT NULL,
    applied_clock_ms INTEGER NOT NULL,
    package_path TEXT,
    report_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS policy_entries (
    policy_version TEXT NOT NULL,
    policy_key TEXT NOT NULL,
    layer_id TEXT NOT NULL,
    source_format TEXT NOT NULL,
    source_version TEXT NOT NULL,
    generation INTEGER NOT NULL,
    declared_clock_ms INTEGER NOT NULL,
    original_time_json TEXT NOT NULL,
    threshold_text TEXT NOT NULL,
    cold_order INTEGER NOT NULL,
    rejected_json TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY(policy_version, policy_key)
);

CREATE TABLE IF NOT EXISTS policy_applied (
    manifest_version INTEGER PRIMARY KEY,
    version TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    applied_clock_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS rounds (
    round_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    clock_ms INTEGER NOT NULL,
    budget_bytes INTEGER NOT NULL,
    max_concurrent INTEGER NOT NULL,
    policy_version TEXT,
    abandon_reason TEXT,
    abandon_detail_json TEXT NOT NULL DEFAULT '{}',
    created_clock INTEGER NOT NULL,
    closed_clock INTEGER
);

CREATE TABLE IF NOT EXISTS eviction_attempts (
    attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
    uid TEXT NOT NULL UNIQUE,
    idempotency_key TEXT NOT NULL UNIQUE,
    round_id TEXT NOT NULL,
    rank_in_round INTEGER NOT NULL,
    attempt_no INTEGER NOT NULL DEFAULT 1,
    seq_id TEXT NOT NULL,
    layer_id TEXT NOT NULL,
    entry_id TEXT NOT NULL,
    actual_bytes INTEGER NOT NULL,
    status TEXT NOT NULL,
    abandon_reason TEXT,
    error_message TEXT,
    fixed_value INTEGER NOT NULL DEFAULT 0,
    carry_in INTEGER NOT NULL DEFAULT 0,
    carry_out INTEGER NOT NULL DEFAULT 0,
    error_bound_bytes INTEGER NOT NULL DEFAULT 0,
    conversion_id INTEGER,
    policy_version TEXT,
    policy_generation INTEGER,
    threshold_text TEXT,
    file_path TEXT,
    file_offset INTEGER,
    frame_length INTEGER,
    header_json TEXT,
    payload_digest TEXT,
    worker_id TEXT,
    created_clock INTEGER NOT NULL,
    started_clock INTEGER,
    finished_clock INTEGER,
    UNIQUE(round_id, rank_in_round)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_committed_entry
    ON eviction_attempts(entry_id) WHERE status = 'committed';
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_entry
    ON eviction_attempts(entry_id) WHERE status IN ('planned', 'writing');
CREATE INDEX IF NOT EXISTS idx_attempts_round_rank
    ON eviction_attempts(round_id, rank_in_round);
CREATE INDEX IF NOT EXISTS idx_attempts_status
    ON eviction_attempts(status);

CREATE TABLE IF NOT EXISTS nvme_files (
    path TEXT PRIMARY KEY,
    next_offset INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS idempotent_ledger (
    idempotency_key TEXT PRIMARY KEY,
    response_hash TEXT NOT NULL,
    attempt_uid TEXT NOT NULL,
    created_clock INTEGER NOT NULL
);
"""


_process_locks: dict[str, threading.RLock] = {}
_process_locks_guard = threading.Lock()


def process_lock(path: str) -> threading.RLock:
    with _process_locks_guard:
        lock = _process_locks.get(path)
        if lock is None:
            lock = threading.RLock()
            _process_locks[path] = lock
        return lock


@contextlib.contextmanager
def nvme_file_lock(lock_path: str | Path) -> Iterator[None]:
    path = Path(lock_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    key = str(path.resolve())
    with process_lock(key):
        with path.open("a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class Database:
    def __init__(self, workspace: str | Path = "run") -> None:
        self.workspace = Path(workspace).resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.db_path = self.workspace / "kvledger.sqlite3"
        self.conn = sqlite3.connect(str(self.db_path), timeout=30.0, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA_SQL)
        self.conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
            ("1",),
        )

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    @contextlib.contextmanager
    def immediate(self) -> Iterator[sqlite3.Connection]:
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise

    def get_meta(self, key: str, default: Optional[str] = None) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row is not None else default

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def require_config(self) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM config WHERE id=1").fetchone()
        if row is None:
            raise KvError(Code.STATE_NOT_INITIALIZED, "workspace is not loaded; run load first")
        return row

    def require_active_policy(self) -> str:
        version = self.get_meta("active_policy_version")
        if not version:
            raise KvError(Code.POLICY_NOT_LOADED, "no active policy snapshot; run merge first")
        row = self.conn.execute("SELECT version FROM policy_versions WHERE version=?", (version,)).fetchone()
        if row is None:
            raise KvError(Code.POLICY_NOT_LOADED, f"active policy pointer is missing: {version}")
        return version

    def paths(self) -> tuple[Path, Path]:
        config = self.require_config()
        nvme = Path(config["nvme_path"])
        lock = Path(config["lock_path"])
        if not nvme.is_absolute():
            nvme = self.workspace / nvme
        if not lock.is_absolute():
            lock = self.workspace / lock
        nvme.parent.mkdir(parents=True, exist_ok=True)
        lock.parent.mkdir(parents=True, exist_ok=True)
        return nvme, lock


def ensure_workspace(path: str | Path) -> Path:
    workspace = Path(path)
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace


def dumps_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def loads_json(text: str) -> Any:
    return json.loads(text)


def fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(str(path), flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
