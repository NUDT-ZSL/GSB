"""SQLite persistence and transactional state transitions."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .errors import E_STATE, WorkbenchError
from .faults import maybe_kill
from .jsonio import canonical_hash
from .paths import Paths

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS package_registry (
    package_id TEXT PRIMARY KEY,
    manifest_version INTEGER NOT NULL,
    descriptor_digest TEXT NOT NULL,
    decision TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    loaded_clock_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshot_pointer (
    name TEXT PRIMARY KEY,
    snapshot_version INTEGER NOT NULL,
    package_id TEXT NOT NULL,
    view_hash TEXT NOT NULL,
    switched_clock_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshot_session (
    snapshot_version INTEGER NOT NULL,
    connection_id TEXT NOT NULL,
    source_format TEXT NOT NULL,
    generation INTEGER NOT NULL,
    logical_clock_ms INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY(snapshot_version, connection_id)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS staging_snapshot_session (
    connection_id TEXT PRIMARY KEY,
    source_format TEXT NOT NULL,
    generation INTEGER NOT NULL,
    logical_clock_ms INTEGER NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS unified_view (
    view_hash TEXT PRIMARY KEY,
    package_id TEXT NOT NULL,
    manifest_version INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    created_clock_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS table_version (
    version TEXT PRIMARY KEY,
    parent_version TEXT,
    digest TEXT NOT NULL,
    entries_json TEXT NOT NULL,
    patch_id TEXT,
    created_clock_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS applied_patch (
    patch_id TEXT PRIMARY KEY,
    patch_digest TEXT NOT NULL,
    new_version TEXT NOT NULL,
    closure_json TEXT NOT NULL,
    applied_clock_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS session_state (
    connection_id TEXT PRIMARY KEY,
    snapshot_version INTEGER NOT NULL,
    bound_snapshot_version INTEGER NOT NULL,
    generation INTEGER NOT NULL,
    logical_clock_ms INTEGER NOT NULL,
    table_version TEXT NOT NULL,
    route_keys_json TEXT NOT NULL,
    stage TEXT NOT NULL,
    stage_basis_json TEXT NOT NULL,
    watermark_ms INTEGER NOT NULL,
    claim_status TEXT NOT NULL DEFAULT 'pending',
    claim_worker TEXT,
    claim_token TEXT,
    updated_clock_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS forwarding_attempt (
    attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
    connection_id TEXT NOT NULL,
    snapshot_version INTEGER NOT NULL,
    generation INTEGER NOT NULL,
    table_version TEXT NOT NULL,
    dedupe_hash TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'pending',
    conclusion TEXT,
    result_json TEXT,
    evidence_hash TEXT,
    worker_id TEXT,
    claim_token TEXT,
    call_count INTEGER NOT NULL DEFAULT 0,
    created_clock_ms INTEGER NOT NULL,
    committed_clock_ms INTEGER,
    UNIQUE(connection_id, snapshot_version, generation, table_version)
);
CREATE INDEX IF NOT EXISTS idx_attempt_status ON forwarding_attempt(status, attempt_id);
CREATE TABLE IF NOT EXISTS closure_record (
    patch_id TEXT PRIMARY KEY,
    from_version TEXT NOT NULL,
    to_version TEXT NOT NULL,
    affected_keys_json TEXT NOT NULL,
    affected_sessions_json TEXT NOT NULL,
    reusable_sessions_json TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    computed_clock_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS clock_watermark (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    watermark_ms INTEGER NOT NULL
);
"""


class Store:
    def __init__(self, paths: Paths):
        self.paths = paths
        paths.ensure()
        self.connection = sqlite3.connect(paths.db_path, timeout=30, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA busy_timeout=30000")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.executescript(SCHEMA)
        self.connection.execute("INSERT OR IGNORE INTO clock_watermark(id, watermark_ms) VALUES (1, 0)")

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    @contextmanager
    def immediate(self) -> Iterator[sqlite3.Connection]:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield self.connection
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise
        else:
            self.connection.execute("COMMIT")

    def watermark(self) -> int:
        row = self.connection.execute("SELECT watermark_ms FROM clock_watermark WHERE id=1").fetchone()
        return int(row[0])

    def advance_watermark(self, value: int, db: sqlite3.Connection | None = None) -> int:
        value = int(value)
        executor = db or self.connection
        executor.execute(
            "UPDATE clock_watermark SET watermark_ms=MAX(watermark_ms, ?) WHERE id=1",
            (value,),
        )
        row = executor.execute("SELECT watermark_ms FROM clock_watermark WHERE id=1").fetchone()
        return int(row[0])

    def current_snapshot(self) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM snapshot_pointer WHERE name='current'").fetchone()

    def package(self, package_id: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM package_registry WHERE package_id=?", (package_id,)).fetchone()

    def register_package(
        self,
        *,
        package_id: str,
        manifest_version: int,
        descriptor_digest: str,
        decision: str,
        payload: dict[str, Any],
        clock_ms: int,
    ) -> None:
        with self.immediate() as db:
            db.execute(
                """
                INSERT INTO package_registry
                (package_id, manifest_version, descriptor_digest, decision, payload_json, loaded_clock_ms)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (package_id, manifest_version, descriptor_digest, decision, json.dumps(payload, sort_keys=True), clock_ms),
            )
            self.advance_watermark(clock_ms, db)

    def apply_snapshot(self, view: dict[str, Any], *, package_id: str, switched_clock_ms: int) -> int:
        version = int(view["manifest_version"])
        maybe_kill("snapshot_staged")
        with self.immediate() as db:
            db.execute("DELETE FROM staging_snapshot_session")
            for session in view["sessions"]:
                db.execute(
                    """
                    INSERT INTO staging_snapshot_session
                    (connection_id, source_format, generation, logical_clock_ms, payload_json)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        session["connection_id"],
                        session["selected_source_format"],
                        int(session["generation"]),
                        int(session["declared_logical_clock_ms"]),
                        json.dumps(session, sort_keys=True, ensure_ascii=False),
                    ),
                )
            maybe_kill("snapshot_before_switch")
            db.execute("DELETE FROM snapshot_session WHERE snapshot_version=?", (version,))
            db.execute(
                """
                INSERT INTO snapshot_session
                (snapshot_version, connection_id, source_format, generation, logical_clock_ms, payload_json)
                SELECT ?, connection_id, source_format, generation, logical_clock_ms, payload_json
                FROM staging_snapshot_session
                """,
                (version,),
            )
            db.execute(
                """
                INSERT INTO snapshot_pointer(name, snapshot_version, package_id, view_hash, switched_clock_ms)
                VALUES ('current', ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    snapshot_version=excluded.snapshot_version,
                    package_id=excluded.package_id,
                    view_hash=excluded.view_hash,
                    switched_clock_ms=excluded.switched_clock_ms
                """,
                (version, package_id, view["view_hash"], switched_clock_ms),
            )
            db.execute(
                "INSERT OR REPLACE INTO unified_view(view_hash, package_id, manifest_version, payload_json, created_clock_ms) VALUES (?, ?, ?, ?, ?)",
                (view["view_hash"], package_id, version, json.dumps(view, sort_keys=True, ensure_ascii=False), switched_clock_ms),
            )
            self.advance_watermark(int(view.get("clock_watermark_ms", switched_clock_ms)), db)
        return version

    def get_unified_view(self) -> dict[str, Any] | None:
        pointer = self.current_snapshot()
        if pointer is None:
            return None
        row = self.connection.execute(
            "SELECT payload_json FROM unified_view WHERE view_hash=?",
            (pointer["view_hash"],),
        ).fetchone()
        return json.loads(row["payload_json"]) if row else None

    def ensure_table_version(self, version: str, entries: list[dict[str, Any]], clock_ms: int, parent: str | None = None, patch_id: str | None = None) -> None:
        digest = canonical_hash(entries)
        with self.immediate() as db:
            db.execute(
                "INSERT OR IGNORE INTO table_version(version, parent_version, digest, entries_json, patch_id, created_clock_ms) VALUES (?, ?, ?, ?, ?, ?)",
                (version, parent, digest, json.dumps(entries, sort_keys=True, ensure_ascii=False), patch_id, clock_ms),
            )

    def upsert_sessions_from_view(self, view: dict[str, Any]) -> dict[str, int]:
        snapshot_version = int(view["manifest_version"])
        created = 0
        preserved = 0
        with self.immediate() as db:
            for session in view["sessions"]:
                connection_id = session["connection_id"]
                clock_ms = int(session["declared_logical_clock_ms"])
                existing = db.execute(
                    "SELECT * FROM session_state WHERE connection_id=?",
                    (connection_id,),
                ).fetchone()
                route_keys = session.get("route_keys", [])
                stage_hint = session.get("stage_hint", {})
                stage = stage_hint.get("stage", "declared")
                basis = {
                    "adjudication": session.get("adjudication"),
                    "table_version_comparison": session.get("table_version_comparison"),
                    "stage_hint": stage_hint,
                }
                if existing is None:
                    db.execute(
                        """
                        INSERT INTO session_state
                        (connection_id, snapshot_version, bound_snapshot_version, generation, logical_clock_ms,
                         table_version, route_keys_json, stage, stage_basis_json, watermark_ms,
                         claim_status, updated_clock_ms)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                        """,
                        (
                            connection_id,
                            snapshot_version,
                            snapshot_version,
                            int(session["generation"]),
                            clock_ms,
                            str(session.get("table_version")),
                            json.dumps(route_keys, sort_keys=True),
                            stage,
                            json.dumps(basis, sort_keys=True, ensure_ascii=False),
                            clock_ms,
                            clock_ms,
                        ),
                    )
                    created += 1
                else:
                    preserved += 1
                    if existing["claim_status"] != "claimed":
                        db.execute(
                            """
                            UPDATE session_state
                            SET snapshot_version=?, bound_snapshot_version=?, generation=?, logical_clock_ms=?,
                                table_version=?, route_keys_json=?,
                                stage_basis_json=?, updated_clock_ms=?
                            WHERE connection_id=?
                            """,
                            (snapshot_version, snapshot_version, int(session["generation"]), clock_ms,
                             str(session.get("table_version")),
                             json.dumps(route_keys, sort_keys=True),
                             json.dumps(basis, sort_keys=True, ensure_ascii=False), clock_ms, connection_id),
                        )
                dedupe = canonical_hash(
                    {
                        "connection_id": connection_id,
                        "snapshot_version": snapshot_version,
                        "generation": int(session["generation"]),
                        "table_version": str(session.get("table_version")),
                    }
                )
                db.execute(
                    """
                    INSERT OR IGNORE INTO forwarding_attempt
                    (connection_id, snapshot_version, generation, table_version, dedupe_hash,
                     status, created_clock_ms)
                    VALUES (?, ?, ?, ?, ?, 'pending', ?)
                    """,
                    (
                        connection_id,
                        snapshot_version,
                        int(session["generation"]),
                        str(session.get("table_version")),
                        dedupe,
                        clock_ms,
                    ),
                )
            self.advance_watermark(max([int(s["declared_logical_clock_ms"]) for s in view["sessions"]] + [0]), db)
        return {"created": created, "existing": preserved}

    def pending_attempts(self, connection_id: str | None = None, limit: int = 1000) -> list[sqlite3.Row]:
        if connection_id:
            return self.connection.execute(
                """
                SELECT * FROM forwarding_attempt
                WHERE connection_id=? AND status IN ('pending','claimed')
                ORDER BY attempt_id DESC LIMIT 1
                """,
                (connection_id,),
            ).fetchall()
        return self.connection.execute(
            """
            SELECT * FROM forwarding_attempt
            WHERE status='pending'
            ORDER BY connection_id, attempt_id
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    def claim_attempt(self, attempt_id: int, worker_id: str, token: str) -> sqlite3.Row | None:
        with self.immediate() as db:
            row = db.execute("SELECT * FROM forwarding_attempt WHERE attempt_id=?", (attempt_id,)).fetchone()
            if row is None or row["status"] == "committed":
                return None
            db.execute(
                "UPDATE forwarding_attempt SET status='claimed', worker_id=?, claim_token=? WHERE attempt_id=?",
                (worker_id, token, attempt_id),
            )
            db.execute(
                """
                UPDATE session_state
                SET claim_status='claimed', claim_worker=?, claim_token=?,
                    snapshot_version=?, bound_snapshot_version=?, generation=?, table_version=?,
                    updated_clock_ms=?
                WHERE connection_id=?
                """,
                (
                    worker_id,
                    token,
                    row["snapshot_version"],
                    row["snapshot_version"],
                    row["generation"],
                    row["table_version"],
                    self.watermark(),
                    row["connection_id"],
                ),
            )
            return db.execute("SELECT * FROM forwarding_attempt WHERE attempt_id=?", (attempt_id,)).fetchone()

    def committed_attempt(self, connection_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            """
            SELECT * FROM forwarding_attempt
            WHERE connection_id=? AND status='committed'
            ORDER BY committed_clock_ms DESC, attempt_id DESC LIMIT 1
            """,
            (connection_id,),
        ).fetchone()

    def commit_attempt(self, attempt_id: int, token: str, conclusion: str, result: dict[str, Any], call_count: int, clock_ms: int) -> None:
        result_json = json.dumps(result, sort_keys=True, ensure_ascii=False)
        evidence_hash = canonical_hash(result)
        maybe_kill("before_commit")
        with self.immediate() as db:
            row = db.execute("SELECT * FROM forwarding_attempt WHERE attempt_id=?", (attempt_id,)).fetchone()
            if row is None or row["claim_token"] != token:
                raise WorkbenchError(E_STATE, "attempt claim token no longer valid", {"attempt_id": attempt_id})
            db.execute(
                """
                UPDATE forwarding_attempt
                SET status='committed', conclusion=?, result_json=?, evidence_hash=?, call_count=?,
                    committed_clock_ms=?
                WHERE attempt_id=?
                """,
                (conclusion, result_json, evidence_hash, call_count, clock_ms, attempt_id),
            )
            stage = {
                "success": "response_returned",
                "failed": "failed",
                "skipped": "tombstone_applied",
            }.get(conclusion, "response_returned")
            db.execute(
                """
                UPDATE session_state
                SET stage=?, claim_status='committed', updated_clock_ms=?, watermark_ms=?
                WHERE connection_id=?
                """,
                (stage, clock_ms, clock_ms, row["connection_id"]),
            )
            self.advance_watermark(clock_ms, db)

    def reset_claim(self, attempt_id: int) -> None:
        with self.immediate() as db:
            row = db.execute("SELECT * FROM forwarding_attempt WHERE attempt_id=?", (attempt_id,)).fetchone()
            if row is None:
                return
            db.execute(
                "UPDATE forwarding_attempt SET status='pending', worker_id=NULL, claim_token=NULL WHERE attempt_id=? AND status='claimed'",
                (attempt_id,),
            )
            db.execute(
                "UPDATE session_state SET claim_status='pending', claim_worker=NULL, claim_token=NULL WHERE connection_id=? AND claim_token=?",
                (row["connection_id"], row["claim_token"]),
            )

    def all_status_rows(self) -> list[sqlite3.Row]:
        return self.connection.execute(
            """
            SELECT s.*, a.conclusion, a.result_json, a.evidence_hash, a.table_version AS attempt_table_version,
                   a.snapshot_version AS attempt_snapshot_version, a.status AS attempt_status,
                   a.committed_clock_ms, a.call_count
            FROM session_state s
            LEFT JOIN forwarding_attempt a ON a.attempt_id = (
                SELECT attempt_id FROM forwarding_attempt x
                WHERE x.connection_id=s.connection_id
                ORDER BY x.attempt_id DESC LIMIT 1
            )
            ORDER BY s.connection_id
            """
        ).fetchall()
