"""SQLite persistence: snapshots, versions, change log and rollback boundary.

Schema overview
---------------
meta              : single-row bookkeeping (initialized flag)
field             : current field rows, versioned by created_version/dropped info
edge              : current edge rows
version           : one row per successfully applied version (baseline = 0)
snapshot_field    : immutable per-version field snapshot
snapshot_edge     : immutable per-version edge snapshot
change_log        : rule-attributed mutations of each version
audit_event       : deprecated-field deleted-edge lists and other audit facts

All writes of one version happen inside a single SQLite transaction. Any
exception rolls the transaction back, leaving the database exactly at the
previous committed version.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .graph import ChangeRecord
from .models import Edge, Field

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    initialized INTEGER NOT NULL,
    last_version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS field (
    ref TEXT PRIMARY KEY,
    table_name TEXT NOT NULL,
    name TEXT NOT NULL,
    data_type TEXT NOT NULL,
    deprecated INTEGER NOT NULL,
    created_version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS edge (
    edge_id TEXT PRIMARY KEY,
    source_ref TEXT NOT NULL,
    target_ref TEXT NOT NULL,
    origin TEXT NOT NULL,
    created_version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS version (
    version INTEGER PRIMARY KEY,
    status TEXT NOT NULL,
    rule_count INTEGER NOT NULL,
    edge_count INTEGER NOT NULL,
    field_count INTEGER NOT NULL,
    fingerprint TEXT NOT NULL,
    applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshot_field (
    version INTEGER NOT NULL,
    ref TEXT NOT NULL,
    table_name TEXT NOT NULL,
    name TEXT NOT NULL,
    data_type TEXT NOT NULL,
    deprecated INTEGER NOT NULL,
    PRIMARY KEY (version, ref)
);
CREATE TABLE IF NOT EXISTS snapshot_edge (
    version INTEGER NOT NULL,
    edge_id TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    target_ref TEXT NOT NULL,
    origin TEXT NOT NULL,
    PRIMARY KEY (version, edge_id)
);
CREATE TABLE IF NOT EXISTS change_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version INTEGER NOT NULL,
    rule_id TEXT NOT NULL,
    rule_type TEXT NOT NULL,
    action TEXT NOT NULL,
    detail TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version INTEGER NOT NULL,
    rule_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload TEXT NOT NULL
);
"""


class Store:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        self.conn.executescript(SCHEMA)
        row = self.conn.execute("SELECT initialized FROM meta WHERE id = 1").fetchone()
        if row is None:
            self.conn.execute(
                "INSERT INTO meta (id, initialized, last_version) VALUES (1, 0, 0)"
            )
            self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self):
        """Explicit rollback boundary used for every version attempt."""
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def is_initialized(self) -> bool:
        return bool(self.conn.execute("SELECT initialized FROM meta WHERE id = 1").fetchone()[0])

    def last_version(self) -> int:
        return int(self.conn.execute("SELECT last_version FROM meta WHERE id = 1").fetchone()[0])

    def initialize_baseline(self, fields: list[Field], edges: list[Edge]) -> None:
        """Load the pre-migration graph as version 0. Idempotent guard."""
        if self.is_initialized():
            raise RuntimeError("database already initialized")
        with self.transaction() as conn:
            for item in fields:
                conn.execute(
                    "INSERT INTO field (ref, table_name, name, data_type, deprecated, created_version)"
                    " VALUES (?, ?, ?, ?, ?, 0)",
                    (item.ref, item.table, item.name, item.data_type, int(item.deprecated)),
                )
            for item in edges:
                conn.execute(
                    "INSERT INTO edge (edge_id, source_ref, target_ref, origin, created_version)"
                    " VALUES (?, ?, ?, ?, 0)",
                    (item.edge_id, item.source_ref, item.target_ref, item.origin),
                )
            self._write_snapshot(conn, 0, fields, edges)
            fingerprint = self.compute_fingerprint(fields, edges)
            conn.execute(
                "INSERT INTO version (version, status, rule_count, edge_count, field_count,"
                " fingerprint, applied_at) VALUES (0, 'committed', 0, ?, ?, ?, 'baseline')",
                (len(edges), len(fields), fingerprint),
            )
            conn.execute("UPDATE meta SET initialized = 1, last_version = 0 WHERE id = 1")

    def _write_snapshot(
        self,
        conn: sqlite3.Connection,
        version: int,
        fields: list[Field],
        edges: list[Edge],
    ) -> None:
        conn.executemany(
            "INSERT INTO snapshot_field (version, ref, table_name, name, data_type, deprecated)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            [
                (version, item.ref, item.table, item.name, item.data_type, int(item.deprecated))
                for item in fields
            ],
        )
        conn.executemany(
            "INSERT INTO snapshot_edge (version, edge_id, source_ref, target_ref, origin)"
            " VALUES (?, ?, ?, ?, ?)",
            [
                (version, item.edge_id, item.source_ref, item.target_ref, item.origin)
                for item in edges
            ],
        )

    def snapshot_fields(self, version: int) -> list[Field]:
        rows = self.conn.execute(
            "SELECT * FROM snapshot_field WHERE version = ? ORDER BY ref", (version,)
        ).fetchall()
        return [
            Field(row["table_name"], row["name"], row["data_type"], bool(row["deprecated"]))
            for row in rows
        ]

    def snapshot_edges(self, version: int) -> list[Edge]:
        rows = self.conn.execute(
            "SELECT * FROM snapshot_edge WHERE version = ? ORDER BY edge_id", (version,)
        ).fetchall()
        return [
            Edge(row["edge_id"], row["source_ref"], row["target_ref"], row["origin"])
            for row in rows
        ]

    def live_fields(self) -> list[Field]:
        rows = self.conn.execute("SELECT * FROM field ORDER BY ref").fetchall()
        return [
            Field(row["table_name"], row["name"], row["data_type"], bool(row["deprecated"]))
            for row in rows
        ]

    def live_edges(self) -> list[Edge]:
        rows = self.conn.execute("SELECT * FROM edge ORDER BY edge_id").fetchall()
        return [
            Edge(row["edge_id"], row["source_ref"], row["target_ref"], row["origin"])
            for row in rows
        ]

    def commit_version(
        self,
        version: int,
        fields: list[Field],
        edges: list[Edge],
        changes: list[ChangeRecord],
        audit_events: list[dict],
        rule_count: int,
    ) -> str:
        """Atomically replace live state, persist snapshot and attribution."""
        fingerprint = self.compute_fingerprint(fields, edges)
        with self.transaction() as conn:
            conn.execute("DELETE FROM field")
            conn.executemany(
                "INSERT INTO field (ref, table_name, name, data_type, deprecated, created_version)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (item.ref, item.table, item.name, item.data_type, int(item.deprecated), version)
                    for item in fields
                ],
            )
            conn.execute("DELETE FROM edge")
            conn.executemany(
                "INSERT INTO edge (edge_id, source_ref, target_ref, origin, created_version)"
                " VALUES (?, ?, ?, ?, ?)",
                [
                    (item.edge_id, item.source_ref, item.target_ref, item.origin, version)
                    for item in edges
                ],
            )
            self._write_snapshot(conn, version, fields, edges)
            conn.executemany(
                "INSERT INTO change_log (version, rule_id, rule_type, action, detail)"
                " VALUES (?, ?, ?, ?, ?)",
                [
                    (
                        change.version,
                        change.rule_id,
                        change.rule_type,
                        change.action,
                        json.dumps(change.detail, sort_keys=True, ensure_ascii=False),
                    )
                    for change in changes
                ],
            )
            conn.executemany(
                "INSERT INTO audit_event (version, rule_id, event_type, payload)"
                " VALUES (?, ?, ?, ?)",
                [
                    (event["version"], event["rule_id"], event["event_type"],
                     json.dumps(event["payload"], sort_keys=True, ensure_ascii=False))
                    for event in audit_events
                ],
            )
            conn.execute(
                "INSERT INTO version (version, status, rule_count, edge_count, field_count,"
                " fingerprint, applied_at) VALUES (?, 'committed', ?, ?, ?, ?, 'replayed')",
                (version, rule_count, len(edges), len(fields), fingerprint),
            )
            conn.execute("UPDATE meta SET last_version = ? WHERE id = 1", (version,))
        return fingerprint

    def rollback_to(self, target_version: int) -> None:
        """Truncate every version strictly newer than target_version."""
        current = self.last_version()
        if target_version < 0 or target_version > current:
            raise ValueError(f"rollback target {target_version} out of range [0, {current}]")
        if target_version == current:
            return
        with self.transaction() as conn:
            for table in ("snapshot_field", "snapshot_edge", "change_log", "audit_event", "version"):
                conn.execute(f"DELETE FROM {table} WHERE version > ?", (target_version,))
            fields = self.snapshot_fields(target_version)
            edges = self.snapshot_edges(target_version)
            conn.execute("DELETE FROM field")
            conn.executemany(
                "INSERT INTO field (ref, table_name, name, data_type, deprecated, created_version)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (item.ref, item.table, item.name, item.data_type, int(item.deprecated),
                     target_version)
                    for item in fields
                ],
            )
            conn.execute("DELETE FROM edge")
            conn.executemany(
                "INSERT INTO edge (edge_id, source_ref, target_ref, origin, created_version)"
                " VALUES (?, ?, ?, ?, ?)",
                [
                    (item.edge_id, item.source_ref, item.target_ref, item.origin, target_version)
                    for item in edges
                ],
            )
            conn.execute("UPDATE meta SET last_version = ? WHERE id = 1", (target_version,))

    def versions(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT version, status, rule_count, edge_count, field_count, fingerprint,"
            " applied_at FROM version ORDER BY version"
        ).fetchall()
        return [dict(row) for row in rows]

    def get_version(self, version: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM version WHERE version = ?", (version,)).fetchone()
        return dict(row) if row else None

    def change_log(self, version: int | None = None) -> list[dict]:
        if version is None:
            rows = self.conn.execute(
                "SELECT * FROM change_log ORDER BY version, id"
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM change_log WHERE version = ? ORDER BY id", (version,)
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item["detail"])
            result.append(item)
        return result

    def audit_events(self, version: int | None = None) -> list[dict]:
        if version is None:
            rows = self.conn.execute("SELECT * FROM audit_event ORDER BY version, id").fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM audit_event WHERE version = ? ORDER BY id", (version,)
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item["payload"])
            result.append(item)
        return result

    @staticmethod
    def compute_fingerprint(fields: list[Field], edges: list[Edge]) -> str:
        """Stable SHA-256 over the canonical field and edge sets."""
        field_lines = [
            f"{item.ref}|{item.data_type}|{int(item.deprecated)}" for item in sorted(fields, key=lambda f: f.ref)
        ]
        edge_lines = [
            f"{item.edge_id}|{item.source_ref}->{item.target_ref}|{item.origin}"
            for item in sorted(edges, key=lambda e: e.edge_id)
        ]
        payload = "\n".join(["#fields", *field_lines, "#edges", *edge_lines])
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()
