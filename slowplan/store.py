from __future__ import annotations

import json
import os
import sqlite3
from typing import Any, Iterable

from .errors import BusinessError, E_NOT_INITIALIZED
from .stats import flatten_snapshot
from .util import canonical_json, sha256_json, sha256_text


SCHEMA_VERSION = 1


SCHEMA_SQL = """
PRAGMA foreign_keys = ON;
CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE stats_snapshots (
    version INTEGER PRIMARY KEY,
    collected_at_ms INTEGER NOT NULL,
    effective_at_ms INTEGER NOT NULL,
    database_digest TEXT NOT NULL,
    content_digest TEXT NOT NULL,
    snapshot_json TEXT NOT NULL
);
CREATE TABLE stats_items (
    snapshot_version INTEGER NOT NULL REFERENCES stats_snapshots(version) ON DELETE CASCADE,
    item_key TEXT NOT NULL,
    kind TEXT NOT NULL,
    table_name TEXT NOT NULL,
    column_name TEXT,
    index_name TEXT,
    value_json TEXT NOT NULL,
    value_num REAL NOT NULL,
    PRIMARY KEY (snapshot_version, item_key)
);
CREATE TABLE stats_activations (
    snapshot_version INTEGER PRIMARY KEY REFERENCES stats_snapshots(version) ON DELETE CASCADE,
    activated_clock_ms INTEGER NOT NULL
);
CREATE TABLE templates (
    template_id TEXT PRIMARY KEY,
    template_sql TEXT NOT NULL,
    structure_json TEXT NOT NULL,
    first_seq INTEGER NOT NULL
);
CREATE TABLE samples (
    sample_seq INTEGER PRIMARY KEY,
    collected_at_ms INTEGER NOT NULL,
    sql_text TEXT NOT NULL,
    template_id TEXT NOT NULL REFERENCES templates(template_id),
    duration_ms INTEGER NOT NULL,
    rows_scanned INTEGER NOT NULL,
    rows_returned INTEGER NOT NULL,
    index_hit TEXT,
    session_id TEXT NOT NULL,
    window_start_ms INTEGER NOT NULL,
    sample_digest TEXT NOT NULL
);
CREATE INDEX samples_template_seq ON samples(template_id, sample_seq);
CREATE TABLE dependencies (
    graph_version INTEGER NOT NULL,
    src TEXT NOT NULL,
    dst TEXT NOT NULL,
    edge_type TEXT NOT NULL,
    PRIMARY KEY (graph_version, src, dst, edge_type)
);
CREATE TABLE aggregates (
    aggregate_id TEXT PRIMARY KEY,
    template_id TEXT NOT NULL REFERENCES templates(template_id),
    stats_version INTEGER NOT NULL REFERENCES stats_snapshots(version),
    input_digest TEXT NOT NULL,
    aggregate_json TEXT NOT NULL,
    UNIQUE (template_id, stats_version, input_digest)
);
CREATE TABLE current_aggregates (
    template_id TEXT PRIMARY KEY REFERENCES templates(template_id),
    aggregate_id TEXT NOT NULL REFERENCES aggregates(aggregate_id)
);
CREATE TABLE profiles (
    profile_id TEXT PRIMARY KEY,
    template_id TEXT NOT NULL REFERENCES templates(template_id),
    stats_version INTEGER NOT NULL REFERENCES stats_snapshots(version),
    profile_version TEXT NOT NULL,
    aggregate_id TEXT NOT NULL REFERENCES aggregates(aggregate_id),
    input_digest TEXT NOT NULL,
    conclusion_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    profile_digest TEXT NOT NULL,
    created_graph_version INTEGER NOT NULL,
    UNIQUE (template_id, stats_version, profile_version)
);
CREATE TABLE current_profiles (
    template_id TEXT PRIMARY KEY REFERENCES templates(template_id),
    profile_id TEXT NOT NULL REFERENCES profiles(profile_id)
);
CREATE TABLE invalidations (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_version INTEGER NOT NULL REFERENCES stats_snapshots(version),
    template_id TEXT NOT NULL REFERENCES templates(template_id),
    triggers_json TEXT NOT NULL,
    paths_json TEXT NOT NULL,
    old_profile_id TEXT,
    status TEXT NOT NULL,
    created_clock_ms INTEGER NOT NULL,
    resolved_profile_id TEXT,
    UNIQUE (snapshot_version, template_id)
);
CREATE INDEX invalidations_template ON invalidations(template_id, snapshot_version);
CREATE TABLE profile_errors (
    error_id INTEGER PRIMARY KEY AUTOINCREMENT,
    template_id TEXT NOT NULL REFERENCES templates(template_id),
    stats_version INTEGER NOT NULL REFERENCES stats_snapshots(version),
    run_id TEXT NOT NULL,
    code TEXT NOT NULL,
    details_json TEXT NOT NULL,
    created_clock_ms INTEGER NOT NULL,
    UNIQUE (template_id, stats_version, run_id)
);
CREATE TABLE operations (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    op_id TEXT NOT NULL UNIQUE,
    op_type TEXT NOT NULL,
    input_digest TEXT NOT NULL,
    input_json TEXT NOT NULL,
    status TEXT NOT NULL,
    result_json TEXT
);
CREATE INDEX operations_status ON operations(status, seq);
"""


def state_exists(path: str) -> bool:
    return os.path.exists(path)


def open_state(path: str, *, writable: bool = True, create: bool = False) -> sqlite3.Connection:
    if not os.path.exists(path) and not create:
        raise BusinessError(E_NOT_INITIALIZED, "state database is not initialized", {"db": path})
    if create:
        conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    elif writable:
        conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    else:
        uri = f"file:{os.path.abspath(path)}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
    if writable:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = FULL")
    return conn


def initialize_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)


def set_meta(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, canonical_json(value)),
    )


def get_meta(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    if row is None:
        return default
    return json.loads(row["value"])


def get_meta_int(conn: sqlite3.Connection, key: str, default: int = 0) -> int:
    value = get_meta(conn, key, default)
    return int(value)


def insert_snapshot(conn: sqlite3.Connection, snapshot: dict[str, Any]) -> None:
    conn.execute("DROP TABLE IF EXISTS temp.stage_snapshot_header")
    conn.execute("DROP TABLE IF EXISTS temp.stage_snapshot_items")
    conn.execute(
        """
        CREATE TEMP TABLE stage_snapshot_header(
            version INTEGER,
            collected_at_ms INTEGER,
            effective_at_ms INTEGER,
            database_digest TEXT,
            content_digest TEXT,
            snapshot_json TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TEMP TABLE stage_snapshot_items(
            snapshot_version INTEGER,
            item_key TEXT,
            kind TEXT,
            table_name TEXT,
            column_name TEXT,
            index_name TEXT,
            value_json TEXT,
            value_num REAL
        )
        """
    )
    conn.execute(
        """
        INSERT INTO stage_snapshot_header(
            version, collected_at_ms, effective_at_ms, database_digest, content_digest, snapshot_json
        ) VALUES(?, ?, ?, ?, ?, ?)
        """,
        (
            snapshot["version"],
            snapshot["collected_at_ms"],
            snapshot["effective_at_ms"],
            snapshot["database_digest"],
            snapshot["content_digest"],
            canonical_json(snapshot),
        ),
    )
    for item in flatten_snapshot(snapshot):
        conn.execute(
            """
            INSERT INTO stage_snapshot_items(
                snapshot_version, item_key, kind, table_name, column_name, index_name, value_json, value_num
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot["version"],
                item["item_key"],
                item["kind"],
                item["table"],
                item["column"],
                item["index"],
                canonical_json(item["value"]),
                float(item["value"]),
            ),
        )
    conn.execute(
        """
        INSERT INTO stats_snapshots(
            version, collected_at_ms, effective_at_ms, database_digest, content_digest, snapshot_json
        )
        SELECT version, collected_at_ms, effective_at_ms, database_digest, content_digest, snapshot_json
        FROM stage_snapshot_header
        """
    )
    conn.execute(
        """
        INSERT INTO stats_items(
            snapshot_version, item_key, kind, table_name, column_name, index_name, value_json, value_num
        )
        SELECT snapshot_version, item_key, kind, table_name, column_name, index_name, value_json, value_num
        FROM stage_snapshot_items
        """
    )
    conn.execute("DROP TABLE temp.stage_snapshot_header")
    conn.execute("DROP TABLE temp.stage_snapshot_items")


def load_snapshot(conn: sqlite3.Connection, version: int) -> dict[str, Any]:
    row = conn.execute("SELECT snapshot_json FROM stats_snapshots WHERE version=?", (version,)).fetchone()
    if row is None:
        raise BusinessError(E_NOT_INITIALIZED, "statistics snapshot version does not exist", {"version": version})
    return json.loads(row["snapshot_json"])


def list_snapshots(conn: sqlite3.Connection, *, active_only: bool | None = None) -> list[sqlite3.Row]:
    if active_only is True:
        return conn.execute(
            "SELECT * FROM stats_snapshots WHERE version <= ? ORDER BY version",
            (get_meta_int(conn, "active_stats_version"),),
        ).fetchall()
    return conn.execute("SELECT * FROM stats_snapshots ORDER BY version").fetchall()


def committed_version(conn: sqlite3.Connection) -> int:
    return get_meta_int(conn, "committed_stats_version")


def active_version(conn: sqlite3.Connection) -> int:
    return get_meta_int(conn, "active_stats_version")


def snapshot_version_at_clock(conn: sqlite3.Connection, clock_ms: int) -> int | None:
    row = conn.execute(
        "SELECT version FROM stats_snapshots WHERE effective_at_ms <= ? ORDER BY effective_at_ms DESC, version DESC LIMIT 1",
        (clock_ms,),
    ).fetchone()
    return int(row["version"]) if row is not None else None


def record_activation(conn: sqlite3.Connection, version: int, clock_ms: int) -> None:
    conn.execute(
        """
        INSERT INTO stats_activations(snapshot_version, activated_clock_ms) VALUES(?, ?)
        ON CONFLICT(snapshot_version) DO NOTHING
        """,
        (version, clock_ms),
    )


def template_row(conn: sqlite3.Connection, template_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM templates WHERE template_id=?", (template_id,)).fetchone()


def all_templates(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM templates ORDER BY template_id").fetchall()


def template_payload(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "template_id": row["template_id"],
        "template_sql": row["template_sql"],
        "structure": json.loads(row["structure_json"]),
        "first_seq": row["first_seq"],
    }


def insert_template(
    conn: sqlite3.Connection,
    template_id: str,
    template_sql: str,
    structure: dict[str, Any],
    first_seq: int,
) -> None:
    conn.execute(
        "INSERT INTO templates(template_id, template_sql, structure_json, first_seq) VALUES(?, ?, ?, ?)",
        (template_id, template_sql, canonical_json(structure), first_seq),
    )


def upsert_current_aggregate(conn: sqlite3.Connection, template_id: str, aggregate_id: str) -> None:
    conn.execute(
        """
        INSERT INTO current_aggregates(template_id, aggregate_id) VALUES(?, ?)
        ON CONFLICT(template_id) DO UPDATE SET aggregate_id=excluded.aggregate_id
        """,
        (template_id, aggregate_id),
    )


def upsert_current_profile(conn: sqlite3.Connection, template_id: str, profile_id: str) -> None:
    conn.execute(
        """
        INSERT INTO current_profiles(template_id, profile_id) VALUES(?, ?)
        ON CONFLICT(template_id) DO UPDATE SET profile_id=excluded.profile_id
        """,
        (template_id, profile_id),
    )


def clear_current_profile(conn: sqlite3.Connection, template_id: str) -> str | None:
    row = conn.execute(
        "SELECT profile_id FROM current_profiles WHERE template_id=?", (template_id,)
    ).fetchone()
    old = row["profile_id"] if row is not None else None
    conn.execute("DELETE FROM current_profiles WHERE template_id=?", (template_id,))
    conn.execute("DELETE FROM current_aggregates WHERE template_id=?", (template_id,))
    return old


def rebuild_dependencies(conn: sqlite3.Connection, edges: Iterable[dict[str, str]]) -> int:
    ordered = sorted(
        (
            {"src": edge["src"], "dst": edge["dst"], "edge_type": edge["edge_type"]}
            for edge in edges
        ),
        key=lambda edge: (edge["src"], edge["dst"], edge["edge_type"]),
    )
    version = max(1, int(sha256_json(ordered)[:15], 16))
    conn.execute("DELETE FROM dependencies")
    for edge in ordered:
        conn.execute(
            "INSERT INTO dependencies(graph_version, src, dst, edge_type) VALUES(?, ?, ?, ?)",
            (version, edge["src"], edge["dst"], edge["edge_type"]),
        )
    set_meta(conn, "dependency_graph_version", version)
    return version


def current_dependency_edges(conn: sqlite3.Connection) -> list[dict[str, str]]:
    version = get_meta_int(conn, "dependency_graph_version")
    return [
        {"src": row["src"], "dst": row["dst"], "edge_type": row["edge_type"]}
        for row in conn.execute(
            "SELECT src, dst, edge_type FROM dependencies WHERE graph_version=? ORDER BY src, dst, edge_type",
            (version,),
        )
    ]


def get_operation(conn: sqlite3.Connection, op_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM operations WHERE op_id=?", (op_id,)).fetchone()


def operation_id(op_type: str, input_digest: str) -> str:
    return "op_" + sha256_text(f"{op_type}:{input_digest}")[:24]


def start_operation(
    conn: sqlite3.Connection,
    op_type: str,
    input_digest: str,
    payload: dict[str, Any],
) -> tuple[str, sqlite3.Row | None]:
    op_id = operation_id(op_type, input_digest)
    existing = get_operation(conn, op_id)
    if existing is not None:
        return op_id, existing
    conn.execute(
        """
        INSERT INTO operations(op_id, op_type, input_digest, input_json, status, result_json)
        VALUES(?, ?, ?, ?, 'running', NULL)
        """,
        (op_id, op_type, input_digest, canonical_json(payload)),
    )
    return op_id, None


def complete_operation(conn: sqlite3.Connection, op_id: str, result: dict[str, Any]) -> None:
    conn.execute(
        "UPDATE operations SET status='completed', result_json=? WHERE op_id=?",
        (canonical_json(result), op_id),
    )


def running_operations(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM operations WHERE status='running' ORDER BY seq").fetchall()


def state_digest(conn: sqlite3.Connection) -> str:
    payload: dict[str, Any] = {}
    payload["meta"] = [[row["key"], json.loads(row["value"])] for row in conn.execute("SELECT key, value FROM meta ORDER BY key")]
    payload["stats_snapshots"] = [
        [
            row["version"],
            row["collected_at_ms"],
            row["effective_at_ms"],
            row["database_digest"],
            row["content_digest"],
        ]
        for row in conn.execute("SELECT * FROM stats_snapshots ORDER BY version")
    ]
    payload["stats_items"] = [
        [row["snapshot_version"], row["item_key"], row["value_json"]]
        for row in conn.execute("SELECT * FROM stats_items ORDER BY snapshot_version, item_key")
    ]
    payload["stats_activations"] = [
        [row["snapshot_version"], row["activated_clock_ms"]]
        for row in conn.execute("SELECT * FROM stats_activations ORDER BY snapshot_version")
    ]
    payload["templates"] = [
        [row["template_id"], row["template_sql"], row["structure_json"], row["first_seq"]]
        for row in conn.execute("SELECT * FROM templates ORDER BY template_id")
    ]
    payload["samples"] = [
        [
            row["sample_seq"],
            row["collected_at_ms"],
            row["template_id"],
            row["duration_ms"],
            row["rows_scanned"],
            row["rows_returned"],
            row["index_hit"],
            row["session_id"],
            row["window_start_ms"],
            row["sample_digest"],
        ]
        for row in conn.execute("SELECT * FROM samples ORDER BY sample_seq")
    ]
    payload["dependencies"] = [
        [row["src"], row["dst"], row["edge_type"]]
        for row in conn.execute("SELECT * FROM dependencies ORDER BY graph_version, src, dst, edge_type")
    ]
    payload["aggregates"] = [
        [row["aggregate_id"], row["template_id"], row["stats_version"], row["input_digest"], row["aggregate_json"]]
        for row in conn.execute("SELECT * FROM aggregates ORDER BY aggregate_id")
    ]
    payload["current_aggregates"] = [
        [row["template_id"], row["aggregate_id"]]
        for row in conn.execute("SELECT * FROM current_aggregates ORDER BY template_id")
    ]
    payload["profiles"] = [
        [
            row["profile_id"],
            row["template_id"],
            row["stats_version"],
            row["profile_version"],
            row["input_digest"],
            row["conclusion_json"],
            row["evidence_json"],
            row["profile_digest"],
        ]
        for row in conn.execute("SELECT * FROM profiles ORDER BY profile_id")
    ]
    payload["current_profiles"] = [
        [row["template_id"], row["profile_id"]]
        for row in conn.execute(
            "SELECT c.template_id, c.profile_id FROM current_profiles c ORDER BY c.template_id"
        )
    ]
    payload["invalidations"] = [
        [
            row["snapshot_version"],
            row["template_id"],
            row["triggers_json"],
            row["paths_json"],
            row["old_profile_id"],
            row["status"],
            row["created_clock_ms"],
            row["resolved_profile_id"],
        ]
        for row in conn.execute(
            "SELECT * FROM invalidations ORDER BY snapshot_version, template_id"
        )
    ]
    payload["profile_errors"] = [
        [row["template_id"], row["stats_version"], row["run_id"], row["code"], row["details_json"]]
        for row in conn.execute(
            "SELECT * FROM profile_errors ORDER BY template_id, stats_version, run_id"
        )
    ]
    return sha256_json(payload)
