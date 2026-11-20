from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .errors import fail
from .models import BridgeRule, Channel, Subscription
from .util import canonical_json, digest_json, parse_json_text


SCHEMA_VERSION = "1"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS operations (
    op_key TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    input_json TEXT NOT NULL,
    input_digest TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('pending', 'committed', 'failed')),
    result_json TEXT,
    error_json TEXT,
    clock_ms INTEGER NOT NULL,
    updated_clock INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS config_versions (
    version INTEGER PRIMARY KEY,
    digest TEXT NOT NULL,
    catalog_json TEXT NOT NULL,
    rules_json TEXT NOT NULL,
    subscriptions_json TEXT NOT NULL,
    created_clock INTEGER NOT NULL,
    edit_version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS config_pointer (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    version INTEGER NOT NULL REFERENCES config_versions(version)
);

CREATE TABLE IF NOT EXISTS channel_versions (
    config_version INTEGER NOT NULL,
    name TEXT NOT NULL,
    topic TEXT NOT NULL,
    partitions INTEGER NOT NULL,
    retention_bytes INTEGER NOT NULL,
    max_batch_records INTEGER NOT NULL,
    digest TEXT NOT NULL,
    PRIMARY KEY (config_version, name)
);

CREATE TABLE IF NOT EXISTS rule_versions (
    config_version INTEGER NOT NULL,
    rule_id TEXT NOT NULL,
    upstream TEXT NOT NULL,
    downstream TEXT NOT NULL,
    filter_expr TEXT NOT NULL,
    transform_expr TEXT NOT NULL,
    depends_upstream_offset INTEGER NOT NULL,
    digest TEXT NOT NULL,
    PRIMARY KEY (config_version, rule_id)
);

CREATE TABLE IF NOT EXISTS channels (
    name TEXT PRIMARY KEY,
    next_offset INTEGER NOT NULL DEFAULT 0,
    cleaned_through INTEGER NOT NULL DEFAULT 0,
    updated_clock INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS records (
    channel TEXT NOT NULL,
    offset INTEGER NOT NULL,
    partition INTEGER NOT NULL,
    partition_key_json TEXT NOT NULL,
    payload_json TEXT,
    payload_digest TEXT NOT NULL,
    source_digest TEXT NOT NULL,
    clock_ms INTEGER NOT NULL,
    record_bytes INTEGER NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('live', 'tombstone')),
    origin_type TEXT NOT NULL CHECK (origin_type IN ('producer', 'bridge')),
    origin_ref TEXT NOT NULL,
    config_digest TEXT NOT NULL,
    created_clock INTEGER NOT NULL,
    PRIMARY KEY (channel, offset),
    UNIQUE (channel, origin_type, origin_ref)
);

CREATE TABLE IF NOT EXISTS source_events (
    seq INTEGER PRIMARY KEY,
    topic TEXT NOT NULL,
    partition_key_json TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    clock_ms INTEGER NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('active', 'deleted')),
    revision_version INTEGER NOT NULL DEFAULT 0,
    created_clock INTEGER NOT NULL,
    updated_clock INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS published_events (
    seq INTEGER PRIMARY KEY,
    topic TEXT NOT NULL,
    channel TEXT NOT NULL,
    record_offset INTEGER NOT NULL,
    payload_digest TEXT NOT NULL,
    source_digest TEXT NOT NULL,
    published_clock INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS subscriptions (
    consumer_id TEXT PRIMARY KEY,
    channel TEXT NOT NULL,
    start_offset INTEGER NOT NULL,
    budget INTEGER NOT NULL,
    current_offset INTEGER NOT NULL,
    updated_clock INTEGER NOT NULL,
    config_version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS bridge_state (
    rule_id TEXT PRIMARY KEY,
    upstream_channel TEXT NOT NULL,
    downstream_channel TEXT NOT NULL,
    next_offset INTEGER NOT NULL,
    last_source_offset INTEGER,
    chain_digest TEXT NOT NULL,
    rules_version INTEGER NOT NULL,
    rules_digest TEXT NOT NULL,
    upstream_config_digest TEXT NOT NULL,
    downstream_config_digest TEXT NOT NULL,
    last_upstream_end INTEGER NOT NULL,
    updated_clock INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS bridge_actions (
    rule_id TEXT NOT NULL,
    source_offset INTEGER NOT NULL,
    upstream_channel TEXT NOT NULL,
    source_digest TEXT NOT NULL,
    rules_digest TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('forwarded', 'skipped')),
    transformed_json TEXT,
    transformed_digest TEXT,
    downstream_channel TEXT,
    downstream_offset INTEGER,
    action_digest TEXT NOT NULL,
    clock_ms INTEGER NOT NULL,
    PRIMARY KEY (rule_id, source_offset)
);

CREATE TABLE IF NOT EXISTS append_batches (
    batch_key TEXT PRIMARY KEY,
    channel TEXT NOT NULL,
    start_offset INTEGER NOT NULL,
    end_offset INTEGER NOT NULL,
    event_count INTEGER NOT NULL,
    batch_digest TEXT NOT NULL,
    config_version INTEGER NOT NULL,
    clock_ms INTEGER NOT NULL,
    operation_key TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS checkpoint_reports (
    report_id INTEGER PRIMARY KEY AUTOINCREMENT,
    consumer_id TEXT NOT NULL,
    requested_offset INTEGER NOT NULL,
    previous_offset INTEGER NOT NULL,
    current_offset INTEGER NOT NULL,
    outcome TEXT NOT NULL,
    reason TEXT NOT NULL,
    record_count INTEGER NOT NULL,
    clock_ms INTEGER NOT NULL,
    digest TEXT NOT NULL,
    operation_key TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cleanup_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    operation_key TEXT NOT NULL UNIQUE,
    clock_ms INTEGER NOT NULL,
    summary_json TEXT NOT NULL,
    digest TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS edit_versions (
    version INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    action TEXT NOT NULL,
    object_id TEXT NOT NULL,
    before_json TEXT,
    after_json TEXT,
    impact_json TEXT NOT NULL,
    config_version INTEGER NOT NULL,
    clock_ms INTEGER NOT NULL,
    operation_key TEXT NOT NULL UNIQUE,
    digest TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_records_channel_offset
    ON records(channel, offset);
CREATE INDEX IF NOT EXISTS idx_records_channel_state
    ON records(channel, state, offset);
CREATE INDEX IF NOT EXISTS idx_bridge_actions_downstream
    ON bridge_actions(downstream_channel, downstream_offset);
CREATE INDEX IF NOT EXISTS idx_checkpoint_reports_consumer
    ON checkpoint_reports(consumer_id, report_id);
"""


def connect(db_path: str | Path, readonly: bool = False) -> sqlite3.Connection:
    path = Path(db_path)
    if readonly:
        uri = f"file:{path.resolve()}?mode=ro"
        try:
            conn = sqlite3.connect(
                uri,
                uri=True,
                timeout=10.0,
                isolation_level=None,
            )
        except sqlite3.Error as exc:
            fail("E_DB", "cannot open SQLite database", path=str(path), reason=str(exc))
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            conn = sqlite3.connect(
                str(path),
                timeout=10.0,
                isolation_level=None,
            )
        except sqlite3.Error as exc:
            fail("E_DB", "cannot open SQLite database", path=str(path), reason=str(exc))
    conn.row_factory = sqlite3.Row
    try:
        if not readonly:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
    except sqlite3.Error as exc:
        conn.close()
        fail("E_DB", "cannot configure SQLite database", reason=str(exc))
    return conn


@contextmanager
def transaction(
    conn: sqlite3.Connection,
    immediate: bool = True,
) -> Iterator[sqlite3.Connection]:
    try:
        conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    except sqlite3.Error as exc:
        fail("E_DB", "cannot begin SQLite transaction", reason=str(exc))
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        try:
            conn.execute("COMMIT")
        except sqlite3.Error as exc:
            conn.execute("ROLLBACK")
            fail("E_DB", "cannot commit SQLite transaction", reason=str(exc))


def ensure_schema(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='meta'"
    ).fetchone()
    if row is None:
        conn.executescript(SCHEMA_SQL)
        with transaction(conn):
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                (SCHEMA_VERSION,),
            )
            conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES('commit_seq', '0')"
            )
            conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES('logical_clock', '0')"
            )
            conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES('edit_version', '0')"
            )
        return
    version_row = conn.execute(
        "SELECT value FROM meta WHERE key='schema_version'"
    ).fetchone()
    if version_row is None:
        conn.executescript(SCHEMA_SQL)
        with transaction(conn):
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                (SCHEMA_VERSION,),
            )
            conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES('commit_seq', '0')"
            )
            conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES('logical_clock', '0')"
            )
            conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES('edit_version', '0')"
            )
        return
    if version_row["value"] != SCHEMA_VERSION:
        fail(
            "E_DB",
            "database schema version is unsupported",
            expected=SCHEMA_VERSION,
            actual=version_row["value"],
        )


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return default if row is None else row["value"]


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def get_meta_int(conn: sqlite3.Connection, key: str, default: int = 0) -> int:
    value = get_meta(conn, key)
    return default if value is None else int(value)


def bump_commit_seq(conn: sqlite3.Connection) -> int:
    value = get_meta_int(conn, "commit_seq", 0) + 1
    set_meta(conn, "commit_seq", str(value))
    return value


def get_operation(conn: sqlite3.Connection, op_key: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM operations WHERE op_key=?", (op_key,)
    ).fetchone()


def list_pending_operations(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM operations WHERE state='pending' ORDER BY op_key"
        )
    )


def insert_operation(
    conn: sqlite3.Connection,
    op_key: str,
    kind: str,
    input_value: dict[str, Any],
    clock_ms: int,
) -> None:
    input_json = canonical_json(input_value)
    conn.execute(
        "INSERT INTO operations("
        "op_key, kind, input_json, input_digest, state, clock_ms, updated_clock"
        ") VALUES(?, ?, ?, ?, 'pending', ?, ?)",
        (op_key, kind, input_json, digest_json(input_value), clock_ms, clock_ms),
    )


def mark_operation_committed(
    conn: sqlite3.Connection,
    op_key: str,
    result: dict[str, Any],
    clock_ms: int,
) -> None:
    conn.execute(
        "UPDATE operations SET state='committed', result_json=?, error_json=NULL, "
        "updated_clock=? WHERE op_key=?",
        (canonical_json(result), clock_ms, op_key),
    )


def mark_operation_failed(
    conn: sqlite3.Connection,
    op_key: str,
    error: dict[str, Any],
    clock_ms: int,
) -> None:
    conn.execute(
        "UPDATE operations SET state='failed', error_json=?, updated_clock=? "
        "WHERE op_key=?",
        (canonical_json(error), clock_ms, op_key),
    )


def operation_input(row: sqlite3.Row) -> dict[str, Any]:
    value = parse_json_text(row["input_json"], f"operations.{row['op_key']}")
    if not isinstance(value, dict):
        fail("E_OP_MISMATCH", "stored operation input is not an object")
    return value


def operation_result(row: sqlite3.Row) -> dict[str, Any] | None:
    if row["result_json"] is None:
        return None
    value = parse_json_text(row["result_json"], f"operations.{row['op_key']}")
    return value if isinstance(value, dict) else None


def logical_clock_preflight(
    conn: sqlite3.Connection, clock_ms: int, expected_digest: str | None
) -> None:
    current = get_meta_int(conn, "logical_clock", 0)
    if clock_ms < current:
        fail(
            "E_CLOCK_REGRESSION",
            "injected logical clock is behind the persisted watermark",
            clock_ms=clock_ms,
            watermark=current,
        )
    if expected_digest is not None:
        stored = get_meta(conn, "sample_digest")
        if stored != expected_digest:
            fail(
                "E_SAMPLE_DIGEST",
                "sample digest does not match the persisted sample",
                expected=expected_digest,
                actual=stored,
            )


def advance_logical_clock(conn: sqlite3.Connection, clock_ms: int) -> None:
    current = get_meta_int(conn, "logical_clock", 0)
    if clock_ms < current:
        fail(
            "E_CLOCK_REGRESSION",
            "injected logical clock is behind the persisted watermark",
            clock_ms=clock_ms,
            watermark=current,
        )
    if clock_ms > current:
        set_meta(conn, "logical_clock", str(clock_ms))


def load_config(
    conn: sqlite3.Connection, version: int | None = None
) -> tuple[
    int,
    str,
    list[Channel],
    list[BridgeRule],
    list[Subscription],
    dict[str, Any],
]:
    if version is None:
        pointer = conn.execute(
            "SELECT version FROM config_pointer WHERE id=1"
        ).fetchone()
        if pointer is None:
            fail("E_CONFIG", "event bus is not initialized")
        version = int(pointer["version"])
    row = conn.execute(
        "SELECT * FROM config_versions WHERE version=?", (version,)
    ).fetchone()
    if row is None:
        fail("E_CONFIG", "configuration version does not exist", version=version)
    catalog_raw = parse_json_text(row["catalog_json"], "config.catalog")
    rules_raw = parse_json_text(row["rules_json"], "config.rules")
    subs_raw = parse_json_text(row["subscriptions_json"], "config.subscriptions")
    if not isinstance(catalog_raw, list) or not isinstance(rules_raw, list):
        fail("E_CONFIG", "stored configuration is malformed", version=version)
    if not isinstance(subs_raw, list):
        fail("E_CONFIG", "stored subscriptions are malformed", version=version)
    channels = [
        Channel(
            name=item["name"],
            topic=item["topic"],
            partitions=int(item["partitions"]),
            retention_bytes=int(item["retention_bytes"]),
            max_batch_records=int(item["max_batch_records"]),
        )
        for item in catalog_raw
    ]
    rules = [
        BridgeRule(
            id=item["id"],
            upstream=item["upstream"],
            downstream=item["downstream"],
            filter_expr=item["filter"],
            transform_expr=item["transform"],
            depends_upstream_offset=int(item["depends_upstream_offset"]),
        )
        for item in rules_raw
    ]
    subscriptions = [
        Subscription(
            consumer_id=item["consumer_id"],
            channel=item["channel"],
            start_offset=int(item["start_offset"]),
            budget=int(item["budget"]),
        )
        for item in subs_raw
    ]
    channels.sort(key=lambda item: item.name)
    rules.sort(key=lambda item: item.id)
    subscriptions.sort(key=lambda item: item.consumer_id)
    return (
        version,
        str(row["digest"]),
        channels,
        rules,
        subscriptions,
        {
            "created_clock": int(row["created_clock"]),
            "edit_version": int(row["edit_version"]),
        },
    )


def current_channel_map(conn: sqlite3.Connection) -> dict[str, Channel]:
    _, _, channels, _, _, _ = load_config(conn)
    return {channel.name: channel for channel in channels}


def current_rule_map(conn: sqlite3.Connection) -> dict[str, BridgeRule]:
    _, _, _, rules, _, _ = load_config(conn)
    return {rule.id: rule for rule in rules}


def state_rows(
    conn: sqlite3.Connection,
    table: str,
    order_by: str,
) -> list[dict[str, Any]]:
    rows = conn.execute(f"SELECT * FROM {table} ORDER BY {order_by}").fetchall()
    return [dict(row) for row in rows]


def compute_state_digest(conn: sqlite3.Connection) -> str:
    payload: dict[str, Any] = {}
    tables = [
        ("meta", "key", "key"),
        ("operations", "op_key", "op_key"),
        ("config_versions", "version", "version"),
        ("config_pointer", "id", "id"),
        ("channel_versions", "config_version, name", "config_version, name"),
        ("rule_versions", "config_version, rule_id", "config_version, rule_id"),
        ("channels", "name", "name"),
        ("records", "channel, offset", "channel, offset"),
        ("source_events", "seq", "seq"),
        ("published_events", "seq", "seq"),
        ("subscriptions", "consumer_id", "consumer_id"),
        ("bridge_state", "rule_id", "rule_id"),
        ("bridge_actions", "rule_id, source_offset", "rule_id, source_offset"),
        ("append_batches", "batch_key", "batch_key"),
        ("checkpoint_reports", "report_id", "report_id"),
        ("cleanup_runs", "run_id", "run_id"),
        ("edit_versions", "version", "version"),
    ]
    for table, order_by, _ in tables:
        payload[table] = state_rows(conn, table, order_by)
    return "sha256:" + digest_json(payload)

