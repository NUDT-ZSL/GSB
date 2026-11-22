"""SQLite state store, durable queues, and cross-process locking."""

from __future__ import annotations

import contextlib
import fcntl
import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any, Iterator

from .cas import ObjectStore
from .model import ConfigSpec

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS configs (
    version INTEGER PRIMARY KEY,
    content_hash TEXT NOT NULL,
    config_json TEXT NOT NULL,
    loaded_order INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    name TEXT PRIMARY KEY,
    depends_json TEXT NOT NULL,
    artifact_name TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    written_at_ms INTEGER NOT NULL,
    last_hit_at_ms INTEGER NOT NULL,
    initial_hit_count INTEGER NOT NULL,
    pinned INTEGER NOT NULL,
    expected_digest TEXT,
    digest TEXT NOT NULL,
    object_relpath TEXT NOT NULL,
    load_order INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS pipelines (
    name TEXT PRIMARY KEY,
    tasks_json TEXT NOT NULL,
    runs_json TEXT NOT NULL,
    load_order INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS artifacts (
    task_name TEXT PRIMARY KEY REFERENCES tasks(name),
    artifact_name TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    written_at_ms INTEGER NOT NULL,
    digest TEXT NOT NULL,
    object_relpath TEXT NOT NULL,
    tier TEXT NOT NULL CHECK (tier IN ('cold', 'warm', 'hot')),
    hit_count INTEGER NOT NULL,
    last_hit_ms INTEGER NOT NULL,
    last_access_ms INTEGER NOT NULL,
    last_access_run_seq INTEGER NOT NULL DEFAULT 0,
    last_decision_run_seq INTEGER NOT NULL DEFAULT 0,
    pinned INTEGER NOT NULL,
    evicted INTEGER NOT NULL DEFAULT 0,
    evicted_at_ms INTEGER,
    evicted_run_seq INTEGER,
    error_bound_num INTEGER NOT NULL DEFAULT 0,
    error_scale INTEGER NOT NULL,
    version INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS tier_accounts (
    tier TEXT PRIMARY KEY CHECK (tier IN ('cold', 'warm', 'hot')),
    capacity_bytes INTEGER NOT NULL,
    used_bytes INTEGER NOT NULL,
    error_num INTEGER NOT NULL DEFAULT 0,
    scale INTEGER NOT NULL,
    updated_run_seq INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS runs (
    pipeline TEXT NOT NULL,
    run_seq INTEGER NOT NULL,
    at_ms INTEGER NOT NULL,
    status TEXT NOT NULL,
    config_version INTEGER NOT NULL,
    task_results_json TEXT NOT NULL,
    PRIMARY KEY (pipeline, run_seq)
);

CREATE TABLE IF NOT EXISTS artifact_refs (
    pipeline TEXT NOT NULL,
    run_seq INTEGER NOT NULL,
    task_name TEXT NOT NULL,
    at_ms INTEGER NOT NULL,
    hit INTEGER NOT NULL,
    reason TEXT NOT NULL,
    PRIMARY KEY (pipeline, run_seq, task_name)
);

CREATE TABLE IF NOT EXISTS tier_transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pipeline TEXT NOT NULL,
    run_seq_start INTEGER NOT NULL,
    run_seq_end INTEGER NOT NULL,
    task_name TEXT NOT NULL,
    trigger TEXT NOT NULL,
    from_tier TEXT NOT NULL,
    to_tier TEXT NOT NULL,
    at_ms INTEGER NOT NULL,
    details_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS run_errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pipeline TEXT NOT NULL,
    run_seq INTEGER NOT NULL,
    task_name TEXT,
    tier TEXT,
    code TEXT NOT NULL,
    message TEXT NOT NULL,
    details_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS capacity_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pipeline TEXT NOT NULL,
    run_seq INTEGER NOT NULL,
    at_ms INTEGER NOT NULL,
    tier TEXT NOT NULL,
    before_bytes INTEGER NOT NULL,
    after_bytes INTEGER NOT NULL,
    capacity_bytes INTEGER NOT NULL,
    scale INTEGER NOT NULL,
    rounding TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    error_num INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS arithmetic_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pipeline TEXT NOT NULL,
    run_seq INTEGER NOT NULL,
    task_name TEXT NOT NULL,
    operation TEXT NOT NULL,
    scale INTEGER NOT NULL,
    rounding TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    converted_value INTEGER NOT NULL,
    error_num INTEGER NOT NULL,
    cumulative_error_num INTEGER NOT NULL,
    details_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pipeline TEXT NOT NULL,
    run_seq INTEGER NOT NULL,
    at_ms INTEGER NOT NULL,
    task_name TEXT NOT NULL,
    artifact_name TEXT NOT NULL,
    tier TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    digest TEXT NOT NULL,
    score_num INTEGER NOT NULL,
    error_num INTEGER NOT NULL,
    decision TEXT NOT NULL,
    reason TEXT NOT NULL,
    reclaim_status TEXT NOT NULL,
    reclaimed_bytes INTEGER NOT NULL DEFAULT 0,
    details_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS abandonments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pipeline TEXT NOT NULL,
    run_seq INTEGER NOT NULL,
    at_ms INTEGER NOT NULL,
    task_name TEXT,
    tier TEXT,
    size_bytes INTEGER,
    reason TEXT NOT NULL,
    code TEXT NOT NULL,
    details_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS precision_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pipeline TEXT NOT NULL,
    run_seq INTEGER NOT NULL,
    at_ms INTEGER NOT NULL,
    tier TEXT NOT NULL,
    candidate_a TEXT NOT NULL,
    candidate_b TEXT NOT NULL,
    score_a_num INTEGER NOT NULL,
    score_b_num INTEGER NOT NULL,
    gap_num INTEGER NOT NULL,
    tolerance INTEGER NOT NULL,
    error_a_num INTEGER NOT NULL,
    error_b_num INTEGER NOT NULL,
    higher_scale_changes INTEGER NOT NULL,
    affects_selection INTEGER NOT NULL,
    details_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reclaim_queue (
    digest TEXT PRIMARY KEY,
    size_bytes INTEGER NOT NULL,
    status TEXT NOT NULL,
    reclaimed_bytes INTEGER NOT NULL DEFAULT 0,
    pipeline TEXT NOT NULL,
    run_seq INTEGER NOT NULL,
    at_ms INTEGER NOT NULL,
    created_order INTEGER NOT NULL,
    details_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_artifacts_tier ON artifacts (tier, evicted);
CREATE INDEX IF NOT EXISTS idx_artifacts_digest ON artifacts (digest, evicted);
CREATE INDEX IF NOT EXISTS idx_refs_task ON artifact_refs (task_name, pipeline, run_seq);
CREATE INDEX IF NOT EXISTS idx_transitions_task ON tier_transitions (task_name, id);
CREATE INDEX IF NOT EXISTS idx_run_errors_run ON run_errors (pipeline, run_seq);
CREATE INDEX IF NOT EXISTS idx_evictions_run ON evictions (pipeline, run_seq);
CREATE INDEX IF NOT EXISTS idx_precision_run ON precision_events (pipeline, run_seq);
"""


class Storage:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.db_path = self.root / "state.sqlite3"
        self.lock_path = self.root / ".engine.lock"
        self._lock_handle = None
        self._objects: ObjectStore | None = None

    @property
    def objects(self) -> ObjectStore:
        if self._objects is None:
            self._objects = ObjectStore(self.root / "objects", self.root / "tmp")
        return self._objects

    def initialize(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "objects").mkdir(parents=True, exist_ok=True)
        (self.root / "tmp").mkdir(parents=True, exist_ok=True)
        conn = self.connection()
        try:
            conn.executescript(SCHEMA)
            current = conn.execute("PRAGMA user_version").fetchone()[0]
            if current not in (0, SCHEMA_VERSION):
                raise RuntimeError(f"unsupported state schema version {current}")
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        finally:
            conn.close()

    def connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.db_path,
            timeout=30.0,
            isolation_level=None,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @contextlib.contextmanager
    def lock(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        handle = self.lock_path.open("a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self.connection()
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
        finally:
            conn.close()

    @staticmethod
    def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    @staticmethod
    def set_meta(conn: sqlite3.Connection, key: str, value: Any) -> None:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )

    @staticmethod
    def next_counter(conn: sqlite3.Connection, key: str) -> int:
        raw = Storage.get_meta(conn, key)
        value = int(raw) + 1 if raw is not None else 1
        Storage.set_meta(conn, key, value)
        return value

    def active_config(self, conn: sqlite3.Connection) -> ConfigSpec | None:
        version = self.get_meta(conn, "active_config_version")
        if version is None:
            return None
        row = conn.execute(
            "SELECT config_json FROM configs WHERE version=?", (int(version),)
        ).fetchone()
        if row is None:
            return None
        return _config_from_dict(json.loads(row["config_json"]))

    def config_version(self, conn: sqlite3.Connection) -> int | None:
        raw = self.get_meta(conn, "active_config_version")
        return None if raw is None else int(raw)

    def logical_clock(self, conn: sqlite3.Connection) -> int:
        raw = self.get_meta(conn, "logical_clock_ms")
        return 0 if raw is None else int(raw)

    def state_loaded(self, conn: sqlite3.Connection) -> bool:
        return self.get_meta(conn, "state_loaded") == "1"

    def close(self) -> None:
        if self._lock_handle is not None:
            self._lock_handle.close()
            self._lock_handle = None


def _config_from_dict(data: dict[str, Any]) -> ConfigSpec:
    capacities = data["tier_capacity_bytes"]
    return ConfigSpec(
        version=int(data["version"]),
        tier_capacity_bytes={
            "cold": int(capacities["cold"]),
            "warm": int(capacities["warm"]),
            "hot": int(capacities["hot"]),
        },
        promotion_min_hits=int(data["promotion_min_hits"]),
        demotion_idle_ms=int(data["demotion_idle_ms"]),
        eviction_batch_size=int(data["eviction_batch_size"]),
        reclaim_byte_budget=int(data["reclaim_byte_budget"]),
        fixed_point_scale=int(data["fixed_point_scale"]),
        comparison_tolerance=int(data["comparison_tolerance"]),
        logical_clock_step_ms=int(data["logical_clock_step_ms"]),
        clock_rollback_tolerance_ms=int(
            data.get("clock_rollback_tolerance_ms", data["comparison_tolerance"])
        ),
    )


def remove_state(root: str | Path) -> None:
    path = Path(root)
    if not path.exists():
        return
    shutil.rmtree(path)


def safe_json_load(value: str | None, default: Any) -> Any:
    if value is None:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default
