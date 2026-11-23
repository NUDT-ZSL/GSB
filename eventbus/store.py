"""SQLite persistence and versioned configuration pointer."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .errors import err
from .models import Config
from .util import canonical_json, now_ms

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS config_versions (
    version INTEGER PRIMARY KEY,
    parent_version INTEGER,
    config_json TEXT NOT NULL,
    patch_json TEXT,
    fingerprint TEXT,
    created_at_ms INTEGER NOT NULL,
    actor TEXT
);

CREATE TABLE IF NOT EXISTS config_pointer (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    version INTEGER NOT NULL REFERENCES config_versions(version)
);

CREATE TABLE IF NOT EXISTS channels (
    name TEXT PRIMARY KEY,
    generation INTEGER NOT NULL,
    partitions INTEGER NOT NULL,
    field_schema_json TEXT NOT NULL,
    config_version INTEGER NOT NULL,
    low_watermark INTEGER NOT NULL DEFAULT 0,
    high_watermark INTEGER NOT NULL DEFAULT 0,
    retention_bytes INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel TEXT NOT NULL REFERENCES channels(name),
    offset INTEGER NOT NULL,
    sequence INTEGER NOT NULL,
    partition INTEGER NOT NULL,
    partition_count INTEGER NOT NULL,
    partition_key TEXT NOT NULL,
    logical_clock INTEGER NOT NULL,
    produced_at_ms INTEGER NOT NULL,
    fields_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    source_version TEXT NOT NULL,
    source_generation INTEGER NOT NULL,
    raw_repr_json TEXT NOT NULL,
    time_conversion_json TEXT,
    tombstone INTEGER NOT NULL DEFAULT 0,
    compacted INTEGER NOT NULL DEFAULT 0,
    config_version INTEGER NOT NULL,
    batch_id TEXT NOT NULL,
    committed INTEGER NOT NULL DEFAULT 1,
    created_at_ms INTEGER NOT NULL,
    UNIQUE (channel, offset),
    UNIQUE (channel, sequence)
);

CREATE INDEX IF NOT EXISTS idx_events_channel_offset ON events(channel, offset);
CREATE INDEX IF NOT EXISTS idx_events_channel_sequence ON events(channel, sequence);

CREATE TABLE IF NOT EXISTS batches (
    channel TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    start_offset INTEGER NOT NULL,
    end_offset INTEGER NOT NULL,
    event_count INTEGER NOT NULL,
    config_version INTEGER NOT NULL,
    payload_root TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL,
    PRIMARY KEY (channel, batch_id)
);

CREATE TABLE IF NOT EXISTS merge_decisions (
    channel TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    selected_source TEXT NOT NULL,
    selected_generation INTEGER NOT NULL,
    logical_clock INTEGER NOT NULL,
    decision_basis TEXT NOT NULL,
    options_json TEXT NOT NULL,
    raw_v1_json TEXT,
    raw_v2_json TEXT,
    config_version INTEGER NOT NULL,
    PRIMARY KEY (channel, sequence)
);

CREATE TABLE IF NOT EXISTS degradations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    field TEXT NOT NULL,
    source_version TEXT NOT NULL,
    action TEXT NOT NULL,
    default_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    config_version INTEGER NOT NULL,
    UNIQUE (channel, sequence, field, source_version, action)
);

CREATE TABLE IF NOT EXISTS bridge_positions (
    bridge_id TEXT PRIMARY KEY,
    source_channel TEXT NOT NULL,
    target_channel TEXT NOT NULL,
    next_offset INTEGER NOT NULL,
    last_digest TEXT,
    source_generation INTEGER NOT NULL,
    target_generation INTEGER NOT NULL,
    config_version INTEGER NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    updated_at_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS subscriptions (
    name TEXT PRIMARY KEY,
    channel TEXT NOT NULL,
    offset INTEGER NOT NULL,
    config_version INTEGER NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    updated_at_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS bridge_deliveries (
    bridge_id TEXT NOT NULL,
    source_offset INTEGER NOT NULL,
    target_channel TEXT NOT NULL,
    target_offset INTEGER NOT NULL,
    transform_digest TEXT NOT NULL,
    source_payload_sha256 TEXT NOT NULL,
    config_version INTEGER NOT NULL,
    rule_snapshot_json TEXT NOT NULL,
    transform_trace_json TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL,
    PRIMARY KEY (bridge_id, source_offset),
    UNIQUE (target_channel, target_offset)
);

CREATE TABLE IF NOT EXISTS bridge_checks (
    bridge_id TEXT NOT NULL,
    source_offset INTEGER NOT NULL,
    matched INTEGER NOT NULL,
    source_payload_sha256 TEXT NOT NULL,
    source_fields_json TEXT NOT NULL,
    source_logical_clock INTEGER NOT NULL,
    source_produced_at_ms INTEGER NOT NULL,
    source_tombstone INTEGER NOT NULL,
    transform_digest TEXT,
    config_version INTEGER NOT NULL,
    reason TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL,
    PRIMARY KEY (bridge_id, source_offset)
);

CREATE TABLE IF NOT EXISTS bridge_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bridge_id TEXT NOT NULL,
    config_version INTEGER NOT NULL,
    start_offset INTEGER NOT NULL,
    end_offset INTEGER,
    status TEXT NOT NULL,
    error_code TEXT,
    started_at_ms INTEGER NOT NULL,
    completed_at_ms INTEGER,
    details_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_bridge_runs_status ON bridge_runs(status, bridge_id);

CREATE TABLE IF NOT EXISTS operation_intents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL,
    error_code TEXT,
    created_at_ms INTEGER NOT NULL,
    completed_at_ms INTEGER,
    result_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_operation_intents_status
    ON operation_intents(status, id);

CREATE TABLE IF NOT EXISTS subscription_commits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subscription_name TEXT NOT NULL,
    request_id TEXT NOT NULL,
    from_offset INTEGER NOT NULL,
    to_offset INTEGER NOT NULL,
    created_at_ms INTEGER NOT NULL,
    UNIQUE (subscription_name, request_id)
);

CREATE INDEX IF NOT EXISTS idx_subscription_commits_target
    ON subscription_commits(subscription_name, to_offset);

CREATE TABLE IF NOT EXISTS cleanup_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    logical_clock_ms INTEGER NOT NULL,
    retention_bytes INTEGER NOT NULL,
    bytes_before INTEGER NOT NULL,
    bytes_after INTEGER NOT NULL,
    physical_removed INTEGER NOT NULL,
    tombstones_marked INTEGER NOT NULL,
    blocked_json TEXT NOT NULL,
    reason_json TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS cleanup_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    channel TEXT NOT NULL,
    offset INTEGER NOT NULL,
    action TEXT NOT NULL,
    bytes_reclaimed INTEGER NOT NULL,
    reason TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS clock_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    logical_clock_ms INTEGER NOT NULL,
    event_clock_watermark INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL
);
"""


def connect(db_path: str | Path, *, create: bool = False) -> sqlite3.Connection:
    path = Path(db_path)
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    elif not path.exists():
        raise err("NOT_FOUND", "数据库不存在，请先执行 load", path=str(path))
    connection = sqlite3.connect(
        str(path),
        timeout=30.0,
        isolation_level=None,
        check_same_thread=False,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=30000")
    connection.execute("PRAGMA synchronous=FULL")
    return connection


def initialize_schema(db_path: str | Path) -> None:
    connection = connect(db_path, create=True)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.executescript(SCHEMA_SQL)
        connection.execute(
            "INSERT OR IGNORE INTO schema_meta(key, value) VALUES('schema_version', '1')"
        )
    finally:
        connection.close()


@contextmanager
def transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield connection
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    else:
        connection.execute("COMMIT")


def get_config_version(connection: sqlite3.Connection) -> int:
    row = connection.execute("SELECT version FROM config_pointer WHERE id=1").fetchone()
    if row is None:
        raise err("NOT_FOUND", "数据库尚未 load 配置")
    return int(row["version"])


def get_config(
    connection: sqlite3.Connection, version: int | None = None
) -> tuple[int, Config]:
    selected = get_config_version(connection) if version is None else version
    row = connection.execute(
        "SELECT config_json FROM config_versions WHERE version=?", (selected,)
    ).fetchone()
    if row is None:
        raise err("CONFIG_VERSION_INVALID", "配置版本不存在", version=selected)
    return selected, Config.from_dict(_json_loads(row["config_json"]))


def latest_config_version(connection: sqlite3.Connection) -> int:
    row = connection.execute("SELECT COALESCE(MAX(version), 0) AS v FROM config_versions").fetchone()
    return int(row["v"])


def stage_config_version(
    connection: sqlite3.Connection,
    config: Config,
    *,
    version: int,
    parent_version: int | None,
    patch: dict[str, Any] | None,
    created_at_ms: int,
    actor: str,
) -> None:
    """Write through a temp table, then switch the single pointer row atomically."""

    config_json = canonical_json(config.to_dict())
    patch_json = canonical_json(patch) if patch is not None else None
    connection.execute(
        "CREATE TEMP TABLE IF NOT EXISTS config_stage ("
        "version INTEGER PRIMARY KEY, parent_version INTEGER, config_json TEXT, "
        "patch_json TEXT, fingerprint TEXT, created_at_ms INTEGER, actor TEXT)"
    )
    connection.execute("DELETE FROM config_stage")
    connection.execute(
        "INSERT INTO config_stage VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            version,
            parent_version,
            config_json,
            patch_json,
            config.source_fingerprint,
            created_at_ms,
            actor,
        ),
    )
    connection.execute(
        "INSERT INTO config_versions "
        "(version, parent_version, config_json, patch_json, fingerprint, created_at_ms, actor) "
        "SELECT version, parent_version, config_json, patch_json, fingerprint, "
        "created_at_ms, actor FROM config_stage"
    )
    connection.execute("UPDATE config_pointer SET version=? WHERE id=1", (version,))
    if connection.execute("SELECT changes()").fetchone()[0] != 1:
        connection.execute(
            "INSERT INTO config_pointer(id, version) VALUES(1, ?)", (version,)
        )
    connection.execute("DROP TABLE config_stage")


def update_clock(
    connection: sqlite3.Connection,
    requested_ms: int | None,
    tolerance_ms: int,
    *,
    event_clock: int | None = None,
) -> int:
    row = connection.execute(
        "SELECT logical_clock_ms, event_clock_watermark FROM clock_state WHERE id=1"
    ).fetchone()
    if row is None:
        current_ms = 0
        current_event_clock = 0
    else:
        current_ms = int(row["logical_clock_ms"])
        current_event_clock = int(row["event_clock_watermark"])
    if requested_ms is not None and requested_ms < current_ms - tolerance_ms:
        raise err(
            "CLOCK_REWIND",
            "逻辑时钟回拨超过容差",
            current_ms=current_ms,
            requested_ms=requested_ms,
            tolerance_ms=tolerance_ms,
        )
    effective_ms = current_ms if requested_ms is None else max(current_ms, requested_ms)
    effective_event_clock = max(current_event_clock, event_clock or 0)
    connection.execute(
        "INSERT INTO clock_state(id, logical_clock_ms, event_clock_watermark, updated_at_ms) "
        "VALUES(1, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET logical_clock_ms=excluded.logical_clock_ms, "
        "event_clock_watermark=excluded.event_clock_watermark, "
        "updated_at_ms=excluded.updated_at_ms",
        (effective_ms, effective_event_clock, now_ms()),
    )
    return effective_ms


def config_fingerprint(config: Config) -> str:
    return canonical_json(config.to_dict())


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _json_loads(text: str) -> Any:
    import json

    return json.loads(text)
