"""Database helpers shared by the generator, planner and execution engine."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .schema import SCHEMA_VERSION, create_all, drop_all


DEFAULT_DB_PATH = Path("data/water_quality.db")


def connect(db_path: str | Path, *, initialize: bool = True) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    if initialize:
        create_all(conn)
        conn.execute(
            "INSERT OR IGNORE INTO metadata(key, value) VALUES (?, ?)",
            ("schema_version", SCHEMA_VERSION),
        )
        conn.commit()
    return conn


def initialize_database(db_path: str | Path, *, reset: bool = False) -> sqlite3.Connection:
    conn = connect(db_path, initialize=False)
    if reset:
        drop_all(conn)
    create_all(conn)
    conn.execute(
        "INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)",
        ("schema_version", SCHEMA_VERSION),
    )
    conn.commit()
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        conn.execute("BEGIN")
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def metadata_get(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
    return row["value"] if row is not None else default


def metadata_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO metadata(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def data_fingerprint(
    conn: sqlite3.Connection, *, include_queries: bool = True
) -> str:
    """Hash generated source data, independent of physical row ids."""

    payload: dict[str, Any] = {}
    payload["pipes"] = [
        dict(row)
        for row in conn.execute(
            """
            SELECT pipe_id, upstream_node, downstream_node, region, diameter_mm, material
            FROM pipes
            ORDER BY
                CASE WHEN pipe_id IS NULL THEN 1 ELSE 0 END,
                pipe_id,
                upstream_node,
                downstream_node
            """
        )
    ]
    payload["monitoring_points"] = [
        dict(row)
        for row in conn.execute(
            """
            SELECT point_id, pipe_id, location, metric_types
            FROM monitoring_points
            ORDER BY point_id
            """
        )
    ]
    payload["readings"] = [
        dict(row)
        for row in conn.execute(
            """
            SELECT point_id, sampled_at, residual_chlorine, turbidity, ph, water_temp
            FROM readings
            ORDER BY point_id, sampled_at
            """
        )
    ]
    if include_queries:
        payload["query_requests"] = [
            json.loads(row["request_json"])
            for row in conn.execute(
                "SELECT request_json FROM query_requests ORDER BY query_id"
            )
        ]
    return sha256_text(canonical_json(payload))


def source_data_fingerprint(conn: sqlite3.Connection) -> str:
    """Hash pipes, points and readings only (query edits do not change it)."""

    return data_fingerprint(conn, include_queries=False)


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]

