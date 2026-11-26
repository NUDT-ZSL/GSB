from __future__ import annotations

import contextlib
import sqlite3
from pathlib import Path
from typing import Iterator

from quota_ledger.errors import fail
from quota_ledger.util import file_lock

SCHEMA_VERSION = "1"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tenants (
    tenant TEXT PRIMARY KEY,
    resource_key TEXT NOT NULL,
    baseline_micros INTEGER NOT NULL,
    elasticity_text TEXT NOT NULL,
    lease_duration_ms INTEGER NOT NULL,
    config_generation INTEGER NOT NULL,
    quota_generation INTEGER NOT NULL,
    threshold_micros INTEGER NOT NULL,
    threshold_generation INTEGER NOT NULL,
    source_version TEXT NOT NULL,
    source_snapshot_id TEXT NOT NULL,
    policy_id TEXT NOT NULL,
    effective_clock_ms INTEGER NOT NULL,
    policy_generation INTEGER NOT NULL,
    safety_watermark_micros INTEGER NOT NULL DEFAULT 0,
    reserved_micros INTEGER NOT NULL DEFAULT 0,
    occupancy_micros INTEGER NOT NULL DEFAULT 0,
    shave_backlog_micros INTEGER NOT NULL DEFAULT 0,
    shave_cost_micros INTEGER NOT NULL DEFAULT 0,
    cumulative_actual_micros INTEGER NOT NULL DEFAULT 0,
    cumulative_released_micros INTEGER NOT NULL DEFAULT 0,
    cumulative_error_bound_micros INTEGER NOT NULL DEFAULT 0,
    cumulative_adjustment_micros INTEGER NOT NULL DEFAULT 0,
    last_adjust_round INTEGER NOT NULL DEFAULT 0,
    UNIQUE(tenant, resource_key)
);

CREATE TABLE IF NOT EXISTS applications (
    application_seq INTEGER PRIMARY KEY,
    tenant TEXT NOT NULL,
    resource_key TEXT NOT NULL,
    requested_micros INTEGER NOT NULL,
    requested_error_micros INTEGER NOT NULL,
    requested_rounding TEXT NOT NULL,
    requested_input TEXT NOT NULL,
    logical_clock_ms INTEGER NOT NULL,
    priority INTEGER NOT NULL,
    state TEXT NOT NULL,
    reserved_micros INTEGER NOT NULL DEFAULT 0,
    estimate_error_bound_micros INTEGER NOT NULL DEFAULT 0,
    actual_micros INTEGER,
    applied_round INTEGER,
    reject_reason TEXT
);

CREATE TABLE IF NOT EXISTS releases (
    release_seq INTEGER PRIMARY KEY,
    application_seq INTEGER NOT NULL UNIQUE,
    tenant TEXT NOT NULL,
    resource_key TEXT NOT NULL,
    actual_micros INTEGER NOT NULL,
    actual_error_micros INTEGER NOT NULL,
    actual_rounding TEXT NOT NULL,
    actual_input TEXT NOT NULL,
    logical_clock_ms INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    processed_round INTEGER,
    difference_micros INTEGER NOT NULL DEFAULT 0,
    shaved_micros INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS observations (
    observation_seq INTEGER PRIMARY KEY,
    tenant TEXT NOT NULL,
    resource_key TEXT NOT NULL,
    actual_occupancy_micros INTEGER NOT NULL,
    actual_error_micros INTEGER NOT NULL,
    actual_rounding TEXT NOT NULL,
    actual_input TEXT NOT NULL,
    logical_clock_ms INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    processed_round INTEGER,
    quota_fixedpoint_micros INTEGER,
    deviation_micros INTEGER,
    deviation_ratio_text TEXT,
    drift_threshold_text TEXT,
    is_danger INTEGER,
    policy_generation INTEGER,
    policy_id TEXT,
    decision_status TEXT
);

CREATE TABLE IF NOT EXISTS policies (
    policy_id TEXT PRIMARY KEY,
    snapshot_id TEXT NOT NULL,
    version TEXT NOT NULL,
    generation INTEGER NOT NULL,
    created_clock_ms INTEGER NOT NULL,
    effective_clock_ms INTEGER NOT NULL,
    params_json TEXT NOT NULL,
    thresholds_json TEXT NOT NULL,
    tombstones_json TEXT NOT NULL,
    dependencies_json TEXT NOT NULL,
    shards_json TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    source_path TEXT NOT NULL,
    version_rank INTEGER NOT NULL,
    UNIQUE(snapshot_id, version, generation)
);

CREATE TABLE IF NOT EXISTS policy_thresholds (
    policy_id TEXT NOT NULL,
    tenant TEXT NOT NULL,
    resource_key TEXT NOT NULL,
    threshold_micros INTEGER NOT NULL,
    PRIMARY KEY (policy_id, tenant),
    FOREIGN KEY (policy_id) REFERENCES policies(policy_id)
);

CREATE TABLE IF NOT EXISTS policy_tombstones (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    policy_id TEXT NOT NULL,
    target_snapshot_id TEXT NOT NULL,
    target_version TEXT NOT NULL,
    tenant TEXT NOT NULL,
    resource_key TEXT NOT NULL,
    invalidated_generation INTEGER NOT NULL,
    effective_clock_ms INTEGER NOT NULL,
    reason TEXT NOT NULL,
    FOREIGN KEY (policy_id) REFERENCES policies(policy_id)
);

CREATE TABLE IF NOT EXISTS resource_dependencies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    policy_id TEXT NOT NULL,
    resource_key TEXT NOT NULL,
    depends_on TEXT NOT NULL,
    FOREIGN KEY (policy_id) REFERENCES policies(policy_id)
);

CREATE TABLE IF NOT EXISTS policy_merge (
    tenant TEXT PRIMARY KEY,
    policy_id TEXT NOT NULL,
    source_version TEXT NOT NULL,
    source_snapshot_id TEXT NOT NULL,
    effective_clock_ms INTEGER NOT NULL,
    policy_generation INTEGER NOT NULL,
    threshold_micros INTEGER NOT NULL,
    invalidated INTEGER NOT NULL,
    recomputed INTEGER NOT NULL,
    previous_policy_id TEXT
);

CREATE TABLE IF NOT EXISTS policy_invalidations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    root_snapshot_id TEXT NOT NULL,
    root_tenant TEXT NOT NULL,
    tenant TEXT NOT NULL,
    resource_key TEXT NOT NULL,
    depth INTEGER NOT NULL,
    direct INTEGER NOT NULL,
    reason TEXT NOT NULL,
    UNIQUE(root_snapshot_id, root_tenant, tenant)
);

CREATE TABLE IF NOT EXISTS policy_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    policy_id TEXT NOT NULL,
    tenant TEXT NOT NULL,
    round_no INTEGER NOT NULL,
    old_threshold_micros INTEGER NOT NULL,
    new_threshold_micros INTEGER NOT NULL,
    source_version TEXT NOT NULL,
    effective_clock_ms INTEGER NOT NULL,
    UNIQUE(policy_id, tenant),
    FOREIGN KEY (policy_id) REFERENCES policies(policy_id)
);

CREATE TABLE IF NOT EXISTS applied_policies (
    policy_id TEXT PRIMARY KEY,
    round_no INTEGER NOT NULL,
    effective_clock_ms INTEGER NOT NULL,
    fingerprint TEXT NOT NULL,
    FOREIGN KEY (policy_id) REFERENCES policies(policy_id)
);

CREATE TABLE IF NOT EXISTS pipeline_rounds (
    round_no INTEGER PRIMARY KEY,
    round_start_ms INTEGER NOT NULL,
    round_end_ms INTEGER NOT NULL,
    process_status TEXT NOT NULL,
    adjust_status TEXT NOT NULL DEFAULT 'pending',
    global_budget_micros INTEGER NOT NULL,
    round_budget_micros INTEGER NOT NULL,
    per_tenant_cap_micros INTEGER NOT NULL,
    safety_watermark_micros INTEGER NOT NULL,
    drift_threshold_text TEXT NOT NULL,
    danger_threshold_text TEXT NOT NULL,
    policy_id TEXT NOT NULL,
    processed_applications INTEGER NOT NULL DEFAULT 0,
    processed_releases INTEGER NOT NULL DEFAULT 0,
    processed_observations INTEGER NOT NULL DEFAULT 0,
    processed_clock_max_ms INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS ledger_entries (
    entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key TEXT NOT NULL UNIQUE,
    event_kind TEXT NOT NULL,
    application_seq INTEGER,
    release_seq INTEGER,
    tenant TEXT NOT NULL,
    resource_key TEXT NOT NULL,
    quota_generation INTEGER NOT NULL,
    policy_generation INTEGER NOT NULL,
    round_no INTEGER NOT NULL,
    logical_clock_ms INTEGER NOT NULL,
    amount_micros INTEGER NOT NULL,
    estimated_micros INTEGER NOT NULL,
    actual_micros INTEGER,
    difference_micros INTEGER NOT NULL,
    estimate_scale INTEGER NOT NULL,
    actual_scale INTEGER NOT NULL,
    rounding_direction TEXT NOT NULL,
    error_bound_micros INTEGER NOT NULL,
    cumulative_error_bound_micros INTEGER NOT NULL,
    reserved_after_micros INTEGER NOT NULL,
    occupancy_after_micros INTEGER NOT NULL,
    available_after_micros INTEGER NOT NULL,
    shaved_micros INTEGER NOT NULL DEFAULT 0,
    details_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS drift_decisions (
    decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
    round_no INTEGER NOT NULL,
    tenant TEXT NOT NULL,
    observation_seq INTEGER NOT NULL,
    source_version TEXT NOT NULL,
    policy_generation INTEGER NOT NULL,
    quota_fixedpoint_micros INTEGER NOT NULL,
    observed_micros INTEGER NOT NULL,
    deviation_micros INTEGER NOT NULL,
    deviation_ratio_text TEXT NOT NULL,
    drift_threshold_text TEXT NOT NULL,
    direction TEXT NOT NULL,
    dangerous INTEGER NOT NULL,
    fair_tag TEXT NOT NULL,
    desired_delta_micros INTEGER NOT NULL,
    applied_delta_micros INTEGER NOT NULL,
    threshold_before_micros INTEGER NOT NULL,
    threshold_after_micros INTEGER NOT NULL,
    remaining_after_micros INTEGER NOT NULL,
    budget_before_micros INTEGER NOT NULL,
    budget_after_micros INTEGER NOT NULL,
    candidate_order INTEGER NOT NULL,
    status TEXT NOT NULL,
    reason TEXT NOT NULL,
    UNIQUE(round_no, tenant)
);

CREATE TABLE IF NOT EXISTS threshold_changes (
    change_id INTEGER PRIMARY KEY AUTOINCREMENT,
    round_no INTEGER NOT NULL,
    tenant TEXT NOT NULL,
    old_threshold_micros INTEGER NOT NULL,
    new_threshold_micros INTEGER NOT NULL,
    delta_micros INTEGER NOT NULL,
    generation_before INTEGER NOT NULL,
    generation_after INTEGER NOT NULL,
    reason TEXT NOT NULL,
    source_version TEXT NOT NULL,
    policy_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shave_logs (
    shave_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key TEXT NOT NULL UNIQUE,
    round_no INTEGER NOT NULL,
    tenant TEXT NOT NULL,
    application_seq INTEGER,
    release_seq INTEGER,
    kind TEXT NOT NULL,
    original_difference_micros INTEGER NOT NULL,
    cap_micros INTEGER NOT NULL,
    returned_micros INTEGER NOT NULL,
    deferred_micros INTEGER NOT NULL,
    drained_micros INTEGER NOT NULL,
    cost_micros INTEGER NOT NULL,
    remaining_after_micros INTEGER NOT NULL,
    safety_watermark_after_micros INTEGER NOT NULL,
    safe_watermark_preserved INTEGER NOT NULL,
    details_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scheduling_rounds (
    round_no INTEGER PRIMARY KEY,
    budget_micros INTEGER NOT NULL,
    used_micros INTEGER NOT NULL,
    remaining_micros INTEGER NOT NULL,
    candidate_count INTEGER NOT NULL,
    danger_candidate_count INTEGER NOT NULL,
    abandoned_count INTEGER NOT NULL,
    candidates_json TEXT NOT NULL,
    abandoned_json TEXT NOT NULL,
    status TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_applications_clock
    ON applications(logical_clock_ms, application_seq);
CREATE INDEX IF NOT EXISTS idx_releases_clock
    ON releases(logical_clock_ms, release_seq);
CREATE INDEX IF NOT EXISTS idx_observations_clock
    ON observations(logical_clock_ms, observation_seq);
CREATE INDEX IF NOT EXISTS idx_ledger_tenant_round
    ON ledger_entries(tenant, round_no);
CREATE INDEX IF NOT EXISTS idx_policy_thresholds_lookup
    ON policy_thresholds(tenant, policy_id);
"""


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path).resolve()
        self.lock_path = self.path.with_name(self.path.name + ".lock")

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(self.path), timeout=30.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with file_lock(self.lock_path):
            connection = self.connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.executescript(SCHEMA_SQL)
                connection.execute(
                    "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
                    (SCHEMA_VERSION,),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def exists(self) -> bool:
        return self.path.exists()

    def is_initialized(self) -> bool:
        if not self.path.exists():
            return False
        connection = self.connect()
        try:
            row = connection.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
            return bool(row)
        except sqlite3.Error:
            return False
        finally:
            connection.close()

    @contextlib.contextmanager
    def write_tx(self) -> Iterator[sqlite3.Connection]:
        with file_lock(self.lock_path):
            connection = self.connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    @contextlib.contextmanager
    def read_conn(self) -> Iterator[sqlite3.Connection]:
        if not self.is_initialized():
            fail("E_STATE_NOT_INITIALIZED", "ledger database is not initialized", str(self.path))
        connection = self.connect()
        try:
            yield connection
        finally:
            connection.close()

    def meta_get(self, connection: sqlite3.Connection, key: str) -> str | None:
        row = connection.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    def meta_set(
        self, connection: sqlite3.Connection, key: str, value: str | int
    ) -> None:
        connection.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )

    def meta_get_int(
        self, connection: sqlite3.Connection, key: str, default: int = 0
    ) -> int:
        value = self.meta_get(connection, key)
        return default if value is None else int(value)

