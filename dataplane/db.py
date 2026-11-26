from __future__ import annotations

import contextlib
import sqlite3
from pathlib import Path
from typing import Iterator, Optional

from .errors import BizError, ErrorCode


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cases (
    case_id TEXT PRIMARY KEY,
    data_size INTEGER NOT NULL,
    feature_dim INTEGER NOT NULL,
    label_dim INTEGER NOT NULL,
    dtype TEXT NOT NULL,
    quant_scale REAL NOT NULL,
    quant_zero_point REAL NOT NULL,
    block_plan_json TEXT NOT NULL,
    revision TEXT NOT NULL,
    source_clock_ms INTEGER NOT NULL,
    case_digest TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    loaded_clock_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS tensor_blocks (
    case_id TEXT NOT NULL,
    role TEXT NOT NULL,
    block_index INTEGER NOT NULL,
    name TEXT NOT NULL,
    shape_json TEXT NOT NULL,
    descriptor_json TEXT NOT NULL,
    content_digest TEXT NOT NULL,
    PRIMARY KEY(case_id, role, block_index, name)
);

CREATE TABLE IF NOT EXISTS patches (
    patch_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    block_start INTEGER NOT NULL,
    block_end INTEGER NOT NULL,
    block_range_json TEXT NOT NULL,
    patch_kind TEXT NOT NULL,
    patch_generation INTEGER NOT NULL,
    logical_clock_ms INTEGER NOT NULL,
    new_digest TEXT NOT NULL,
    content_json TEXT NOT NULL,
    refs_json TEXT NOT NULL,
    received_clock_ms INTEGER NOT NULL,
    applied INTEGER NOT NULL DEFAULT 0,
    superseded INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_patches_case_clock
    ON patches(case_id, logical_clock_ms, patch_generation);

CREATE TABLE IF NOT EXISTS config_snapshot (
    config_version INTEGER PRIMARY KEY,
    package_id TEXT NOT NULL,
    package_hash TEXT NOT NULL,
    format_generation TEXT NOT NULL,
    policy_generation_floor INTEGER NOT NULL,
    effective_at_ms INTEGER NOT NULL,
    classification TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    merged_json TEXT NOT NULL,
    merged_digest TEXT NOT NULL,
    key_set_digest TEXT NOT NULL,
    activated_clock_ms INTEGER NOT NULL,
    UNIQUE(package_hash)
);

CREATE TABLE IF NOT EXISTS config_shard (
    config_version INTEGER NOT NULL,
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    generation INTEGER NOT NULL,
    shard_json TEXT NOT NULL,
    PRIMARY KEY(config_version, path),
    FOREIGN KEY(config_version) REFERENCES config_snapshot(config_version)
);

CREATE TABLE IF NOT EXISTS block_task (
    task_uid TEXT PRIMARY KEY,
    logical_uid TEXT NOT NULL,
    case_id TEXT NOT NULL,
    block_index INTEGER NOT NULL,
    block_kind TEXT NOT NULL,
    config_version INTEGER NOT NULL,
    policy_generation INTEGER NOT NULL,
    revision INTEGER NOT NULL,
    input_chain_json TEXT NOT NULL,
    input_chain_digest TEXT NOT NULL,
    effective_patches_json TEXT NOT NULL,
    cost_ms INTEGER NOT NULL,
    state TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 0,
    lease_owner TEXT,
    lease_pid INTEGER,
    lease_nonce TEXT,
    leased_clock_ms INTEGER,
    started_clock_ms INTEGER,
    finished_clock_ms INTEGER,
    content_digest TEXT,
    object_path TEXT,
    decision_id TEXT,
    failure_code TEXT,
    failure_message TEXT,
    created_clock_ms INTEGER NOT NULL,
    updated_clock_ms INTEGER NOT NULL,
    UNIQUE(logical_uid, revision),
    UNIQUE(case_id, block_index, block_kind, config_version, input_chain_digest)
);
CREATE INDEX IF NOT EXISTS idx_block_task_state_order
    ON block_task(state, case_id, block_index);
CREATE INDEX IF NOT EXISTS idx_block_task_logical
    ON block_task(logical_uid, revision);

CREATE TABLE IF NOT EXISTS invalidation_closure (
    closure_uid TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    closure_digest TEXT NOT NULL,
    roots_json TEXT NOT NULL,
    affected_json TEXT NOT NULL,
    winners_json TEXT NOT NULL,
    patch_ids_json TEXT NOT NULL,
    clock_ms INTEGER NOT NULL,
    created_at_clock_ms INTEGER NOT NULL,
    UNIQUE(case_id, closure_digest)
);

CREATE TABLE IF NOT EXISTS schedule_round (
    round_id TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    clock_ms INTEGER NOT NULL,
    budget_ms INTEGER NOT NULL,
    concurrency_limit INTEGER NOT NULL,
    config_version INTEGER,
    status TEXT NOT NULL,
    candidate_count INTEGER NOT NULL DEFAULT 0,
    selected_count INTEGER NOT NULL DEFAULT 0,
    completed_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    not_started_count INTEGER NOT NULL DEFAULT 0,
    abandoned_count INTEGER NOT NULL DEFAULT 0,
    remaining_blocks INTEGER NOT NULL DEFAULT 0,
    remaining_cost_ms INTEGER NOT NULL DEFAULT 0,
    report_json TEXT NOT NULL,
    created_clock_ms INTEGER NOT NULL,
    updated_clock_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS round_item (
    round_id TEXT NOT NULL,
    task_uid TEXT NOT NULL,
    case_id TEXT NOT NULL,
    block_index INTEGER NOT NULL,
    outcome TEXT NOT NULL,
    reason TEXT NOT NULL,
    budget_state TEXT NOT NULL,
    cost_ms INTEGER NOT NULL,
    config_version INTEGER NOT NULL,
    decision_id TEXT,
    error_code TEXT,
    details_json TEXT NOT NULL,
    PRIMARY KEY(round_id, task_uid)
);
CREATE INDEX IF NOT EXISTS idx_round_item_outcome ON round_item(outcome);

CREATE TABLE IF NOT EXISTS inference_decision (
    decision_id TEXT PRIMARY KEY,
    task_uid TEXT NOT NULL UNIQUE,
    case_id TEXT NOT NULL,
    block_index INTEGER NOT NULL,
    config_version INTEGER NOT NULL,
    policy_generation INTEGER NOT NULL,
    input_chain_digest TEXT NOT NULL,
    input_chain_json TEXT NOT NULL,
    quantization_json TEXT NOT NULL,
    candidate_exact REAL NOT NULL,
    candidate_fixed REAL NOT NULL,
    difference REAL NOT NULL,
    tolerance REAL NOT NULL,
    precision_sensitive INTEGER NOT NULL,
    verdict TEXT NOT NULL,
    error_source_chain_json TEXT NOT NULL,
    result_digest TEXT NOT NULL,
    clock_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS error_ledger (
    decision_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    block_index INTEGER NOT NULL,
    config_version INTEGER NOT NULL DEFAULT 0,
    is_current INTEGER NOT NULL DEFAULT 1,
    fixed_point_scale REAL NOT NULL,
    local_error_bound REAL NOT NULL,
    local_error_units INTEGER NOT NULL,
    cumulative_error_bound REAL NOT NULL,
    cumulative_error_units INTEGER NOT NULL,
    source_chain_json TEXT NOT NULL,
    clock_ms INTEGER NOT NULL,
    FOREIGN KEY(decision_id) REFERENCES inference_decision(decision_id)
);
CREATE INDEX IF NOT EXISTS idx_error_current_order
    ON error_ledger(config_version, is_current, case_id, block_index);

CREATE TABLE IF NOT EXISTS batch_adjudication (
    batch_id TEXT PRIMARY KEY,
    clock_ms INTEGER NOT NULL,
    tolerance REAL NOT NULL,
    decision_ids_json TEXT NOT NULL,
    total_error_bound REAL NOT NULL,
    total_error_units INTEGER NOT NULL,
    precision_sensitive_count INTEGER NOT NULL,
    report_json TEXT NOT NULL
);
"""


def connect(db_path: Path | str) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.DatabaseError:
        pass
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', '1')"
    )
    conn.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES ('clock_watermark_ms', '0')"
    )


@contextlib.contextmanager
def immediate(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
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


def get_meta(conn: sqlite3.Connection, key: str, default: Optional[str] = None) -> Optional[str]:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def advance_clock(
    conn: sqlite3.Connection,
    clock_ms: int,
    tolerance_ms: int = 5_000,
) -> int:
    """Validate an injected clock and persist the monotone watermark."""

    if not isinstance(clock_ms, int) or isinstance(clock_ms, bool) or clock_ms < 0:
        raise BizError(
            ErrorCode.CLOCK_ROLLBACK_TOLERANCE,
            "injected logical clock must be a non-negative integer",
            {"clock_ms": clock_ms},
        )
    current = int(get_meta(conn, "clock_watermark_ms", "0") or 0)
    if clock_ms + tolerance_ms < current:
        raise BizError(
            ErrorCode.CLOCK_ROLLBACK_TOLERANCE,
            "injected logical clock moves backwards beyond tolerance",
            {
                "injected_clock_ms": clock_ms,
                "watermark_ms": current,
                "rollback_ms": current - clock_ms,
                "tolerance_ms": tolerance_ms,
            },
        )
    watermark = max(current, clock_ms)
    set_meta(conn, "clock_watermark_ms", str(watermark))
    return watermark


def default_db_path(data_dir: Path) -> Path:
    return data_dir / "state.sqlite3"
