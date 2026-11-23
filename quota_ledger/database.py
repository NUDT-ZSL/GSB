from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import sqlite3
import threading
from typing import Iterator


SCHEMA_VERSION = "1"

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS resource_pools (
    name TEXT PRIMARY KEY,
    capacity_mc INTEGER NOT NULL CHECK (capacity_mc >= 0),
    capacity_mib INTEGER NOT NULL CHECK (capacity_mib >= 0),
    borrowed_mc INTEGER NOT NULL DEFAULT 0 CHECK (borrowed_mc >= 0),
    borrowed_mib INTEGER NOT NULL DEFAULT 0 CHECK (borrowed_mib >= 0)
);

CREATE TABLE IF NOT EXISTS tenants (
    name TEXT PRIMARY KEY,
    resource_pool TEXT NOT NULL REFERENCES resource_pools(name),
    current_version INTEGER NOT NULL CHECK (current_version >= 1),
    current_digest TEXT NOT NULL,
    quota_mc INTEGER NOT NULL CHECK (quota_mc >= 0),
    quota_mib INTEGER NOT NULL CHECK (quota_mib >= 0),
    allow_overdraft INTEGER NOT NULL CHECK (allow_overdraft IN (0, 1)),
    borrow_limit_mc INTEGER NOT NULL CHECK (borrow_limit_mc >= 0),
    borrow_limit_mib INTEGER NOT NULL CHECK (borrow_limit_mib >= 0),
    base_used_mc INTEGER NOT NULL DEFAULT 0 CHECK (base_used_mc >= 0),
    base_used_mib INTEGER NOT NULL DEFAULT 0 CHECK (base_used_mib >= 0),
    borrowed_mc INTEGER NOT NULL DEFAULT 0 CHECK (borrowed_mc >= 0),
    borrowed_mib INTEGER NOT NULL DEFAULT 0 CHECK (borrowed_mib >= 0),
    precision_sensitive INTEGER NOT NULL DEFAULT 0 CHECK (precision_sensitive IN (0, 1)),
    cumulative_error_micro INTEGER NOT NULL DEFAULT 0 CHECK (cumulative_error_micro >= 0),
    created_at_ms INTEGER NOT NULL DEFAULT 0,
    updated_at_ms INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS tenant_versions (
    tenant_name TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 1),
    digest TEXT NOT NULL,
    resource_pool TEXT NOT NULL,
    quota_mc INTEGER NOT NULL CHECK (quota_mc >= 0),
    quota_mib INTEGER NOT NULL CHECK (quota_mib >= 0),
    allow_overdraft INTEGER NOT NULL CHECK (allow_overdraft IN (0, 1)),
    borrow_limit_mc INTEGER NOT NULL CHECK (borrow_limit_mc >= 0),
    borrow_limit_mib INTEGER NOT NULL CHECK (borrow_limit_mib >= 0),
    classification TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (tenant_name, version)
);

CREATE TABLE IF NOT EXISTS services (
    name TEXT PRIMARY KEY,
    resource_pool TEXT NOT NULL REFERENCES resource_pools(name),
    requests_per_round INTEGER NOT NULL CHECK (requests_per_round >= 1),
    max_concurrent_reservations INTEGER NOT NULL CHECK (max_concurrent_reservations >= 1)
);

CREATE TABLE IF NOT EXISTS applications (
    application_id TEXT PRIMARY KEY,
    sequence INTEGER NOT NULL UNIQUE CHECK (sequence >= 1),
    tenant TEXT NOT NULL REFERENCES tenants(name),
    service TEXT NOT NULL REFERENCES services(name),
    request_mc INTEGER NOT NULL CHECK (request_mc > 0),
    request_mib INTEGER NOT NULL CHECK (request_mib > 0),
    requested_at_ms INTEGER NOT NULL CHECK (requested_at_ms >= 0),
    duration_ms INTEGER NOT NULL CHECK (duration_ms >= 0),
    expires_at_ms INTEGER NOT NULL CHECK (expires_at_ms >= requested_at_ms),
    fingerprint TEXT NOT NULL,
    status TEXT NOT NULL,
    reason_code TEXT,
    reason_text TEXT,
    round_id TEXT,
    round_start_ms INTEGER,
    round_budget_ms INTEGER,
    round_enrolled INTEGER NOT NULL DEFAULT 0 CHECK (round_enrolled IN (0, 1)),
    admitted_version INTEGER,
    admitted_at_ms INTEGER,
    precision_sensitive INTEGER NOT NULL DEFAULT 0 CHECK (precision_sensitive IN (0, 1)),
    created_at_ms INTEGER NOT NULL DEFAULT 0,
    updated_at_ms INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_applications_status_sequence
    ON applications(status, sequence);
CREATE INDEX IF NOT EXISTS idx_applications_tenant_status
    ON applications(tenant, status);

CREATE TABLE IF NOT EXISTS reservations (
    reservation_id TEXT PRIMARY KEY,
    application_id TEXT NOT NULL UNIQUE REFERENCES applications(application_id),
    tenant TEXT NOT NULL REFERENCES tenants(name),
    service TEXT NOT NULL REFERENCES services(name),
    declaration_version INTEGER NOT NULL,
    declaration_digest TEXT NOT NULL,
    base_mc INTEGER NOT NULL CHECK (base_mc >= 0),
    base_mib INTEGER NOT NULL CHECK (base_mib >= 0),
    borrow_mc INTEGER NOT NULL CHECK (borrow_mc >= 0),
    borrow_mib INTEGER NOT NULL CHECK (borrow_mib >= 0),
    total_mc INTEGER NOT NULL CHECK (total_mc > 0),
    total_mib INTEGER NOT NULL CHECK (total_mib > 0),
    admitted_at_ms INTEGER NOT NULL,
    expires_at_ms INTEGER NOT NULL CHECK (expires_at_ms >= admitted_at_ms),
    state TEXT NOT NULL CHECK (state IN ('ACTIVE', 'RELEASED')),
    released_at_ms INTEGER,
    release_id TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_reservations_active_application
    ON reservations(application_id) WHERE state = 'ACTIVE';
CREATE INDEX IF NOT EXISTS idx_reservations_tenant_state
    ON reservations(tenant, state);
CREATE INDEX IF NOT EXISTS idx_reservations_service_state
    ON reservations(service, state);
CREATE INDEX IF NOT EXISTS idx_reservations_expiry
    ON reservations(state, expires_at_ms);

CREATE TABLE IF NOT EXISTS wait_queue (
    application_id TEXT PRIMARY KEY REFERENCES applications(application_id),
    tenant TEXT NOT NULL REFERENCES tenants(name),
    sequence INTEGER NOT NULL,
    requested_at_ms INTEGER NOT NULL,
    enqueued_at_ms INTEGER NOT NULL,
    reason_code TEXT NOT NULL,
    reason_text TEXT NOT NULL,
    last_release_id TEXT,
    UNIQUE (tenant, sequence)
);

CREATE INDEX IF NOT EXISTS idx_wait_queue_tenant_order
    ON wait_queue(tenant, requested_at_ms, sequence);

CREATE TABLE IF NOT EXISTS borrow_obligations (
    obligation_id TEXT PRIMARY KEY,
    application_id TEXT NOT NULL UNIQUE REFERENCES applications(application_id),
    tenant TEXT NOT NULL REFERENCES tenants(name),
    resource_pool TEXT NOT NULL REFERENCES resource_pools(name),
    borrow_mc INTEGER NOT NULL CHECK (borrow_mc >= 0),
    borrow_mib INTEGER NOT NULL CHECK (borrow_mib >= 0),
    source_available_mc INTEGER NOT NULL CHECK (source_available_mc >= 0),
    source_available_mib INTEGER NOT NULL CHECK (source_available_mib >= 0),
    outstanding_mc INTEGER NOT NULL CHECK (outstanding_mc >= 0),
    outstanding_mib INTEGER NOT NULL CHECK (outstanding_mib >= 0),
    borrowed_at_ms INTEGER NOT NULL,
    due_at_ms INTEGER NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('OPEN', 'RETURNED')),
    returned_at_ms INTEGER,
    release_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_borrow_obligations_tenant_state
    ON borrow_obligations(tenant, state);

CREATE TABLE IF NOT EXISTS operations (
    operation_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('ADMIT', 'RELEASE')),
    application_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('PREPARED', 'DONE', 'FAILED')),
    payload_json TEXT NOT NULL,
    result_json TEXT,
    error_code TEXT,
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_operations_state
    ON operations(state, created_at_ms);

CREATE TABLE IF NOT EXISTS release_events (
    release_id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL UNIQUE REFERENCES operations(operation_id),
    application_id TEXT NOT NULL REFERENCES applications(application_id),
    trigger_type TEXT NOT NULL,
    trigger_source_application_id TEXT NOT NULL,
    requested_at_ms INTEGER NOT NULL,
    released_at_ms INTEGER,
    declaration_version INTEGER,
    released_base_mc INTEGER,
    released_base_mib INTEGER,
    returned_borrow_mc INTEGER,
    returned_borrow_mib INTEGER,
    state TEXT NOT NULL CHECK (state IN ('PREPARED', 'COMPLETE')),
    wake_json TEXT,
    final_queue_json TEXT,
    completed_at_ms INTEGER
);

CREATE TABLE IF NOT EXISTS wake_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    release_id TEXT NOT NULL REFERENCES release_events(release_id),
    tenant TEXT NOT NULL,
    candidate_application_id TEXT NOT NULL,
    queue_rank INTEGER NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('ADMITTED', 'BLOCKED')),
    reason_code TEXT,
    reason_text TEXT,
    at_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS ledger_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    transaction_id TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    sequence_in_transaction INTEGER NOT NULL,
    tenant TEXT NOT NULL,
    application_id TEXT NOT NULL,
    entry_type TEXT NOT NULL CHECK (
        entry_type IN ('QUOTA_ADMIT', 'BORROW_ADMIT', 'BORROW_RETURN', 'QUOTA_RELEASE')
    ),
    amount_mc INTEGER NOT NULL CHECK (amount_mc >= 0),
    amount_mib INTEGER NOT NULL CHECK (amount_mib >= 0),
    base_used_before_mc INTEGER NOT NULL,
    base_used_after_mc INTEGER NOT NULL,
    base_used_before_mib INTEGER NOT NULL,
    base_used_after_mib INTEGER NOT NULL,
    borrowed_before_mc INTEGER NOT NULL,
    borrowed_after_mc INTEGER NOT NULL,
    borrowed_before_mib INTEGER NOT NULL,
    borrowed_after_mib INTEGER NOT NULL,
    pool_borrowed_before_mc INTEGER,
    pool_borrowed_after_mc INTEGER,
    pool_borrowed_before_mib INTEGER,
    pool_borrowed_after_mib INTEGER,
    declaration_version INTEGER NOT NULL,
    at_ms INTEGER NOT NULL,
    UNIQUE (transaction_id, sequence_in_transaction)
);

CREATE INDEX IF NOT EXISTS idx_ledger_tenant
    ON ledger_entries(tenant, id);
CREATE INDEX IF NOT EXISTS idx_ledger_application
    ON ledger_entries(application_id, id);

CREATE TABLE IF NOT EXISTS conversion_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    operation_id TEXT NOT NULL,
    tenant TEXT NOT NULL,
    application_id TEXT NOT NULL,
    field TEXT NOT NULL,
    scale INTEGER NOT NULL,
    rounding TEXT NOT NULL,
    raw_numerator INTEGER NOT NULL,
    raw_denominator INTEGER NOT NULL,
    converted INTEGER NOT NULL,
    error_bound_micro INTEGER NOT NULL,
    at_ms INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_conversion_application
    ON conversion_steps(application_id, id);

CREATE TABLE IF NOT EXISTS precision_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant TEXT NOT NULL,
    application_id TEXT NOT NULL,
    decision TEXT NOT NULL,
    gap_micro INTEGER NOT NULL,
    tolerance_micro INTEGER NOT NULL,
    sensitive INTEGER NOT NULL CHECK (sensitive IN (0, 1)),
    chain_json TEXT NOT NULL,
    at_ms INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_precision_tenant
    ON precision_events(tenant, id);

CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    operation_id TEXT NOT NULL,
    anchor TEXT NOT NULL,
    logical_clock_ms INTEGER NOT NULL,
    submitted_watermark INTEGER NOT NULL,
    tenant_digest TEXT NOT NULL,
    reservation_digest TEXT NOT NULL,
    queue_digest TEXT NOT NULL,
    borrow_digest TEXT NOT NULL,
    ledger_digest TEXT NOT NULL,
    combined_digest TEXT NOT NULL,
    at_ms INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_snapshots_id
    ON snapshots(id DESC);
"""


DELETE_ORDER = (
    "wake_attempts",
    "release_events",
    "precision_events",
    "conversion_steps",
    "ledger_entries",
    "snapshots",
    "operations",
    "borrow_obligations",
    "wait_queue",
    "reservations",
    "applications",
    "services",
    "tenant_versions",
    "tenants",
    "resource_pools",
    "meta",
)


class FileLock:
    """Cross-process exclusive lock backed by an OS lock file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle = None

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = open(self.path, "a+b")
        if os.name == "nt":
            import msvcrt

            if self._handle.seek(0, os.SEEK_END) == 0:
                self._handle.write(b"\0")
                self._handle.flush()
            self._handle.seek(0)
            msvcrt.locking(self._handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        assert self._handle is not None
        if os.name == "nt":
            import msvcrt

            self._handle.seek(0)
            msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        self._handle.close()
        self._handle = None


_PROCESS_LOCKS: dict[str, threading.RLock] = {}
_PROCESS_LOCKS_GUARD = threading.Lock()


def process_lock(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _PROCESS_LOCKS_GUARD:
        lock = _PROCESS_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PROCESS_LOCKS[key] = lock
        return lock


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(db_path),
        timeout=30.0,
        isolation_level=None,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = FULL")
    return conn


def initialize_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    defaults = {
        "schema_version": SCHEMA_VERSION,
        "logical_clock_ms": "0",
        "submitted_watermark": "0",
        "fixed_scale": "1000",
        "precision_tolerance": "0.000001",
        "last_operation_id": "",
    }
    for key, value in defaults.items():
        conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES (?, ?)",
            (key, value),
        )


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
