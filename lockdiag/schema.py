"""SQLite schema for the diagnostic state database."""

from __future__ import annotations

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS run_info (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    fingerprint TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    run_db_path TEXT NOT NULL,
    status TEXT NOT NULL,
    initialized_bundle TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transactions (
    tx_id TEXT PRIMARY KEY,
    start_order INTEGER NOT NULL UNIQUE,
    version INTEGER NOT NULL,
    isolation TEXT NOT NULL,
    budget_ms INTEGER NOT NULL,
    depends_json TEXT NOT NULL,
    status TEXT NOT NULL,
    current_op_seq INTEGER NOT NULL DEFAULT 1,
    started_sample INTEGER,
    finished_sample INTEGER,
    wait_total_units INTEGER NOT NULL DEFAULT 0,
    wait_count INTEGER NOT NULL DEFAULT 0,
    wait_actual_error_micro INTEGER NOT NULL DEFAULT 0,
    wait_error_micro INTEGER NOT NULL DEFAULT 0,
    end_reason TEXT,
    worker_pid INTEGER
);

CREATE TABLE IF NOT EXISTS operations (
    tx_id TEXT NOT NULL,
    op_seq INTEGER NOT NULL,
    op_version INTEGER NOT NULL,
    op_id TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    resource_key TEXT,
    resource_type TEXT,
    table_name TEXT NOT NULL,
    row_id INTEGER,
    range_start INTEGER,
    range_end INTEGER,
    requested_mode TEXT,
    index_path TEXT,
    status TEXT NOT NULL,
    plan_json TEXT,
    worker_pid INTEGER,
    acquired_sample INTEGER,
    completed_sample INTEGER,
    reentrant_reason TEXT,
    PRIMARY KEY (tx_id, op_seq, op_version)
);

CREATE INDEX IF NOT EXISTS idx_operations_tx_status
    ON operations(tx_id, op_seq, status);

CREATE TABLE IF NOT EXISTS clock_samples (
    sample_id INTEGER PRIMARY KEY AUTOINCREMENT,
    sequence_index INTEGER NOT NULL UNIQUE,
    raw_value INTEGER NOT NULL,
    raw_units_per_ms INTEGER NOT NULL,
    purpose TEXT NOT NULL,
    tx_id TEXT,
    op_seq INTEGER,
    bundle_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS clock_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    next_sequence INTEGER NOT NULL,
    next_raw INTEGER NOT NULL,
    last_sample_id INTEGER,
    last_raw INTEGER,
    raw_units_per_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS lock_holders (
    holder_id INTEGER PRIMARY KEY AUTOINCREMENT,
    resource_key TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    table_name TEXT NOT NULL,
    row_id INTEGER,
    range_start INTEGER,
    range_end INTEGER,
    mode TEXT NOT NULL,
    tx_id TEXT NOT NULL,
    op_seq INTEGER NOT NULL,
    op_version INTEGER NOT NULL,
    acquired_sample INTEGER NOT NULL,
    reentrant_count INTEGER NOT NULL DEFAULT 0,
    UNIQUE (resource_key, tx_id)
);

CREATE INDEX IF NOT EXISTS idx_lock_holders_tx ON lock_holders(tx_id);
CREATE INDEX IF NOT EXISTS idx_lock_holders_table ON lock_holders(table_name);

CREATE TABLE IF NOT EXISTS wait_queue (
    wait_id TEXT PRIMARY KEY,
    queue_no INTEGER NOT NULL UNIQUE,
    tx_id TEXT NOT NULL,
    op_seq INTEGER NOT NULL,
    op_version INTEGER NOT NULL,
    resource_key TEXT NOT NULL,
    requested_mode TEXT NOT NULL,
    holder_tx TEXT,
    holder_txs_json TEXT NOT NULL,
    index_path TEXT NOT NULL,
    enqueue_sample INTEGER NOT NULL,
    status TEXT NOT NULL,
    resolve_sample INTEGER,
    resolution TEXT,
    reason TEXT,
    UNIQUE (tx_id, op_seq, op_version)
);

CREATE INDEX IF NOT EXISTS idx_wait_queue_status_order
    ON wait_queue(status, queue_no);

CREATE TABLE IF NOT EXISTS waits (
    wait_id TEXT PRIMARY KEY,
    tx_id TEXT NOT NULL,
    op_seq INTEGER NOT NULL,
    op_version INTEGER NOT NULL,
    resource_key TEXT NOT NULL,
    holder_tx TEXT,
    holder_txs_json TEXT NOT NULL,
    index_path TEXT NOT NULL,
    enqueue_sample INTEGER NOT NULL,
    resolve_sample INTEGER,
    enqueue_sequence_index INTEGER NOT NULL,
    resolve_sequence_index INTEGER,
    enqueue_raw INTEGER NOT NULL,
    resolve_raw INTEGER,
    raw_delta INTEGER,
    scale INTEGER NOT NULL,
    raw_units_per_ms INTEGER NOT NULL,
    rounded_units INTEGER,
    exact_numerator INTEGER,
    exact_denominator INTEGER,
    rounding_direction TEXT,
    residual_numerator INTEGER,
    actual_residual_micro INTEGER,
    error_bound_micro INTEGER,
    status TEXT NOT NULL,
    resolution TEXT,
    UNIQUE (tx_id, op_seq, op_version)
);

CREATE INDEX IF NOT EXISTS idx_waits_tx ON waits(tx_id, op_seq);
CREATE INDEX IF NOT EXISTS idx_waits_status ON waits(status);

CREATE TABLE IF NOT EXISTS tx_accounts (
    tx_id TEXT PRIMARY KEY,
    wait_count INTEGER NOT NULL DEFAULT 0,
    wait_total_units INTEGER NOT NULL DEFAULT 0,
    wait_actual_error_micro INTEGER NOT NULL DEFAULT 0,
    wait_error_micro INTEGER NOT NULL DEFAULT 0,
    updated_sample INTEGER
);

CREATE TABLE IF NOT EXISTS tx_error_chains (
    tx_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    wait_id TEXT NOT NULL,
    resource_key TEXT NOT NULL,
    holders_json TEXT NOT NULL,
    enqueue_sample INTEGER NOT NULL,
    resolve_sample INTEGER NOT NULL,
    error_bound_micro INTEGER NOT NULL,
    source_json TEXT NOT NULL,
    PRIMARY KEY (tx_id, ordinal)
);

CREATE TABLE IF NOT EXISTS attribution (
    holder_tx TEXT NOT NULL,
    waiter_tx TEXT NOT NULL,
    index_path TEXT NOT NULL,
    wait_count INTEGER NOT NULL,
    total_units INTEGER NOT NULL,
    actual_error_micro INTEGER NOT NULL,
    error_bound_micro INTEGER NOT NULL,
    PRIMARY KEY (holder_tx, waiter_tx, index_path)
);

CREATE TABLE IF NOT EXISTS precision_flags (
    flag_id INTEGER PRIMARY KEY AUTOINCREMENT,
    reason TEXT NOT NULL,
    tx_a TEXT NOT NULL,
    tx_b TEXT NOT NULL,
    difference_units INTEGER NOT NULL,
    tolerance_units INTEGER NOT NULL,
    first_divergent_sample INTEGER,
    details_json TEXT NOT NULL,
    UNIQUE (reason, tx_a, tx_b)
);

CREATE TABLE IF NOT EXISTS deadlocks (
    deadlock_id INTEGER PRIMARY KEY AUTOINCREMENT,
    detected_sample INTEGER NOT NULL,
    cycle_json TEXT NOT NULL,
    victim_tx TEXT NOT NULL,
    victim_start_order INTEGER NOT NULL,
    basis TEXT NOT NULL,
    released_wait_id TEXT,
    continued_json TEXT NOT NULL,
    queue_json TEXT NOT NULL,
    locks_json TEXT NOT NULL,
    revalidated INTEGER NOT NULL,
    details_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schedule_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    rr_start_order INTEGER NOT NULL DEFAULT 0,
    steps INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    next_queue_no INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS recovery_intents (
    intent_id INTEGER PRIMARY KEY AUTOINCREMENT,
    tx_id TEXT NOT NULL,
    op_seq INTEGER NOT NULL,
    stage TEXT NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    bundle_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_recovery_intents_status
    ON recovery_intents(status);

CREATE TABLE IF NOT EXISTS commit_bundles (
    bundle_id TEXT PRIMARY KEY,
    tx_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    clock_watermark_sample INTEGER,
    clock_watermark_raw INTEGER,
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    step INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    tx_id TEXT,
    op_seq INTEGER,
    sample_id INTEGER,
    bundle_id TEXT NOT NULL,
    details_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_step ON events(step, event_id);

CREATE TABLE IF NOT EXISTS lock_reentrancy (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tx_id TEXT NOT NULL,
    op_seq INTEGER NOT NULL,
    resource_key TEXT NOT NULL,
    reason TEXT NOT NULL,
    sample_id INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS recovery_runs (
    recovery_id INTEGER PRIMARY KEY AUTOINCREMENT,
    staged_removed INTEGER NOT NULL,
    orphan_locks_removed INTEGER NOT NULL,
    orphan_waits_removed INTEGER NOT NULL,
    queue_json TEXT NOT NULL,
    account_rebuilt INTEGER NOT NULL,
    details_json TEXT NOT NULL
);
"""
